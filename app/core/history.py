"""履歴の永続化（設計書 §11・決定D9）。

単一ファイル `data/history.json` を全体書き換えで保存する。書き込みは
「tmp へ全体書き出し → `os.replace()`」の原子的手順のみを使い、
`app/core/fileops.py` の「partial → 検証 → 昇格」と同じ思想に揃える。

バックアップ順序（v1.1 改訂の最重要仕様）:
  1. `load()` はまず現行 `history.json` をパース＋スキーマ検証する。
  2. **正常だった場合のみ** `history.json.bak` を更新する（1世代）。
  3. 破損していた場合は `.bak` に一切触れず、破損ファイルを
     `history_corrupt_{YYYYmmdd_HHMMSS}.json` へ退避し、`.bak` 自体も
     パース検証したうえで復旧に使う。
  4. `.bak` も破損／不在なら空履歴で開始し、日本語警告を返す（例外にしない）。
  5. セッション中の更新（add / mark_*）では `.bak` を書き換えない。

パスの境界（設計書 §11.1・contracts の「パスの境界」）:
  外部（JobSpec / UI）とは**絶対パス**でやり取りし、JSON には必ず
  **data_root からの相対パス**で保存する。変換と範囲検証はこのモジュールが行う。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

log = logging.getLogger("atelier.history")

from app.core.contracts import (
    BackendIdentity,
    JobSpec,
    JobStatus,
    can_transition,
)
from app.core.naming import is_valid_manual_concat_id

SCHEMA_VERSION = 1

#: ジョブIDとして受け付けてよい文字（P5.2）。パス区切り・`..`・制御文字を排除する。
#: 「本物のIDかどうか」は履歴に実在するかで判定するので、ここでは**危険な文字列を
#: 通さないこと**だけを見る（チェーン連結と同じ強度に揃える）。
_SAFE_JOB_ID = re.compile(r"^[0-9A-Za-z_.-]{1,64}$")


def is_safe_job_id(job_id: str) -> bool:
    """ジョブIDとして安全か（パス区切り・`..`・制御文字を含まないか）。

    「実在する本物のIDか」は判定しない（それは履歴を引いて確かめる）。
    連結の入力検証と台帳の記録検証で**同じ判定**を使う。
    """
    return bool(_SAFE_JOB_ID.match(str(job_id))) and ".." not in str(job_id)


#: 後方互換の別名（モジュール内部の呼び出し用）
_is_safe_job_id = is_safe_job_id

#: 起動時中断の既定メッセージ（設計書 §9.1・§17.1）
INTERRUPTED_MESSAGE = "アプリ終了により中断"

#: resolve_chain が許容する最大チェーン長（root → 自分）。暴走・巨大連結の防止。
MAX_CHAIN_DEPTH = 20

#: 連結の最小本数（設計書 §10.6。1本だけの「連結」は意味がない）
MIN_CONCAT_CLIPS = 2

#: 任意順序連結（P5.2）で一度に選べる最大本数。チェーン深さ上限と揃える。
#: 20本 × 5.17秒 ≒ 103秒。検証式（§10.6.2）も 20本まで実測で確認済み。
MAX_CUSTOM_CONCAT_CLIPS = 20

#: 連結チェーン内で一致していなければならない項目（P4契約 §3）。
#: 表示名は日本語エラーメッセージにそのまま使う。
#: audio_sample_rate は履歴スキーマ v1 に無く（§11.2）、スキーマ変更は禁止のため
#: ここでは検証しない（V1 は 32000Hz 固定。config で強制される）。
CHAIN_COMPAT_FIELDS: tuple[tuple[str, str], ...] = (
    ("backend_id", "生成バックエンド"),
    ("model_id", "モデル"),
    ("width", "幅"),
    ("height", "高さ"),
    ("fps", "fps"),
)

#: 任意順序連結（P5.2）で一致していなければならない項目。
#: チェーン連結の項目に `model_revision` と `execution_engine` を**足した上位集合**。
#: チェーンでは継続生成の投入時検証が両者の一致を既に保証しているが（P4）、
#: 任意選択では利用者が無関係な動画を並べられるため、ここで明示的に確かめる。
#: **チェーン側の判定条件は変えない**（既存動作の非回帰を優先）。
CUSTOM_CONCAT_COMPAT_FIELDS: tuple[tuple[str, str], ...] = CHAIN_COMPAT_FIELDS + (
    ("model_revision", "モデルの版"),
    ("execution_engine", "生成方法（実機／お試し）"),
)

#: 状態の日本語表示（連結エラーメッセージ用）
_STATUS_LABELS_JA = {
    "queued": "待機中",
    "running": "生成中",
    "success": "成功",
    "failed": "失敗",
    "canceled": "キャンセル",
    "interrupted": "中断",
}

_JOB_TYPES = ("single", "continuation")
_EXECUTION_ENGINES = ("real", "mock")


class HistoryError(Exception):
    """履歴の読み書き・状態遷移の失敗（日本語メッセージ）。"""


# ---------------------------------------------------------------- 補助関数


def _aware(dt: datetime | None) -> datetime | None:
    """naive な datetime にローカルタイムゾーンのオフセットを付ける（設計書 §11.2）。

    JSON へは ISO8601（ローカルオフセット付き）で保存するため、レコード生成時点で
    aware へ正規化しておき、保存 → 読み込みの往復で値がぶれないようにする。
    """
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        raise HistoryError(f"日時の型が不正です: {dt!r}")
    if dt.tzinfo is None:
        return dt.astimezone()  # naive はローカル時刻とみなす
    return dt


def _dt_to_str(dt: datetime | None) -> str | None:
    aware = _aware(dt)
    return aware.isoformat() if aware is not None else None


def _dt_from_str(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HistoryError(f"{field_name} の型が不正です: {value!r}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise HistoryError(f"{field_name} の日時形式が不正です: {value}") from e


def relative_to_data_root(path: Path | str, data_root: Path) -> str:
    """絶対／相対パスを data_root 相対の POSIX 文字列へ変換する（範囲検証つき）。"""
    base = Path(data_root).resolve()
    p = Path(path)
    candidate = p if p.is_absolute() else (base / p)
    try:
        resolved = candidate.resolve()
    except OSError as e:  # pragma: no cover - 実行環境依存
        raise HistoryError(f"パスを解決できません: {path}（{e}）") from e
    if not resolved.is_relative_to(base):
        raise HistoryError(
            f"アプリのデータ領域の外のパスは履歴に保存できません: {path}"
        )
    return resolved.relative_to(base).as_posix()


def _require(d: dict, key: str) -> object:
    if key not in d or d[key] is None:
        raise HistoryError(f"履歴レコードに必須項目がありません: {key}")
    return d[key]


def _as_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise HistoryError(f"{field_name} の型が不正です: {value!r}")
    return value


def _as_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoryError(f"{field_name} の型が不正です: {value!r}")
    return value


def _opt_str(d: dict, key: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    return _as_str(value, key)


def _opt_int(d: dict, key: str) -> int | None:
    value = d.get(key)
    if value is None:
        return None
    return _as_int(value, key)


def _opt_float(d: dict, key: str) -> float | None:
    value = d.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HistoryError(f"{key} の型が不正です: {value!r}")
    return float(value)


# ---------------------------------------------------------------- レコード


@dataclass(frozen=True)
class HistoryRecord:
    """履歴1件（設計書 §11.2 スキーマ v1.2）。

    frozen（不変）にしている理由: HistoryStore は内部状態をそのまま返すため、
    呼び出し側の代入で履歴が壊れないようにする。状態更新は
    `dataclasses.replace()` で新しいレコードを作って差し替える。
    可変コンテナ（concat_sources / backend_params）は `__post_init__` で
    複製するので、`replace(rec)` を通した時点で内部と共有されなくなる。

    パスはすべて **data_root 相対**の文字列で保持する（絶対パスは保持しない）。
    """

    id: str
    type: str  # "single" | "continuation"
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    prompt: str
    duration_label: str
    num_frames: int
    fps: int
    width: int
    height: int
    steps: int
    seed_requested: int | None
    seed_used: int | None
    parent_id: str | None
    keyframe_path: str | None
    output_path: str | None
    last_frame_path: str | None
    concat_path: str | None
    concat_sources: list[str] | None
    elapsed_sec: float | None
    error: str | None
    error_category: str | None
    execution_engine: str  # "real" | "mock"
    backend_id: str
    model_id: str
    model_revision: str
    backend_params: dict | None
    app_version: str

    def __post_init__(self) -> None:
        # frozen なので object.__setattr__ で正規化する（生成時のみ）。
        object.__setattr__(self, "status", _coerce_status(self.status))
        object.__setattr__(self, "created_at", _aware(self.created_at))
        object.__setattr__(self, "started_at", _aware(self.started_at))
        object.__setattr__(self, "finished_at", _aware(self.finished_at))
        if self.created_at is None:
            raise HistoryError("created_at は必須です")
        if self.type not in _JOB_TYPES:
            raise HistoryError(f"ジョブ種別が不正です: {self.type}")
        if self.execution_engine not in _EXECUTION_ENGINES:
            raise HistoryError(f"実行エンジン種別が不正です: {self.execution_engine}")
        # 可変コンテナは複製して内部状態の共有を断つ
        if self.concat_sources is not None:
            object.__setattr__(self, "concat_sources", list(self.concat_sources))
        if self.backend_params is not None:
            object.__setattr__(self, "backend_params", dict(self.backend_params))

    # ------------------------------------------------------------ 変換

    def to_dict(self) -> dict:
        """設計書 §11.2 のフィールド順で JSON 化可能な dict を返す。"""
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status.value,
            "created_at": _dt_to_str(self.created_at),
            "started_at": _dt_to_str(self.started_at),
            "finished_at": _dt_to_str(self.finished_at),
            "prompt": self.prompt,
            "duration_label": self.duration_label,
            "num_frames": self.num_frames,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "steps": self.steps,
            "seed_requested": self.seed_requested,
            "seed_used": self.seed_used,
            "parent_id": self.parent_id,
            "keyframe_path": self.keyframe_path,
            "output_path": self.output_path,
            "last_frame_path": self.last_frame_path,
            "concat_path": self.concat_path,
            "concat_sources": (
                list(self.concat_sources) if self.concat_sources is not None else None
            ),
            "elapsed_sec": self.elapsed_sec,
            "error": self.error,
            "error_category": self.error_category,
            "execution_engine": self.execution_engine,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "backend_params": (
                dict(self.backend_params) if self.backend_params is not None else None
            ),
            "app_version": self.app_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> HistoryRecord:
        """JSON 由来の dict から復元する（スキーマ検証を兼ねる）。"""
        if not isinstance(d, dict):
            raise HistoryError(f"履歴レコードの形式が不正です: {type(d).__name__}")

        concat_sources = d.get("concat_sources")
        if concat_sources is not None:
            if not isinstance(concat_sources, list) or not all(
                isinstance(v, str) for v in concat_sources
            ):
                raise HistoryError(f"concat_sources の型が不正です: {concat_sources!r}")
            concat_sources = list(concat_sources)

        backend_params = d.get("backend_params")
        if backend_params is not None and not isinstance(backend_params, dict):
            raise HistoryError(f"backend_params の型が不正です: {backend_params!r}")

        return cls(
            id=_as_str(_require(d, "id"), "id"),
            type=_as_str(_require(d, "type"), "type"),
            status=_coerce_status(_require(d, "status")),
            created_at=_dt_from_str(_require(d, "created_at"), "created_at"),
            started_at=_dt_from_str(d.get("started_at"), "started_at"),
            finished_at=_dt_from_str(d.get("finished_at"), "finished_at"),
            prompt=_as_str(_require(d, "prompt"), "prompt"),
            duration_label=_as_str(_require(d, "duration_label"), "duration_label"),
            num_frames=_as_int(_require(d, "num_frames"), "num_frames"),
            fps=_as_int(_require(d, "fps"), "fps"),
            width=_as_int(_require(d, "width"), "width"),
            height=_as_int(_require(d, "height"), "height"),
            steps=_as_int(_require(d, "steps"), "steps"),
            seed_requested=_opt_int(d, "seed_requested"),
            seed_used=_opt_int(d, "seed_used"),
            parent_id=_opt_str(d, "parent_id"),
            keyframe_path=_opt_str(d, "keyframe_path"),
            output_path=_opt_str(d, "output_path"),
            last_frame_path=_opt_str(d, "last_frame_path"),
            concat_path=_opt_str(d, "concat_path"),
            concat_sources=concat_sources,
            elapsed_sec=_opt_float(d, "elapsed_sec"),
            error=_opt_str(d, "error"),
            error_category=_opt_str(d, "error_category"),
            execution_engine=_as_str(
                _require(d, "execution_engine"), "execution_engine"
            ),
            backend_id=_as_str(_require(d, "backend_id"), "backend_id"),
            model_id=_as_str(_require(d, "model_id"), "model_id"),
            model_revision=_as_str(_require(d, "model_revision"), "model_revision"),
            backend_params=dict(backend_params) if backend_params is not None else None,
            app_version=_as_str(_require(d, "app_version"), "app_version"),
        )

    @classmethod
    def from_job_spec(
        cls,
        spec: JobSpec,
        *,
        identity: BackendIdentity,
        execution_engine: str,
        app_version: str,
        data_root: Path,
        created_at: datetime,
    ) -> HistoryRecord:
        """投入直後（QUEUED）のレコードを作る。

        JobSpec の**絶対パス**を data_root 相対へ変換して格納する。
        data_root の外を指していた場合は HistoryError。
        """
        return cls(
            id=spec.job_id,
            type=spec.job_type,
            status=JobStatus.QUEUED,
            created_at=created_at,
            started_at=None,
            finished_at=None,
            prompt=spec.prompt,
            duration_label=spec.duration_label,
            num_frames=spec.num_frames,
            fps=spec.fps,
            width=spec.width,
            height=spec.height,
            steps=spec.steps,
            seed_requested=spec.seed_requested,
            seed_used=None,  # 成功時に確定する
            parent_id=spec.parent_id,
            keyframe_path=(
                relative_to_data_root(spec.keyframe_path, data_root)
                if spec.keyframe_path is not None
                else None
            ),
            output_path=relative_to_data_root(spec.output_path, data_root),
            last_frame_path=relative_to_data_root(spec.last_frame_path, data_root),
            concat_path=None,  # P4 で連結したときのみ設定される
            concat_sources=None,
            elapsed_sec=None,
            error=None,
            error_category=None,
            execution_engine=execution_engine,
            app_version=app_version,
            backend_id=spec.backend_id,
            model_id=identity.model_id,
            model_revision=identity.model_revision,
            backend_params=None,
        )


def _coerce_status(value: object) -> JobStatus:
    if isinstance(value, JobStatus):
        return value
    if isinstance(value, str):
        try:
            return JobStatus(value)
        except ValueError as e:
            raise HistoryError(f"ジョブ状態が不正です: {value}") from e
    raise HistoryError(f"ジョブ状態の型が不正です: {value!r}")


# ---------------------------------------------------------------- ストア


class HistoryStore:
    """`data/history.json` の読み書き（設計書 §11）。

    すべての公開メソッドは単一の `threading.RLock` で保護する
    （書き込みは UI プロセスのこのクラスのみ。設計書 §11.1）。
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, history_path: Path, data_root: Path) -> None:
        self._path = Path(history_path)
        self._data_root = Path(data_root)
        self._lock = threading.RLock()
        # 投入順を保つ（dict は挿入順を保持する）
        self._records: dict[str, HistoryRecord] = {}
        self._loaded = False
        # 遅延ロードで発生した警告を取りこぼさないための持ち越し
        self._pending_warnings: list[str] = []
        # .bak は「起動時点で検証済みのスナップショット」を保つ。
        # セッション中に load() を再度呼んでも .bak を実行中の内容で汚染しない。
        self._backup_written = False

    # ------------------------------------------------------------ パス

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data_root(self) -> Path:
        return self._data_root

    @property
    def backup_path(self) -> Path:
        return self._path.with_name(self._path.name + ".bak")

    def _new_tmp_path(self) -> Path:
        """書き込みごとに一意な tmp を作る（同一ディレクトリ内＝同一FS）。

        固定名にすると、同じ data_root を使う別インスタンスと tmp を奪い合い、
        os.replace が ENOENT で失敗したり部分書き込みが昇格したりする。
        """
        return self._path.with_name(
            f"{self._path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
        )

    def to_absolute(self, relpath: str | None) -> Path | None:
        """data_root 相対パス → 絶対パス（UI 表示・Finder 表示用）。

        履歴ファイルが手編集・部分破損していても data_root の外を指さないよう、
        下位層でも境界を検証する（外を指していたら None を返して「無いもの」として扱う）。
        """
        if relpath is None or relpath == "":
            return None
        p = Path(relpath)
        if p.is_absolute():
            log.warning("履歴に絶対パスが記録されています（無視します）: %s", relpath)
            return None
        try:
            resolved = (self._data_root / p).resolve()
            if not resolved.is_relative_to(self._data_root.resolve()):
                log.warning(
                    "履歴のパスがデータ領域の外を指しています（無視します）: %s", relpath
                )
                return None
        except OSError:
            return None
        return resolved

    def to_relative(self, path: Path | str | None) -> str | None:
        """絶対パス → data_root 相対パス。data_root の外なら HistoryError。"""
        if path is None or path == "":
            return None
        return relative_to_data_root(path, self._data_root)

    # ------------------------------------------------------------ 読み込み

    def load(self) -> list[str]:
        """history.json を読み込み、UI 表示用の日本語警告一覧を返す（設計書 §11.1）。"""
        with self._lock:
            warnings: list[str] = list(self._pending_warnings)
            self._pending_warnings.clear()

            if not self._path.exists():
                # 初回起動: 空の履歴ファイルを作る。
                # 書けない場合（読み取り専用・満杯）も起動は止めず警告に落とす。
                self._records = {}
                self._loaded = True
                self._save_locked_quietly(warnings)
                return warnings

            try:
                raw = self._path.read_text(encoding="utf-8")
                records = _parse_document(raw)
            except (HistoryError, OSError, UnicodeDecodeError) as e:
                warnings.extend(self._recover_locked(str(e)))
                self._loaded = True
                return warnings

            # 正常に読めた場合のみ .bak を更新する（v1.1 改訂の核心）。
            # ただしプロセス内で1回だけ＝「起動時点のスナップショット」を保つ。
            self._records = records
            self._loaded = True
            if not self._backup_written:
                try:
                    self._write_backup_locked(raw)
                    self._backup_written = True
                except OSError as e:
                    warnings.append(f"履歴のバックアップを更新できませんでした（{e}）")
            return warnings

    def _recover_locked(self, reason: str) -> list[str]:
        """破損した現行ファイルを退避し、`.bak` から復旧する（`.bak` は書き換えない）。"""
        warnings: list[str] = []
        corrupt_path = self._quarantine_locked()
        if corrupt_path is None:
            warnings.append(
                f"履歴ファイルを読み込めませんでした（{reason}）。"
                "破損ファイルの退避にも失敗しました"
            )
        else:
            warnings.append(
                f"履歴ファイルを読み込めませんでした（{reason}）。"
                f"破損ファイルを {corrupt_path.name} へ退避しました"
            )

        quarantined = corrupt_path.name if corrupt_path is not None else "なし"
        bak = self.backup_path
        if not bak.exists():
            self._records = {}
            warnings.append(
                "バックアップが見つからないため、履歴を復旧できませんでした"
                f"（退避ファイル: {quarantined}）。空の履歴で開始します"
            )
            self._save_locked_quietly(warnings)
            return warnings

        try:
            # .bak も必ずパース＋スキーマ検証してから使う（設計書 §11.1）
            bak_records = _parse_document(bak.read_text(encoding="utf-8"))
        except (HistoryError, OSError, UnicodeDecodeError) as e:
            self._records = {}
            warnings.append(
                f"バックアップも読み込めませんでした（{e}）。"
                f"履歴を復旧できませんでした（退避ファイル: {quarantined}）。"
                "空の履歴で開始します"
            )
            self._save_locked_quietly(warnings)
            return warnings

        self._records = bak_records
        warnings.append(
            f"バックアップ（{bak.name}）から履歴 {len(bak_records)} 件を復旧しました"
        )
        # 復旧内容で history.json を作り直す（.bak は触らない）
        self._save_locked_quietly(warnings)
        return warnings

    def _quarantine_locked(self) -> Path | None:
        """破損した history.json を `history_corrupt_{日時}.json` へ退避する。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{self._path.stem}_corrupt_{stamp}"
        target = self._path.with_name(f"{base}.json")
        counter = 1
        while target.exists():
            target = self._path.with_name(f"{base}_{counter}.json")
            counter += 1
        try:
            os.replace(self._path, target)  # 同一ディレクトリ内なので原子的
        except OSError:
            return None
        return target

    # ------------------------------------------------------------ 保存

    def _save_locked(self) -> None:
        """全体を tmp へ書き出し → os.replace() で差し替える（設計書 §11.1）。"""
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "records": [r.to_dict() for r in self._records.values()],
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as e:  # pragma: no cover - 契約違反時のみ
            raise HistoryError(f"履歴をJSONへ変換できません: {e}") from e

        tmp = self._new_tmp_path()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)  # 中途半端な tmp を残さない
            except OSError:  # pragma: no cover - 実行環境依存
                pass
            raise HistoryError(f"履歴を保存できません: {self._path}（{e}）") from e

    def _save_locked_quietly(self, warnings: list[str]) -> None:
        """復旧経路の保存失敗は警告に落とす（起動を止めない）。"""
        try:
            self._save_locked()
        except HistoryError as e:
            warnings.append(str(e))

    def _write_backup_locked(self, raw: str) -> None:
        """検証済みの現行内容を `.bak` へ1世代コピーする（load 成功時のみ）。"""
        bak = self.backup_path
        tmp = bak.with_name(f"{bak.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, bak)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)  # 孤児 tmp を残さない
            except OSError:  # pragma: no cover - 実行環境依存
                pass
            raise

    def _ensure_loaded_locked(self) -> None:
        """load() 未呼び出しでの更新が既存履歴を潰さないための保険。"""
        if not self._loaded:
            self._pending_warnings.extend(self.load())

    # ------------------------------------------------------------ 起動時復元

    def startup_recover(self) -> int:
        """残存する QUEUED / RUNNING を INTERRUPTED へ確定する（自動再投入はしない）。"""
        with self._lock:
            self._ensure_loaded_locked()
            now = datetime.now().astimezone()
            previous = dict(self._records)
            changed = 0
            for job_id, rec in list(self._records.items()):
                if rec.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                    continue  # SUCCESS / FAILED / CANCELED / INTERRUPTED は触らない
                self._records[job_id] = replace(
                    rec,
                    status=JobStatus.INTERRUPTED,
                    finished_at=rec.finished_at or now,
                    error=rec.error or INTERRUPTED_MESSAGE,
                    # 成果物は昇格前に落ちているため参照させない（設計書 §10.7）
                    output_path=None,
                    last_frame_path=None,
                )
                changed += 1
            if changed:
                try:
                    self._save_locked()
                except HistoryError:
                    self._records = previous  # 保存できなければ変更を巻き戻す
                    raise
            return changed

    # ------------------------------------------------------------ 参照・追加

    def add(self, record: HistoryRecord) -> None:
        with self._lock:
            self._ensure_loaded_locked()
            if record.id in self._records:
                raise HistoryError(f"既に履歴に存在するジョブIDです: {record.id}")
            # 絶対パスで渡された場合もここで相対化・範囲検証する
            normalized = replace(
                record,
                keyframe_path=self.to_relative(record.keyframe_path),
                output_path=self.to_relative(record.output_path),
                last_frame_path=self.to_relative(record.last_frame_path),
                concat_path=self.to_relative(record.concat_path),
            )
            self._records[normalized.id] = normalized
            try:
                self._save_locked()
            except HistoryError:
                # 「メモリの内容＝ディスクに書けた内容」を不変条件に保つ
                self._records.pop(normalized.id, None)
                raise

    def get(self, job_id: str) -> HistoryRecord | None:
        with self._lock:
            self._ensure_loaded_locked()
            rec = self._records.get(job_id)
            return _public(rec) if rec is not None else None

    def list_records(
        self,
        *,
        newest_first: bool = True,
        statuses: Iterable[JobStatus] | None = None,
    ) -> list[HistoryRecord]:
        with self._lock:
            self._ensure_loaded_locked()
            wanted = None
            if statuses is not None:
                wanted = {_coerce_status(s) for s in statuses}
            items = [
                _public(r)
                for r in self._records.values()
                if wanted is None or r.status in wanted
            ]
            if newest_first:
                items.reverse()  # 保存は投入順。新しい順は反転で得る
            return items

    # ------------------------------------------------------------ 状態更新

    def _get_locked(self, job_id: str) -> HistoryRecord:
        rec = self._records.get(job_id)
        if rec is None:
            raise HistoryError(f"履歴に存在しないジョブIDです: {job_id}")
        return rec

    def _transition_locked(
        self, job_id: str, new_status: JobStatus, **changes
    ) -> HistoryRecord:
        current = self._get_locked(job_id)
        if not can_transition(current.status, new_status):
            raise HistoryError(
                f"許可されていない状態遷移です（{job_id}: "
                f"{current.status.value} → {new_status.value}）"
            )
        updated = replace(current, status=new_status, **changes)
        self._records[job_id] = updated
        try:
            self._save_locked()
        except HistoryError:
            # 「メモリの内容＝ディスクに書けた内容」を不変条件に保つ
            self._records[job_id] = current
            raise
        return _public(updated)

    def mark_running(self, job_id: str, started_at: datetime) -> HistoryRecord:
        with self._lock:
            self._ensure_loaded_locked()
            return self._transition_locked(
                job_id, JobStatus.RUNNING, started_at=started_at
            )

    def mark_success(
        self,
        job_id: str,
        *,
        output_path: Path,
        last_frame_path: Path | None,
        seed_used: int | None,
        elapsed_sec: float | None,
        finished_at: datetime,
    ) -> HistoryRecord:
        """成果物の**昇格後**にのみ呼ぶ（設計書 §10.7）。絶対パスを相対で保存する。"""
        with self._lock:
            self._ensure_loaded_locked()
            return self._transition_locked(
                job_id,
                JobStatus.SUCCESS,
                output_path=self.to_relative(output_path),
                last_frame_path=self.to_relative(last_frame_path),
                seed_used=seed_used,
                elapsed_sec=elapsed_sec,
                finished_at=finished_at,
                error=None,
                error_category=None,
            )

    def mark_failed(
        self,
        job_id: str,
        *,
        error: str,
        category: str | None,
        elapsed_sec: float | None,
        finished_at: datetime,
    ) -> HistoryRecord:
        with self._lock:
            self._ensure_loaded_locked()
            return self._transition_locked(
                job_id,
                JobStatus.FAILED,
                error=error,
                error_category=category,
                elapsed_sec=elapsed_sec,
                finished_at=finished_at,
                # 失敗時は成果物が昇格していない（設計書 §11.2: 失敗時 null）
                output_path=None,
                last_frame_path=None,
            )

    def mark_canceled(self, job_id: str, canceled_at: datetime) -> HistoryRecord:
        with self._lock:
            self._ensure_loaded_locked()
            return self._transition_locked(
                job_id,
                JobStatus.CANCELED,
                finished_at=canceled_at,
                output_path=None,
                last_frame_path=None,
            )

    # ------------------------------------------------------------ チェーン

    def resolve_chain(self, job_id: str) -> list[HistoryRecord]:
        """root → 自分 の順で親子チェーンを返す（設計書 §10.6）。"""
        with self._lock:
            self._ensure_loaded_locked()
            chain: list[HistoryRecord] = []
            seen: set[str] = set()
            current = self._get_locked(job_id)
            while True:
                if current.id in seen:
                    raise HistoryError(
                        f"履歴の親子関係が循環しています: {current.id}"
                    )
                seen.add(current.id)
                chain.append(current)
                if len(chain) > MAX_CHAIN_DEPTH:
                    raise HistoryError(
                        f"継続生成のチェーンが長すぎます（最大{MAX_CHAIN_DEPTH}件）: {job_id}"
                    )
                parent_id = current.parent_id
                if parent_id is None:
                    break
                parent = self._records.get(parent_id)
                if parent is None:
                    raise HistoryError(
                        f"親ジョブが履歴に見つかりません（{current.id} の親: {parent_id}）"
                    )
                current = parent
            chain.reverse()  # root → 自分
            return [_public(r) for r in chain]

    def resolve_concat_chain(self, job_id: str) -> list[HistoryRecord]:
        """**連結可能な**チェーンを root → 選択ノードの順で返す（P4契約 §3）。

        `resolve_chain()` の結果（親を遡るので選択ノードより後の子孫は含まれない）に
        対して、連結の前提条件を追加検証する。1つでも満たさなければ日本語の
        HistoryError を送出し、**部分的な連結を絶対に行わせない**。

        検証順序（ユーザーにとって直せる順）:
          1. 全件が SUCCESS か
          2. backend_id / model_id / width / height / fps が揃っているか
          3. 成果物ファイルが実在するか
          4. 2本以上あるか
        """
        with self._lock:
            self._ensure_loaded_locked()
            chain = self.resolve_chain(job_id)

            self._require_all_success(chain, context="チェーンに")
            self._require_compatible(chain)
            self._require_outputs_exist(chain)

            # 本数（root 単体を選んだ場合はここで止まる）
            if len(chain) < MIN_CONCAT_CLIPS:
                raise HistoryError(
                    f"連結には2本以上の動画が必要です（{job_id} は継続元がありません）"
                )

            return chain

    # ------------------------------------------------------------ 連結の共通検証
    #
    # チェーン連結（resolve_concat_chain）と任意順序連結（resolve_custom_concat）で
    # **同じ判定・同じ日本語文言**を使うための共通部品（P5.2）。
    # チェーン固有の親探索・循環検出は resolve_chain() にだけ残してある。

    @staticmethod
    def _require_all_success(records: list[HistoryRecord], *, context: str) -> None:
        """全件が SUCCESS であること（未完了・失敗・中断は成果物が無い）。"""
        bad = [r for r in records if r.status is not JobStatus.SUCCESS]
        if bad:
            detail = "、".join(
                f"{r.id}（{_STATUS_LABELS_JA.get(r.status.value, r.status.value)}）"
                for r in bad
            )
            raise HistoryError(
                f"連結できません。{context}成功していない動画が含まれています: {detail}"
            )

    @staticmethod
    def _require_compatible(
        records: list[HistoryRecord],
        fields: tuple[tuple[str, str], ...] = CHAIN_COMPAT_FIELDS,
    ) -> None:
        """解像度・fps・モデル・実行方式が混在した動画をつながない。

        比較の基準は先頭のレコード。任意順序連結では「先頭＝ユーザーが最初に
        並べた動画」になるが、全件が一致していなければならない点は同じ。
        検査する項目だけを引数で切り替える（判定と文言は1つに保つ）。
        """
        head = records[0]
        for rec in records[1:]:
            for field_name, label in fields:
                expected = getattr(head, field_name)
                actual = getattr(rec, field_name)
                if expected != actual:
                    raise HistoryError(
                        f"連結できません。{label}が一致しません"
                        f"（{head.id}: {expected} / {rec.id}: {actual}）"
                    )

    def _require_outputs_exist(self, records: list[HistoryRecord]) -> None:
        """成果物が data_root 内に通常ファイルとして実在すること。"""
        missing: list[str] = []
        for rec in records:
            absolute = self.to_absolute(rec.output_path)
            if absolute is None or not absolute.is_file():
                missing.append(rec.id)
        if missing:
            raise HistoryError(
                "連結できません。動画ファイルが見つかりません: " + "、".join(missing)
            )

    def resolve_custom_concat(self, job_ids: Iterable[str]) -> list[HistoryRecord]:
        """任意順序連結の入力を検証し、**指定された順のまま**返す（P5.2・設計書 §23.2）。

        `resolve_concat_chain()` と違い親子関係を一切見ない。代わりに
        「ユーザーが選んだ任意の並び」が連結してよいものかを確かめる。
        **並べ替えは絶対にしない**（作成日時順・ID順へ整えると、ユーザーが
        指定した順番と成果物が食い違う）。

        検証順序（ユーザーにとって直せる順）:
          1. ID の形式（手編集・でたらめな値を弾く）
          2. 重複していないこと
          3. 本数が 2〜20 本
          4. 履歴に実在すること
          5. 全件が SUCCESS
          6. 全件が個別動画であること（連結成果物は素材にできない）
          7. backend / model / revision / 実行方式 / 幅 / 高さ / fps の一致
          8. 成果物が data_root 内に実在すること
        """
        ids = [str(v).strip() for v in job_ids]

        # 1. ID の安全性（履歴を引く前に弾く）。
        #    ここで見るのは「危険な文字列でないこと」だけで、本人確認は
        #    4.（履歴に実在するか）が行う。形式を `v_日付_時刻_乱数` に限定すると
        #    チェーン連結より厳しくなり、同じ履歴なのに一方だけ連結できない
        #    という不整合が生まれるため、そこまでは要求しない。
        malformed = [i for i in ids if not _is_safe_job_id(i)]
        if malformed:
            raise HistoryError(
                "連結できません。動画IDの形式が正しくありません: "
                + "、".join(i if i else "（空）" for i in malformed)
            )

        # 1b. 連結成果物（`cm_...`）は素材にできない。実在検査でも弾かれるが、
        #     理由が伝わる文言で先に断る。
        products = [i for i in ids if is_valid_manual_concat_id(i)]
        if products:
            raise HistoryError(
                "連結できません。連結した動画は素材にできません"
                "（個別の動画だけを選んでください）: " + "、".join(products)
            )

        # 2. 重複（同じ動画を2回使うのは V1 では禁止）
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise HistoryError(
                "連結できません。同じ動画が複数回選ばれています: " + "、".join(duplicated)
            )

        # 3. 本数
        if len(ids) < MIN_CONCAT_CLIPS:
            raise HistoryError(
                f"連結には2本以上の動画が必要です（選択: {len(ids)}本）"
            )
        if len(ids) > MAX_CUSTOM_CONCAT_CLIPS:
            raise HistoryError(
                f"一度に連結できるのは{MAX_CUSTOM_CONCAT_CLIPS}本までです"
                f"（選択: {len(ids)}本）"
            )

        with self._lock:
            self._ensure_loaded_locked()

            # 4. 履歴に実在すること
            unknown = [i for i in ids if i not in self._records]
            if unknown:
                raise HistoryError(
                    "連結できません。履歴に無い動画が選ばれています: " + "、".join(unknown)
                )
            records = [_public(self._records[i]) for i in ids]  # ★ 指定順のまま

            # 5. SUCCESS 以外
            self._require_all_success(records, context="選んだ動画に")

            # 6. 個別動画のみ（連結成果物・任意連結成果物は素材にできない）
            not_clips = [r.id for r in records if r.type not in _JOB_TYPES]
            if not_clips:
                raise HistoryError(
                    "連結できません。個別の動画だけを選んでください: "
                    + "、".join(not_clips)
                )

            # 7. 互換性（チェーンより厳しい上位集合で見る）、8. 成果物の実在
            self._require_compatible(records, CUSTOM_CONCAT_COMPAT_FIELDS)
            self._require_outputs_exist(records)

            return records

    def mark_concat(
        self,
        job_id: str,
        *,
        concat_path: Path,
        concat_sources: Iterable[str],
    ) -> HistoryRecord:
        """連結MP4の**昇格後にのみ**呼ぶ（設計書 §10.6 ⑤・§10.7）。

        状態は SUCCESS のまま変えない（連結は生成ジョブの状態遷移ではない）ため、
        `_transition_locked` は使わずここで直接差し替える。スキーマは変更せず、
        既存の `concat_path` / `concat_sources` フィールドだけを埋める。
        """
        with self._lock:
            self._ensure_loaded_locked()
            current = self._get_locked(job_id)
            if current.status is not JobStatus.SUCCESS:
                raise HistoryError(
                    "成功していないジョブに連結結果は記録できません"
                    f"（{job_id}: {current.status.value}）"
                )
            sources = [str(s) for s in concat_sources]
            if len(sources) < MIN_CONCAT_CLIPS:
                raise HistoryError(
                    f"連結元は2件以上必要です（{job_id}: {len(sources)}件）"
                )
            if sources[-1] != job_id:
                raise HistoryError(
                    "連結元の末尾は連結先のジョブIDと一致している必要があります"
                    f"（{job_id} / 末尾: {sources[-1]}）"
                )
            updated = replace(
                current,
                concat_path=self.to_relative(concat_path),
                concat_sources=sources,
            )
            self._records[job_id] = updated
            try:
                self._save_locked()
            except HistoryError:
                # 「メモリの内容＝ディスクに書けた内容」を不変条件に保つ
                self._records[job_id] = current
                raise
            return _public(updated)


def _public(record: HistoryRecord) -> HistoryRecord:
    """外部へ返す複製。frozen なので代入は防げるが、可変コンテナの共有も断つ。"""
    return replace(record)


def _parse_document(raw: str) -> dict[str, HistoryRecord]:
    """history.json 本文をパース＋スキーマ検証する（失敗は HistoryError）。"""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HistoryError(f"JSON構文エラー: {e}") from e
    if not isinstance(doc, dict):
        raise HistoryError("履歴ファイルの形式が不正です（オブジェクトではありません）")

    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise HistoryError(
            f"schema_version が対応外です（検出: {version!r}、対応: {SCHEMA_VERSION}）"
        )

    raw_records = doc.get("records")
    if not isinstance(raw_records, list):
        raise HistoryError("records が配列ではありません")

    records: dict[str, HistoryRecord] = {}
    for i, item in enumerate(raw_records):
        try:
            rec = HistoryRecord.from_dict(item)
        except HistoryError as e:
            raise HistoryError(f"records[{i}] が不正です: {e}") from e
        if rec.id in records:
            raise HistoryError(f"ジョブIDが重複しています: {rec.id}")
        records[rec.id] = rec
    return records
