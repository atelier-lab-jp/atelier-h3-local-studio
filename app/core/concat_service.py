"""連結の実行サービス（設計書 §10.6・§10.6.1・§10.7・§23、P4契約 §4・§5）。

2種類の連結を**1つのサービス**で扱う。同じレーン（同じ排他・同じスレッド管理・
同じ失敗時清掃）に載せることで、両者が同時に走ることが構造的に起こらない。

| 種類 | 入口 | 入力の解決 | 記録先 |
|---|---|---|---|
| チェーン連結（P4） | `start_concat(job_id)` | `resolve_concat_chain()` | 履歴の `concat_path` |
| 指定順連結（P5.2） | `start_custom_concat(job_ids)` | `resolve_custom_concat()` | `concat_manifest.json` |

責務:
  - 入力の解決と再検証を**実行スレッドの中で**行う（開始時ではなく実行時の状態で判断）
  - （config で有効な場合のみ）連結境界の重複1フレームを**比較してから**除去する
    （指定順連結では隣接クリップに親子関係が無いため、この処理自体を行わない）
  - `ffmpeg_ops.concat_reencode()` で partial へ書き出し、検証して原子的に昇格する
    （**V1 の連結方式はこれ1つだけ**。`-c copy` は config から到達できない。P5）
  - 昇格後にのみ記録する。**記録に失敗したら昇格済み MP4 を取り消す**（§23.4）

守る不変条件:
  - **連結は同時に1つだけ**（`ffmpeg_ops._lock` に加えサービス層でも排他。種類も跨ぐ）
  - **Gradio コールバックをブロックしない**（daemon スレッドで実行し UI は status() を読む）
  - 失敗時に正式名も孤児 partial も残さない（P4契約 §4）
  - **source 動画を一切変更しない**（ffmpeg は入力を読むだけ。出力は必ず新規ファイル）
  - 指定順連結は**ユーザーが指定した順序をそのまま**使う（並べ替えない）
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.core import ffmpeg_ops as fo
from app.core.fileops import FileopsError, ensure_within, partial_path
from app.core.ffmpeg_ops import FfmpegError, FrameDiff
from app.core.history import HistoryError, HistoryRecord, HistoryStore

log = logging.getLogger("atelier.concat")

# 状態（UI はこの文字列で分岐する。増やす場合は UI 側と同時に更新すること）
STATE_IDLE = "idle"
STATE_RESOLVING = "resolving"
STATE_CONCATENATING = "concatenating"
STATE_VERIFYING = "verifying"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: 実行中とみなす状態（この間は新しい連結を受け付けない）
BUSY_STATES = frozenset({STATE_RESOLVING, STATE_CONCATENATING, STATE_VERIFYING})

STATE_LABELS_JA = {
    STATE_IDLE: "待機中",
    STATE_RESOLVING: "チェーンを確認中",
    STATE_CONCATENATING: "連結中",
    STATE_VERIFYING: "検証中",
    STATE_DONE: "完成",
    STATE_FAILED: "失敗",
}

#: 連結の種類（P5.2）。**チェーン連結と任意順序連結は同じサービスで排他される**
MODE_CHAIN = "chain"
MODE_CUSTOM = "custom"

MODE_LABELS_JA = {MODE_CHAIN: "チェーン連結", MODE_CUSTOM: "指定順連結"}


class ConcatError(Exception):
    """連結の実行を受け付けられない（日本語メッセージ）。"""


# ---------------------------------------------------------------- 状態


@dataclass(frozen=True)
class ConcatStatus:
    """UI が Timer で読む不変スナップショット（内部状態を露出させない）。"""

    state: str = STATE_IDLE
    key: str | None = None
    job_id: str | None = None
    message: str = ""
    clips: int = 0
    sources: tuple[str, ...] = ()
    output_path: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None
    #: 重複フレームを除去した境界の入力インデックス（0始まり。除去なしなら空）
    trimmed_boundaries: tuple[int, ...] = ()
    #: 連結の種類（P5.2）。既定は従来どおりチェーン連結＝既存 UI は非回帰
    mode: str = MODE_CHAIN
    #: 任意順序連結の成果物ID（`cm_...`）。チェーン連結では None
    concat_id: str | None = None

    @property
    def running(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def state_label(self) -> str:
        return STATE_LABELS_JA.get(self.state, self.state)

    @property
    def mode_label(self) -> str:
        return MODE_LABELS_JA.get(self.mode, self.mode)


# ---------------------------------------------------------------- ランナー


class ConcatRunner(Protocol):
    """ffmpeg 実行の差し替え口（テストで実バイナリを使わない経路を作るため）。"""

    def concat(
        self,
        inputs: list[Path],
        out_path: Path,
        *,
        fps: int,
        sample_rate: int,
        expected_duration_sec: float | None,
        trim_first_frame_of: set[int] | None,
        warnings_out: list[str] | None,
        expected_frames: int | None,
    ) -> Path: ...

    def extract_frame(self, video: Path, frame_index: int, out_png: Path) -> Path: ...

    def compare(self, png_a: Path, png_b: Path) -> FrameDiff: ...

    def probe(self, video: Path): ...


class FfmpegConcatRunner:
    """既定の実装。ffmpeg 実体の解決は初回使用時まで遅延する。"""

    def __init__(self, ffmpeg_path: str = "", timeout: int = 900) -> None:
        self._configured = ffmpeg_path
        self._resolved: str | None = None
        self._timeout = timeout

    @property
    def ffmpeg(self) -> str:
        if self._resolved is None:
            self._resolved = fo.resolve_ffmpeg(self._configured)
        return self._resolved

    def concat(
        self,
        inputs: list[Path],
        out_path: Path,
        *,
        fps: int,
        sample_rate: int,
        expected_duration_sec: float | None,
        trim_first_frame_of: set[int] | None,
        warnings_out: list[str] | None,
        expected_frames: int | None = None,
    ) -> Path:
        return fo.concat_reencode(
            self.ffmpeg,
            inputs,
            out_path,
            fps=fps,
            sample_rate=sample_rate,
            expected_duration_sec=expected_duration_sec,
            timeout=self._timeout,
            trim_first_frame_of=trim_first_frame_of,
            warnings_out=warnings_out,
            expected_frames=expected_frames,
        )

    def extract_frame(self, video: Path, frame_index: int, out_png: Path) -> Path:
        return fo.extract_frame_exact(self.ffmpeg, video, frame_index, out_png)

    def compare(self, png_a: Path, png_b: Path) -> FrameDiff:
        return fo.compare_frames(png_a, png_b)

    def probe(self, video: Path):
        """素材のデコード確認（P5.2 の任意連結でのみ使う）。"""
        return fo.decode_probe(self.ffmpeg, video)


# ---------------------------------------------------------------- サービス


class ConcatService:
    """連結の実行・排他・状態管理（UI からはこのオブジェクトだけを触る）。"""

    def __init__(
        self,
        cfg,
        history: HistoryStore,
        *,
        ffmpeg_path: str = "",
        runner: ConcatRunner | None = None,
        manifest=None,
    ) -> None:
        self._cfg = cfg
        self._history = history
        #: 任意順序連結の台帳（P5.2）。None ならチェーン連結だけが使える
        self._manifest = manifest
        self._runner: ConcatRunner = runner or FfmpegConcatRunner(
            ffmpeg_path or getattr(cfg, "ffmpeg_path", "")
        )
        self._lock = threading.RLock()
        self._status = ConcatStatus()
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._closed = False

    # ------------------------------------------------------------ 公開API

    def start_concat(self, job_id: str) -> str:
        """バックグラウンドで連結を開始し、状態キーを返す（呼び出しはブロックしない）。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise ConcatError("連結する動画を選んでください")
        job_id = job_id.strip()

        with self._lock:
            key = self._claim_locked(
                f"concat-{self._seq + 1}-{job_id}",
                mode=MODE_CHAIN,
                job_id=job_id,
                message="チェーンを確認しています…",
            )
            self._spawn_locked(key, self._run, (key, job_id))
            return key

    def start_custom_concat(self, job_ids) -> str:
        """指定された順番どおりに連結する（P5.2・設計書 §23）。

        受け取るのは**ジョブIDの並びだけ**で、ファイルパスは一切受け取らない。
        並びの検証と正式なパス解決は実行スレッドの中で行う（開始した瞬間の
        状態ではなく、**実際に連結を始める時点**の状態で判断するため）。
        """
        if self._manifest is None:
            raise ConcatError("指定順の連結を利用できません（記録先を初期化できませんでした）")
        ids = [str(v).strip() for v in (job_ids or [])]
        if not ids:
            raise ConcatError("連結する動画を選んでください")

        with self._lock:
            key = self._claim_locked(
                f"custom-{self._seq + 1}",
                mode=MODE_CUSTOM,
                job_id=None,
                message="選んだ動画を確認しています…",
                clips=len(ids),
                sources=tuple(ids),
            )
            self._spawn_locked(key, self._run_custom, (key, ids))
            return key

    def _claim_locked(
        self,
        key: str,
        *,
        mode: str,
        job_id: str | None,
        message: str,
        clips: int = 0,
        sources: tuple[str, ...] = (),
    ) -> str:
        """実行権を1つだけ取る。**チェーン連結と任意連結で共通**（相互排他の要）。"""
        if self._closed:
            raise ConcatError("アプリを終了中のため連結を開始できません")
        if self._status.running:
            running_label = MODE_LABELS_JA.get(self._status.mode, "連結")
            target = self._status.job_id or f"{self._status.clips}本"
            raise ConcatError(
                "連結を実行中です。完了までお待ちください"
                f"（実行中: {running_label} / {target}）"
            )
        self._seq += 1
        self._status = ConcatStatus(
            state=STATE_RESOLVING,
            key=key,
            job_id=job_id,
            mode=mode,
            message=message,
            clips=clips,
            sources=sources,
            started_at=datetime.now().astimezone(),
        )
        return key

    def _spawn_locked(self, key: str, target, args: tuple) -> None:
        thread = threading.Thread(
            target=target, args=args, name=f"atelier-concat-{self._seq}", daemon=True
        )
        self._thread = thread
        thread.start()

    def status(self) -> ConcatStatus:
        """現在の状態の不変スナップショット（UI のポーリング用）。"""
        with self._lock:
            return self._status

    def shutdown(self, timeout: float = 5.0) -> None:
        """新規受付を止め、実行中のスレッドの終了を待つ（強制終了はしない）。"""
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning(
                    "連結スレッドが %.1f 秒以内に終了しませんでした（daemon のため放置します）",
                    timeout,
                )

    # ------------------------------------------------------------ 内部

    def _set(self, key: str, **changes) -> None:
        """自分の実行キーのときだけ状態を更新する（古いスレッドの上書きを防ぐ）。"""
        with self._lock:
            if self._status.key != key:
                return
            self._status = replace(self._status, **changes)

    def _run(self, key: str, job_id: str) -> None:
        out_path: Path | None = None
        # 以前の成功で作られた正式ファイルを、今回の失敗で消してしまわないための記録
        output_existed_before = False
        try:
            chain = self._history.resolve_concat_chain(job_id)
            inputs = self._chain_inputs(chain)
            sources = tuple(r.id for r in chain)
            n = len(chain)
            fps = chain[0].fps
            out_path = self._output_path(job_id, n)
            output_existed_before = out_path.exists()

            self._set(
                key,
                state=STATE_CONCATENATING,
                clips=n,
                sources=sources,
                message=f"連結しています…（{n}本）",
            )

            warnings: list[str] = []
            trim = self._plan_dedupe(chain, inputs, warnings)
            # 主検証はフレーム数（各動画のフレーム数の合計 − 除去枚数）。
            # duration は補助で、許容差は境界数に比例して広がる（ffmpeg_ops 参照）。
            expected_frames = sum(r.num_frames for r in chain) - len(trim)
            expected = expected_frames / fps if fps else None
            # 連結が失敗しても判定内容が残るよう、実行前に一度載せておく
            self._set(key, warnings=tuple(warnings), trimmed_boundaries=tuple(sorted(trim)))

            out_path.parent.mkdir(parents=True, exist_ok=True)
            self._runner.concat(
                inputs,
                out_path,
                fps=fps,
                sample_rate=getattr(self._cfg, "audio_sample_rate", 32000),
                expected_duration_sec=expected,
                trim_first_frame_of=trim or None,
                warnings_out=warnings,
                expected_frames=expected_frames,
            )

            self._set(
                key,
                state=STATE_VERIFYING,
                warnings=tuple(warnings),
                trimmed_boundaries=tuple(sorted(trim)),
                message="連結結果を検証しています…",
            )
            self._verify_promoted(out_path)

            # 昇格が確認できた後にのみ履歴へ記録する（設計書 §10.6 ⑤）
            self._history.mark_concat(
                job_id, concat_path=out_path, concat_sources=list(sources)
            )

            self._set(
                key,
                state=STATE_DONE,
                output_path=out_path,
                finished_at=datetime.now().astimezone(),
                message=f"連結が完了しました（{n}本 / {out_path.name}）",
                error=None,
            )
            log.info(
                "連結完了: %s（%d本・除去境界 %s）",
                out_path.name,
                n,
                sorted(trim) or "なし",
            )
        except BaseException as e:  # noqa: BLE001 - スレッド内で握りつぶさず状態に載せる
            message = self._error_message(e)
            self._cleanup_failed(out_path, keep_final=output_existed_before)
            self._set(
                key,
                state=STATE_FAILED,
                output_path=None,
                finished_at=datetime.now().astimezone(),
                message=message,
                error=message,
            )
            log.warning("連結に失敗しました（%s）: %s", job_id, message)

    def _run_custom(self, key: str, job_ids: list[str]) -> None:
        """任意順序連結の本体（P5.2）。

        契約（設計書 §23.4）:
          1. 隠し partial へ書き出す（`concat_reencode` の中で行われる）
          2. 映像・音声・フレーム数・duration・デコードを検証する
          3. 検証に通ったものだけを `os.replace()` で正式名へ昇格する
          4. 台帳へ原子的に追記する
          5. **台帳の保存に失敗したら、今回昇格した正式 MP4 を取り消す**
        「一覧に出るのに台帳に無い MP4」も「台帳にあるのにファイルが無い記録」も
        作らない。元動画と既存の連結成果物には最後まで触れない。
        """
        out_path: Path | None = None
        promoted = False
        try:
            records = self._history.resolve_custom_concat(job_ids)
            inputs = self._chain_inputs(records)  # 指定順のまま（並べ替えない）
            self._verify_sources(records, inputs)

            n = len(records)
            fps = records[0].fps
            sources = tuple(r.id for r in records)
            concat_id = self._manifest.new_id()
            out_path = self._custom_output_path(concat_id, n)

            self._set(
                key,
                state=STATE_CONCATENATING,
                clips=n,
                sources=sources,
                concat_id=concat_id,
                message=f"指定した順番で連結しています…（{n}本）",
            )

            warnings: list[str] = []
            expected_frames = sum(r.num_frames for r in records)
            expected = expected_frames / fps if fps else None

            out_path.parent.mkdir(parents=True, exist_ok=True)
            self._runner.concat(
                inputs,
                out_path,
                fps=fps,
                sample_rate=getattr(self._cfg, "audio_sample_rate", 32000),
                expected_duration_sec=expected,
                # 任意順序では隣り合う動画に親子関係が無いので境界の重複除去はしない
                trim_first_frame_of=None,
                warnings_out=warnings,
                expected_frames=expected_frames,
            )

            self._set(
                key,
                state=STATE_VERIFYING,
                warnings=tuple(warnings),
                message="連結結果を検証しています…",
            )
            self._verify_promoted(out_path)
            promoted = True

            self._record_manifest(
                concat_id=concat_id,
                out_path=out_path,
                records=records,
                expected_frames=expected_frames,
            )

            self._set(
                key,
                state=STATE_DONE,
                output_path=out_path,
                finished_at=datetime.now().astimezone(),
                message=f"指定した順番の連結が完了しました（{n}本 / {out_path.name}）",
                error=None,
            )
            log.info("指定順連結が完了しました: %s（%d本・順序 %s）", out_path.name, n, sources)
        except BaseException as e:  # noqa: BLE001 - スレッド内で握りつぶさず状態に載せる
            message = self._error_message(e)
            if promoted and out_path is not None:
                # 台帳へ載らなかった成果物は、一覧から辿れない孤児になる。必ず取り消す
                message = self._rollback_promoted(out_path, message)
            self._cleanup_failed(out_path, keep_final=False)
            self._set(
                key,
                state=STATE_FAILED,
                output_path=None,
                finished_at=datetime.now().astimezone(),
                message=message,
                error=message,
            )
            log.warning("指定順連結に失敗しました（%s）: %s", job_ids, message)

    # ------------------------------------------------------------ 補助（任意連結）

    def _verify_sources(self, records: list[HistoryRecord], inputs: list[Path]) -> None:
        """素材が実際にデコードでき、音声仕様が揃っていることを確かめる（P5.2）。

        履歴スキーマ v1 には音声仕様の欄が無いため（§11.2・変更禁止）、
        ここだけは**実ファイルを見て**判定する。チェーン連結には無い検査だが、
        任意順序では無関係な動画を並べられるので実施する。
        """
        from app.core.ffmpeg_ops import audio_sample_rate_of

        expected_rate = int(getattr(self._cfg, "audio_sample_rate", 32000))
        rates: list[tuple[str, int | None]] = []
        for rec, path in zip(records, inputs):
            try:
                probe = self._runner.probe(path)
            except (FfmpegError, OSError) as e:
                raise ConcatError(
                    f"連結できません。動画を読み込めませんでした（{rec.id}: {e}）"
                ) from e
            if not probe.has_video:
                raise ConcatError(f"連結できません。映像が入っていません（{rec.id}）")
            if not probe.has_audio:
                raise ConcatError(f"連結できません。音声が入っていません（{rec.id}）")
            rates.append((rec.id, audio_sample_rate_of(probe)))

        known = [(job_id, rate) for job_id, rate in rates if rate is not None]
        mismatched = [
            f"{job_id}: {rate}Hz" for job_id, rate in known if rate != expected_rate
        ]
        if mismatched:
            raise ConcatError(
                f"連結できません。音声の形式が揃っていません（想定 {expected_rate}Hz / "
                + "、".join(mismatched)
                + "）"
            )

    def _custom_output_path(self, concat_id: str, clips: int) -> Path:
        """`data/concat/cm_{日時}_{乱数}_{本数}clips.mp4`（設計書 §23.3）。"""
        from app.core.naming import manual_concat_filename

        concat_dir = Path(getattr(self._cfg, "concat_dir"))
        data_root = Path(getattr(self._cfg, "data_root"))
        try:
            resolved_dir = ensure_within(data_root, concat_dir)
        except FileopsError as e:
            raise ConcatError(f"連結の出力先がデータ領域の外です: {concat_dir}") from e
        return resolved_dir / manual_concat_filename(concat_id, clips)

    def _record_manifest(
        self,
        *,
        concat_id: str,
        out_path: Path,
        records: list[HistoryRecord],
        expected_frames: int,
    ) -> None:
        """昇格が確認できた後にだけ台帳へ追記する（失敗は呼び出し側がロールバック）。"""
        from app.core.concat_manifest import ManualConcatEntry

        head = records[0]
        entry = ManualConcatEntry(
            id=concat_id,
            created_at=datetime.now().astimezone(),
            output_path=self._manifest.to_relative(out_path),
            sources=tuple(r.id for r in records),
            clips=len(records),
            num_frames_total=expected_frames,
            fps=head.fps,
            width=head.width,
            height=head.height,
            backend_id=head.backend_id,
            model_id=head.model_id,
            model_revision=head.model_revision,
            execution_engine=head.execution_engine,
            app_version=str(getattr(self._cfg, "version", "") or "0"),
        )
        self._manifest.add(entry)

    def _rollback_promoted(self, out_path: Path, message: str) -> str:
        """台帳へ載せられなかった正式 MP4 を取り消す（設計書 §23.4 手順5〜9）。

        削除できなければ隔離名へ退避し、それも無理なら**正確なパスと対処方法を
        ログへ残す**。いずれの場合も一覧には出ない（台帳に無いため）。
        """
        try:
            out_path.unlink(missing_ok=True)
            log.warning(
                "記録に失敗したため、連結動画を取り消しました: %s", out_path.name
            )
            return message + "（作りかけの連結動画は削除しました）"
        except OSError as unlink_error:
            log.warning(
                "連結動画を削除できませんでした: %s（%s）", out_path, unlink_error
            )

        quarantine = out_path.with_name(f".orphan_{out_path.name}")
        try:
            os.replace(out_path, quarantine)
            log.warning(
                "連結動画を隔離しました（一覧には出ません）: %s", quarantine.name
            )
            return message + f"（作りかけの連結動画を {quarantine.name} へ退避しました）"
        except OSError as move_error:
            log.error(
                "連結動画の削除にも隔離にも失敗しました。"
                "この動画は記録に載っていないため一覧には出ません。"
                "不要であれば Finder で手動削除してください: %s（削除エラー: %s）",
                out_path,
                move_error,
            )
            return message + (
                f"（不要なファイルが残りました: {out_path.name}。"
                "「詳しい情報」に場所を記録しています）"
            )

    # ------------------------------------------------------------ 補助

    @staticmethod
    def _error_message(e: BaseException) -> str:
        """UI へ出す日本語メッセージ（既知の例外は本文をそのまま使う）。"""
        from app.core.concat_manifest import ConcatManifestError

        text = str(e).strip()
        if isinstance(
            e, (HistoryError, FfmpegError, FileopsError, ConcatError, ConcatManifestError)
        ):
            return text or "連結に失敗しました"
        detail = f"{type(e).__name__}: {text}" if text else type(e).__name__
        return f"連結に失敗しました（{detail}）"

    def _chain_inputs(self, chain: list[HistoryRecord]) -> list[Path]:
        """履歴の相対パスを絶対パスへ変換する（resolve_concat_chain で実在検証済み）。"""
        inputs: list[Path] = []
        for rec in chain:
            absolute = self._history.to_absolute(rec.output_path)
            if absolute is None or not absolute.is_file():
                # 解決から連結までの間に消えた場合
                raise HistoryError(f"連結できません。動画ファイルが見つかりません: {rec.id}")
            inputs.append(absolute)
        return inputs

    def _output_path(self, job_id: str, clips: int) -> Path:
        """`data/concat/c_{子id}_{本数}clips.mp4`（既存命名。設計書 §10.6）。"""
        # 履歴が手編集されていても出力先が data/concat から出ないようにする
        if "/" in job_id or "\\" in job_id or job_id in (".", ".."):
            raise ConcatError(f"連結できないジョブIDです: {job_id}")
        concat_dir = Path(getattr(self._cfg, "concat_dir"))
        data_root = Path(getattr(self._cfg, "data_root"))
        try:
            resolved_dir = ensure_within(data_root, concat_dir)
        except FileopsError as e:
            raise ConcatError(f"連結の出力先がデータ領域の外です: {concat_dir}") from e
        return resolved_dir / f"c_{job_id}_{clips}clips.mp4"

    def _verify_promoted(self, out_path: Path) -> None:
        """昇格後の最終確認（デコード検証は promote 内で済んでいる）。"""
        if not out_path.is_file():
            raise FileopsError(f"連結動画が作成されていません: {out_path.name}")
        if out_path.stat().st_size <= 0:
            raise FileopsError(f"連結動画のサイズが0です: {out_path.name}")
        partial = partial_path(out_path)
        if partial.exists():
            raise FileopsError(
                f"連結の一時ファイルが残っています: {partial.name}"
            )

    def _cleanup_failed(self, out_path: Path | None, *, keep_final: bool) -> None:
        """失敗時に正式名も孤児 partial も残さない（P4契約 §4）。

        `keep_final=True`（＝開始前から正式ファイルが在った）のときだけ正式名を残す。
        過去の成功で作られた完成動画を、今回の失敗で消してしまわないため。
        """
        if out_path is None:
            return
        targets = [partial_path(out_path)]
        if not keep_final:
            targets.append(out_path)
        for path in targets:
            try:
                if path.exists():
                    path.unlink()
                    log.info("失敗した連結の残骸を削除しました: %s", path.name)
            except OSError as e:  # pragma: no cover - 実行環境依存
                log.warning("残骸を削除できませんでした: %s（%s）", path.name, e)

    # ------------------------------------------------------------ 重複フレーム

    def _plan_dedupe(
        self,
        chain: list[HistoryRecord],
        inputs: list[Path],
        warnings: list[str],
    ) -> set[int]:
        """重複1フレーム除去の対象境界を決める（§10.6.1。**比較してからのみ除去**）。

        config `concat.dedupe_boundary_frame` が False なら何もしない（既定 OFF）。
        比較に失敗した境界は必ず「除去しない」側へフォールバックする。
        """
        if not getattr(self._cfg, "dedupe_boundary_frame", False):
            return set()

        max_mean = float(getattr(self._cfg, "dedupe_max_mean_diff", 1.0))
        max_max = float(getattr(self._cfg, "dedupe_max_max_diff", 16.0))
        tmp_dir = Path(getattr(self._cfg, "tmp_dir"))
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            warnings.append(f"重複フレーム判定を省略しました（作業領域を作れません: {e}）")
            return set()

        trim: set[int] = set()
        for i in range(1, len(chain)):
            parent = chain[i - 1]
            child = chain[i]
            parent_png = self._history.to_absolute(parent.last_frame_path)
            if parent_png is None or not parent_png.is_file():
                warnings.append(
                    f"重複フレーム判定を省略しました（{parent.id} の最終フレーム画像がありません）"
                )
                continue
            child_png = tmp_dir / f"dedupe_{child.id}_first.png"
            try:
                self._runner.extract_frame(inputs[i], 0, child_png)
                diff = self._runner.compare(parent_png, child_png)
            except (FfmpegError, FileopsError, OSError) as e:
                warnings.append(
                    f"重複フレーム判定に失敗したため通常連結します（{child.id}: {e}）"
                )
                continue
            finally:
                try:
                    child_png.unlink(missing_ok=True)
                    partial_path(child_png).unlink(missing_ok=True)
                except OSError:  # pragma: no cover - 実行環境依存
                    pass

            if diff.matches(max_mean, max_max):
                trim.add(i)
                warnings.append(
                    f"境界 {parent.id} → {child.id}: 重複フレームとして除去を試みます"
                    f"（{diff.describe()}）。"
                    "※音声の長さに合わせて ffmpeg が埋め戻すため、実際に減る枚数は"
                    "依頼した枚数より少なくなります"
                )
            else:
                warnings.append(
                    f"境界 {parent.id} → {child.id}: 一致しないため除去しません"
                    f"（{diff.describe()}）"
                )
        return trim
