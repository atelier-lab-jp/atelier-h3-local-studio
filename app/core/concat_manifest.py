"""任意順序連結（P5.2）の成果物台帳 `data/concat_manifest.json`。

**なぜ history.json に載せないのか**（設計書 §23）:
チェーン連結は「終端の個別動画レコードの `concat_path` / `concat_sources`」に
記録される（§10.6）。この形は1レコードにつき連結1件しか持てないため、
同じ動画を別の組み合わせで何度も使う任意連結（A→B→C と A→D→C など）を
載せると**先に作った成果物を上書きしてしまう**。かといって履歴スキーマを
変えると、旧版のアプリで `type` 検証に落ちて全履歴がパース不能になり、
隔離＋`.bak` 復旧が走って直近の履歴を失う。したがって
**history.json は1バイトも変更せず**、任意連結だけを別ファイルで管理する。

保存の規律は `HistoryStore` の実証済み方式に揃える（設計書 §11.1）:
  1. `load()` はまず現行ファイルをパース＋スキーマ検証する
  2. **正常だった場合のみ** `.bak` を更新する（1世代・プロセス内1回）
  3. 破損時は `.bak` に触れず、破損ファイルを
     `concat_manifest_corrupt_{YYYYmmdd_HHMMSS}.json` へ退避する
  4. `.bak` も**パース検証してから**復旧に使う
  5. 両方壊れていれば空で開始し、日本語警告を返す（例外にしない）
  6. セッション中の更新（`add`）では `.bak` を書き換えない

**MP4 は絶対に削除しない**。台帳が壊れても成果物ファイルは残り、
一覧に出なくなるだけである（再連結すれば作り直せる）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.core.history import is_safe_job_id, relative_to_data_root
from app.core.naming import is_valid_manual_concat_id

log = logging.getLogger("atelier.concat_manifest")

SCHEMA_VERSION = 1

#: 1件の任意連結が使える素材の本数（設計書 §23.2）
MIN_MANUAL_CLIPS = 2
MAX_MANUAL_CLIPS = 20


class ConcatManifestError(Exception):
    """台帳の読み書き・検証の失敗（日本語メッセージ）。"""


def _require(d: dict, key: str):
    if key not in d or d[key] is None:
        raise ConcatManifestError(f"{key} が指定されていません")
    return d[key]


def _as_str(value, key: str) -> str:
    if not isinstance(value, str):
        raise ConcatManifestError(f"{key} の型が不正です: {value!r}")
    return value


def _as_int(value, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConcatManifestError(f"{key} の型が不正です: {value!r}")
    return value


def _dt_to_str(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _dt_from_str(value, key: str) -> datetime:
    text = _as_str(value, key)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise ConcatManifestError(f"{key} の日時形式が不正です: {text}") from e
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


# ---------------------------------------------------------------- エントリ


@dataclass(frozen=True)
class ManualConcatEntry:
    """任意順序連結1件（不変）。

    `sources` は**ユーザーが指定した順そのまま**で保持する。作成日時順や
    ID順へ並べ替えてはならない（並べ替えると成果物と台帳が食い違う）。
    パスは data_root 相対で保持する（history と同じ規律）。
    """

    id: str
    created_at: datetime
    output_path: str
    sources: tuple[str, ...]
    clips: int
    num_frames_total: int
    fps: int
    width: int
    height: int
    backend_id: str
    model_id: str
    model_revision: str
    execution_engine: str
    app_version: str
    #: 将来の 1080p 高品質化（P6）で埋める。今は常に None（設計書 §23.6）
    upscale_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        validate_entry_fields(
            concat_id=self.id,
            sources=self.sources,
            clips=self.clips,
            output_path=self.output_path,
            num_frames_total=self.num_frames_total,
            fps=self.fps,
            width=self.width,
            height=self.height,
        )

    @property
    def duration_sec(self) -> float:
        """合計フレーム数と fps から算出する（duration 自体は保存しない）。"""
        return self.num_frames_total / float(self.fps)

    @property
    def duration_label(self) -> str:
        return f"{self.duration_sec:.2f}秒"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": _dt_to_str(self.created_at),
            "output_path": self.output_path,
            "sources": list(self.sources),
            "clips": self.clips,
            "num_frames_total": self.num_frames_total,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "execution_engine": self.execution_engine,
            "app_version": self.app_version,
            "upscale_path": self.upscale_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ManualConcatEntry":
        if not isinstance(d, dict):
            raise ConcatManifestError(
                f"エントリの形式が不正です: {type(d).__name__}"
            )
        sources = _require(d, "sources")
        if not isinstance(sources, list) or not all(
            isinstance(v, str) for v in sources
        ):
            raise ConcatManifestError(f"sources の型が不正です: {sources!r}")
        upscale = d.get("upscale_path")
        if upscale is not None and not isinstance(upscale, str):
            raise ConcatManifestError(f"upscale_path の型が不正です: {upscale!r}")
        return cls(
            id=_as_str(_require(d, "id"), "id"),
            created_at=_dt_from_str(_require(d, "created_at"), "created_at"),
            output_path=_as_str(_require(d, "output_path"), "output_path"),
            sources=tuple(sources),
            clips=_as_int(_require(d, "clips"), "clips"),
            num_frames_total=_as_int(
                _require(d, "num_frames_total"), "num_frames_total"
            ),
            fps=_as_int(_require(d, "fps"), "fps"),
            width=_as_int(_require(d, "width"), "width"),
            height=_as_int(_require(d, "height"), "height"),
            backend_id=_as_str(_require(d, "backend_id"), "backend_id"),
            model_id=_as_str(_require(d, "model_id"), "model_id"),
            model_revision=_as_str(_require(d, "model_revision"), "model_revision"),
            execution_engine=_as_str(
                _require(d, "execution_engine"), "execution_engine"
            ),
            app_version=_as_str(_require(d, "app_version"), "app_version"),
            upscale_path=upscale,
        )


def validate_entry_fields(
    *,
    concat_id: str,
    sources,
    clips: int,
    output_path: str,
    num_frames_total: int,
    fps: int,
    width: int,
    height: int,
) -> None:
    """エントリの必須条件（ID形式・本数・重複・パス形式・正の整数）。

    `__post_init__` から呼ぶので、**壊れたエントリはそもそも生成できない**。
    """
    if not is_valid_manual_concat_id(concat_id):
        raise ConcatManifestError(f"任意連結IDの形式が不正です: {concat_id!r}")

    ids = list(sources)
    if len(ids) < MIN_MANUAL_CLIPS:
        raise ConcatManifestError(
            f"連結元は{MIN_MANUAL_CLIPS}件以上必要です（{concat_id}: {len(ids)}件）"
        )
    if len(ids) > MAX_MANUAL_CLIPS:
        raise ConcatManifestError(
            f"連結元は{MAX_MANUAL_CLIPS}件までです（{concat_id}: {len(ids)}件）"
        )
    if len(set(ids)) != len(ids):
        raise ConcatManifestError(f"連結元のジョブIDが重複しています: {concat_id}")
    bad = [i for i in ids if not is_safe_job_id(i)]
    if bad:
        raise ConcatManifestError(
            "連結元のジョブID形式が不正です: " + "、".join(i if i else "（空）" for i in bad)
        )
    if clips != len(ids):
        raise ConcatManifestError(
            f"clips と sources の件数が一致しません（{clips} / {len(ids)}）"
        )

    if not output_path or Path(output_path).is_absolute():
        # data_root 相対だけを保存する（絶対パスはプロジェクト移動で壊れる）
        raise ConcatManifestError(
            f"出力パスはデータ領域からの相対パスで保存してください: {output_path!r}"
        )
    if ".." in Path(output_path).parts:
        raise ConcatManifestError(
            f"出力パスにデータ領域の外を指す表記が含まれています: {output_path}"
        )

    for name, value in (
        ("num_frames_total", num_frames_total),
        ("fps", fps),
        ("width", width),
        ("height", height),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConcatManifestError(f"{name} は1以上の整数で指定してください: {value!r}")


def _parse_document(raw: str) -> dict[str, ManualConcatEntry]:
    """台帳本文をパース＋スキーマ検証する（失敗は ConcatManifestError）。"""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConcatManifestError(f"JSON構文エラー: {e}") from e
    if not isinstance(doc, dict):
        raise ConcatManifestError("台帳の形式が不正です（オブジェクトではありません）")

    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConcatManifestError(
            f"schema_version が対応外です（検出: {version!r}、対応: {SCHEMA_VERSION}）"
        )

    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        raise ConcatManifestError("entries が配列ではありません")

    entries: dict[str, ManualConcatEntry] = {}
    for i, item in enumerate(raw_entries):
        try:
            entry = ManualConcatEntry.from_dict(item)
        except ConcatManifestError as e:
            raise ConcatManifestError(f"entries[{i}] が不正です: {e}") from e
        if entry.id in entries:
            raise ConcatManifestError(f"任意連結IDが重複しています: {entry.id}")
        entries[entry.id] = entry
    return entries


# ---------------------------------------------------------------- ストア


class ConcatManifest:
    """任意順序連結の台帳（書き込みは UI プロセスのこのクラスのみ）。"""

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, manifest_path: Path, data_root: Path) -> None:
        self._path = Path(manifest_path)
        self._data_root = Path(data_root)
        self._lock = threading.RLock()
        self._entries: dict[str, ManualConcatEntry] = {}
        self._loaded = False
        self._pending_warnings: list[str] = []
        # .bak は「起動時点で検証済みのスナップショット」。セッション中は汚さない
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
        """書き込みごとに一意な tmp（同一ディレクトリ＝同一FS で os.replace が原子的）。

        固定名にすると同じ data_root を使う別インスタンスと奪い合い、
        os.replace が ENOENT で失敗したり部分書き込みが昇格したりする。
        """
        return self._path.with_name(
            f"{self._path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
        )

    def to_absolute(self, relpath: str | None) -> Path | None:
        """data_root 相対 → 絶対パス。**data_root の外を指していたら None**。"""
        if relpath is None or relpath == "":
            return None
        p = Path(relpath)
        if p.is_absolute():
            log.warning("台帳に絶対パスが記録されています（無視します）: %s", relpath)
            return None
        try:
            resolved = (self._data_root / p).resolve()
            if not resolved.is_relative_to(self._data_root.resolve()):
                log.warning(
                    "台帳のパスがデータ領域の外を指しています（無視します）: %s", relpath
                )
                return None
        except OSError:
            return None
        return resolved

    def to_relative(self, path: Path | str | None) -> str | None:
        if path is None or path == "":
            return None
        return relative_to_data_root(path, self._data_root)

    # ------------------------------------------------------------ 読み込み

    def load(self) -> list[str]:
        """台帳を読み込み、UI 表示用の日本語警告一覧を返す。

        **ファイルが無いのは異常ではない**（任意連結を1件も作っていない既存環境）。
        その場合は空で開始し、警告も出さない。
        """
        with self._lock:
            warnings: list[str] = list(self._pending_warnings)
            self._pending_warnings.clear()

            if not self._path.exists():
                # 初回。ここではファイルを作らない（作らなくても add で作られる）。
                self._entries = {}
                self._loaded = True
                return warnings

            try:
                raw = self._path.read_text(encoding="utf-8")
                entries = _parse_document(raw)
            except (ConcatManifestError, OSError, UnicodeDecodeError) as e:
                log.warning("任意連結の台帳を読み込めませんでした: %s", e)
                warnings.extend(self._recover_locked(str(e)))
                self._loaded = True
                return warnings

            self._entries = entries
            self._loaded = True
            if not self._backup_written:
                try:
                    self._write_backup_locked(raw)
                    self._backup_written = True
                except OSError as e:
                    warnings.append(
                        f"任意連結の台帳のバックアップを更新できませんでした（{e}）"
                    )
            return warnings

    def _recover_locked(self, reason: str) -> list[str]:
        """破損した現行ファイルを退避し、`.bak` から復旧する（`.bak` は書き換えない）。"""
        warnings: list[str] = []
        corrupt_path = self._quarantine_locked()
        if corrupt_path is None:
            warnings.append(
                f"任意連結の記録ファイルを読み込めませんでした（{reason}）。"
                "破損ファイルの退避にも失敗しました"
            )
        else:
            warnings.append(
                f"任意連結の記録ファイルを読み込めませんでした（{reason}）。"
                f"破損ファイルを {corrupt_path.name} へ退避しました"
            )
        quarantined = corrupt_path.name if corrupt_path is not None else "なし"

        bak = self.backup_path
        if not bak.exists():
            self._entries = {}
            warnings.append(
                "バックアップが見つからないため、任意連結の記録を復旧できませんでした"
                f"（退避ファイル: {quarantined}）。"
                "作成済みの連結動画のファイル自体は削除していません"
            )
            log.warning(
                "任意連結台帳を復旧できません（.bak なし）。退避: %s / MP4 は %s に残存",
                quarantined,
                self._data_root / "concat",
            )
            return warnings

        try:
            # .bak も必ずパース＋スキーマ検証してから使う
            bak_entries = _parse_document(bak.read_text(encoding="utf-8"))
        except (ConcatManifestError, OSError, UnicodeDecodeError) as e:
            self._entries = {}
            warnings.append(
                f"バックアップも読み込めませんでした（{e}）。"
                f"任意連結の記録を復旧できませんでした（退避ファイル: {quarantined}）。"
                "作成済みの連結動画のファイル自体は削除していません"
            )
            log.warning(
                "任意連結台帳の .bak も破損（%s）。退避: %s / MP4 は %s に残存",
                e,
                quarantined,
                self._data_root / "concat",
            )
            return warnings

        self._entries = bak_entries
        warnings.append(
            f"バックアップ（{bak.name}）から任意連結の記録 {len(bak_entries)} 件を復旧しました"
        )
        # 復旧内容で本体を作り直す（.bak は触らない）
        try:
            self._save_locked()
        except ConcatManifestError as e:
            warnings.append(str(e))
        return warnings

    def _quarantine_locked(self) -> Path | None:
        """破損ファイルを `concat_manifest_corrupt_{日時}.json` へ退避する。"""
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
        """全体を tmp へ書き出し → fsync → `os.replace()` で差し替える。"""
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "entries": [e.to_dict() for e in self._entries.values()],
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as e:  # pragma: no cover - 契約違反時のみ
            raise ConcatManifestError(f"任意連結の記録をJSONへ変換できません: {e}") from e

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
            raise ConcatManifestError(
                f"任意連結の記録を保存できません: {self._path}（{e}）"
            ) from e

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
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 実行環境依存
                pass
            raise

    def _ensure_loaded_locked(self) -> None:
        if not self._loaded:
            self._pending_warnings.extend(self.load())

    # ------------------------------------------------------------ 参照・更新

    def list_entries(self, *, newest_first: bool = True) -> list[ManualConcatEntry]:
        with self._lock:
            self._ensure_loaded_locked()
            items = [replace(e) for e in self._entries.values()]
        items.sort(key=lambda e: (e.created_at, e.id), reverse=newest_first)
        return items

    def get(self, concat_id: str) -> ManualConcatEntry | None:
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(str(concat_id))
            return replace(entry) if entry is not None else None

    def new_id(self, now: datetime | None = None) -> str:
        """台帳にも実ファイルにも衝突しないIDを採番する（設計書 §10.3 と同じ思想）。"""
        from app.core.naming import new_manual_concat_id

        concat_dir = self._data_root / "concat"
        with self._lock:
            self._ensure_loaded_locked()
            for _ in range(100):
                candidate = new_manual_concat_id(now)
                if candidate in self._entries:
                    continue
                # ファイル名は本数を含むので、接頭辞一致で既存ファイルを確認する
                if any(concat_dir.glob(f"{candidate}_*clips.mp4")):
                    continue
                return candidate
        raise ConcatManifestError(
            "任意連結IDを採番できませんでした（時刻と乱数が繰り返し衝突しました）"
        )

    def add(self, entry: ManualConcatEntry) -> ManualConcatEntry:
        """エントリを追記する。**成果物の正式名への昇格後にのみ呼ぶこと。**

        保存に失敗した場合はメモリ上の状態を**呼び出し前へ戻して**から送出する
        （「メモリの内容＝ディスクに書けた内容」を不変条件に保つ）。
        呼び出し側はこの例外を捕まえて、昇格済み MP4 をロールバックする。
        """
        if not isinstance(entry, ManualConcatEntry):
            raise ConcatManifestError(
                f"エントリの型が不正です: {type(entry).__name__}"
            )
        with self._lock:
            self._ensure_loaded_locked()
            if entry.id in self._entries:
                raise ConcatManifestError(
                    f"同じIDの任意連結がすでに記録されています: {entry.id}"
                )
            # 保存先が data_root の中にあることを、記録する前に必ず確かめる
            if self.to_absolute(entry.output_path) is None:
                raise ConcatManifestError(
                    f"出力パスがデータ領域の外を指しています: {entry.output_path}"
                )
            previous = dict(self._entries)
            self._entries[entry.id] = entry
            try:
                self._save_locked()
            except ConcatManifestError:
                self._entries = previous  # メモリとディスクの乖離を作らない
                raise
            return replace(entry)
