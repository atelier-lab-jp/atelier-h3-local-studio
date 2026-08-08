"""全チェーン連結の実行サービス（設計書 §10.6・§10.6.1・§10.7、P4契約 §4・§5）。

責務:
  - 選択された子動画から `HistoryStore.resolve_concat_chain()` でチェーンを解決する
  - （config で有効な場合のみ）連結境界の重複1フレームを**比較してから**除去する
  - `ffmpeg_ops.concat_reencode()` で partial へ書き出し、検証して原子的に昇格する
    （**V1 の連結方式はこれ1つだけ**。`-c copy` は config から到達できない。P5）
  - 昇格後にのみ履歴へ `concat_path` / `concat_sources` を記録する

守る不変条件:
  - **連結は同時に1つだけ**（`ffmpeg_ops._lock` に加えサービス層でも排他）
  - **Gradio コールバックをブロックしない**（daemon スレッドで実行し UI は status() を読む）
  - 失敗時に正式名も孤児 partial も残さない（P4契約 §4）
  - **source 動画を一切変更しない**（ffmpeg は入力を読むだけ。出力は必ず新規ファイル）
"""

from __future__ import annotations

import logging
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

    @property
    def running(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def state_label(self) -> str:
        return STATE_LABELS_JA.get(self.state, self.state)


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
    ) -> None:
        self._cfg = cfg
        self._history = history
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
            if self._closed:
                raise ConcatError("アプリを終了中のため連結を開始できません")
            if self._status.running:
                raise ConcatError(
                    "連結を実行中です。完了までお待ちください"
                    f"（実行中: {self._status.job_id}）"
                )
            self._seq += 1
            key = f"concat-{self._seq}-{job_id}"
            self._status = ConcatStatus(
                state=STATE_RESOLVING,
                key=key,
                job_id=job_id,
                message="チェーンを確認しています…",
                started_at=datetime.now().astimezone(),
            )
            thread = threading.Thread(
                target=self._run,
                args=(key, job_id),
                name=f"atelier-concat-{self._seq}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return key

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

    # ------------------------------------------------------------ 補助

    @staticmethod
    def _error_message(e: BaseException) -> str:
        """UI へ出す日本語メッセージ（既知の例外は本文をそのまま使う）。"""
        text = str(e).strip()
        if isinstance(e, (HistoryError, FfmpegError, FileopsError, ConcatError)):
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
