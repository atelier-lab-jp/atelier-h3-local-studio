"""1080p高品質化の実行サービス（P6・設計書 §26）。

**AIアップスケールであって、H3 によるネイティブ1080p生成ではない。**
既存の 576×320 の成果物を realesr-animevideov3（x4）で高品質化し、
1920×1080 の**別ファイル**として保存する。元動画は読むだけで変更しない。

責務:
  - 一度に1件だけ実行する（生成・連結・ゴミ箱とも相互排他。§26.4）
  - 開始時に入力を1回だけ実測し、**実測フレーム数**をワーカーの expected_frames・
    進捗の分母・出力検証で一貫して使う（§26.5）。台帳・履歴の名目値は検証基準に
    しない — 連結成果物は §10.6.2 のとおり名目合計より多くて正常なため
  - ワーカーを subprocess で一発起動し、進捗を拾う（`shell=False`）
  - 映像だけの一時ファイルへ書かせ、**音声は元動画から stream copy** で足す
  - 検証に通ったものだけ `os.replace()` で正式名へ昇格する
  - 失敗・取消では正式ファイルも中間ファイルも残さない

**永続化を持たない**（P5.3-B と同じ簡易方式）。どの動画に高品質版があるかは
「決まった名前のファイルが在るかどうか」だけで決まる。台帳も履歴の追記もしない。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from app.core import ffmpeg_ops as fo
from app.core.fileops import FileopsError, partial_path
from app.core.ffmpeg_ops import FfmpegError

log = logging.getLogger("atelier.upscale")

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

BUSY_STATES = frozenset({STATE_RUNNING})

STATE_LABELS_JA = {
    STATE_IDLE: "待機中",
    STATE_RUNNING: "高品質化中",
    STATE_SUCCEEDED: "完成",
    STATE_FAILED: "失敗",
    STATE_CANCELLED: "中止しました",
}

#: 出力の固定仕様（ワーカーと合わせる）
OUT_WIDTH = 1920
OUT_HEIGHT = 1080

#: 進捗行・結果行の接頭辞（ワーカーと合わせる）
PROGRESS_PREFIX = "@@PROGRESS "
RESULT_PREFIX = "@@RESULT "

#: 実測 15 秒程度（124フレーム）だが、長い連結動画も通せるよう余裕を持たせる
DEFAULT_TIMEOUT_SEC = 3600


class UpscaleError(Exception):
    """高品質化を受け付けられない・実行できない（日本語メッセージ）。"""


@dataclass(frozen=True)
class UpscaleStatus:
    """UI が Timer で読む不変スナップショット。"""

    state: str = STATE_IDLE
    key: str | None = None
    source_key: str | None = None
    source_label: str = ""
    message: str = ""
    frame: int = 0
    total: int = 0
    output_path: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def state_label(self) -> str:
        return STATE_LABELS_JA.get(self.state, self.state)

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        return max(0, min(100, round(self.frame * 100 / self.total)))


@dataclass(frozen=True)
class UpscaleRequest:
    """AppService が組み立てて渡す「何を高品質化するか」（パスは解決済み）。

    `num_frames` は台帳・履歴の**名目値**で、実測が取れるまでの表示にだけ使う。
    検証の基準は開始時に計測する入力の実測フレーム数（§26.5）。連結成果物は
    §10.6.2 のとおり名目合計より多いことが正常で、チェーン連結の行では
    終端クリップ単体の値しか入らないため、名目値との一致は要求できない。
    """

    source_key: str
    source_path: Path
    output_path: Path
    num_frames: int
    fps: int
    label: str = ""


class UpscaleService:
    """1080p高品質化を1件ずつ実行する。UI からはこのオブジェクトだけを触る。"""

    def __init__(
        self,
        cfg,
        *,
        worker_python: Path | None = None,
        worker_script: Path | None = None,
        weights_path: Path | None = None,
        ffmpeg_path: str = "",
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._cfg = cfg
        self._worker_python = Path(
            worker_python or getattr(getattr(cfg, "backend", None), "worker_python", "")
        )
        self._worker_script = Path(
            worker_script
            or (Path(getattr(cfg, "project_root", ".")) / "app/postprocess/upscale_worker.py")
        )
        self._weights = Path(
            weights_path
            or (Path(getattr(cfg, "project_root", ".")) / "app/assets/upscale/realesr-animevideov3.pth")
        )
        self._ffmpeg_configured = ffmpeg_path or getattr(cfg, "ffmpeg_path", "")
        self._ffmpeg: str | None = None
        self._timeout = timeout_sec

        self._lock = threading.RLock()
        self._status = UpscaleStatus()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._cancelled = False
        self._seq = 0
        self._closed = False

    # ------------------------------------------------------------ 事前確認

    @property
    def weights_path(self) -> Path:
        return self._weights

    def availability(self) -> tuple[bool, str]:
        """使える状態か（使えるか, 日本語の理由）。`--check` と UI の両方で使う。"""
        if not self._worker_script.is_file():
            return False, f"高品質化のプログラムが見つかりません: {self._worker_script.name}"
        if not self._worker_python.is_file():
            return False, (
                "高品質化に使う Python が見つかりません"
                "（config の [backends.minimax_h3] worker_python をご確認ください）"
            )
        if not self._weights.is_file():
            return False, (
                "高品質化のモデルファイルがありません。"
                f"`./scripts/setup.sh --with-upscale` を実行すると取得できます"
                f"（配置先: app/assets/upscale/{self._weights.name}）"
            )
        return True, "利用できます"

    # ------------------------------------------------------------ 公開API

    def start_upscale(self, request: UpscaleRequest) -> str:
        """バックグラウンドで高品質化を始める（呼び出しはブロックしない）。"""
        available, reason = self.availability()
        if not available:
            raise UpscaleError(reason)

        with self._lock:
            if self._closed:
                raise UpscaleError("アプリを終了中のため高品質化を開始できません")
            if self._status.running:
                raise UpscaleError(
                    "高品質化を実行中です。完了までお待ちください"
                    f"（実行中: {self._status.source_label or self._status.source_key}）"
                )
            self._seq += 1
            key = f"upscale-{self._seq}-{request.source_key}"
            self._cancelled = False
            self._status = UpscaleStatus(
                state=STATE_RUNNING,
                key=key,
                source_key=request.source_key,
                source_label=request.label or request.source_key,
                message="高品質化の準備をしています…",
                frame=0,
                # 名目値は仮の分母。入力の実測が取れ次第 _run() が上書きする
                total=request.num_frames,
                started_at=datetime.now().astimezone(),
            )
            thread = threading.Thread(
                target=self._run,
                args=(key, request),
                name=f"atelier-upscale-{self._seq}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return key

    def status(self) -> UpscaleStatus:
        with self._lock:
            return self._status

    def cancel(self) -> str:
        """実行中のワーカーを止める（日本語メッセージを返す）。"""
        with self._lock:
            if not self._status.running:
                return "高品質化は実行されていません。"
            self._cancelled = True
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:  # pragma: no cover - 実行環境依存
                log.warning("高品質化ワーカーを終了できませんでした", exc_info=True)
        return "高品質化を中止しています…"

    def shutdown(self, timeout: float = 5.0) -> None:
        """新規受付を止め、実行中があれば終了させてから待つ。"""
        with self._lock:
            self._closed = True
            self._cancelled = True
            process = self._process
            thread = self._thread
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:  # pragma: no cover
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - 実行環境依存
                log.warning("高品質化スレッドが %.1f 秒で終わりませんでした", timeout)

    # ------------------------------------------------------------ 内部

    @property
    def ffmpeg(self) -> str:
        if self._ffmpeg is None:
            self._ffmpeg = fo.resolve_ffmpeg(self._ffmpeg_configured)
        return self._ffmpeg

    def _set(self, key: str, **changes) -> None:
        """自分の実行キーのときだけ状態を更新する（古いスレッドの上書きを防ぐ）。"""
        with self._lock:
            if self._status.key != key:
                return
            self._status = replace(self._status, **changes)

    def _run(self, key: str, request: UpscaleRequest) -> None:
        out_path = request.output_path
        video_only = out_path.with_name(f".{out_path.name}.video.mp4")
        partial = partial_path(out_path)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            for stale in (video_only, partial):
                stale.unlink(missing_ok=True)

            # 検証基準は入力の**実測フレーム数**（§26.5）。ここで1回だけ計測し、
            # ワーカーの expected_frames・進捗の分母・出力検証で同じ値を使う。
            # 計測できなければ名目値へフォールバックせず、ワーカーを起動する前に
            # 日本語エラーで中止する（_probe_source が保証する）。
            self._set(key, message="元の動画を確認しています…")
            source_probe = self._probe_source(request)
            expected_frames = source_probe.frames
            if self._is_cancelled():
                raise UpscaleError("高品質化を中止しました。")

            self._set(
                key,
                total=expected_frames,
                message="動画を1コマずつ高品質化しています…",
            )
            self._run_worker(key, request, video_only, expected_frames)
            if self._is_cancelled():
                raise UpscaleError("高品質化を中止しました。")

            self._set(key, message="音声を付けています…")
            self._mux_audio(request, video_only, partial, source_probe)

            self._set(key, message="仕上がりを確認しています…")
            self._verify_and_promote(request, partial, out_path, source_probe)

            self._set(
                key,
                state=STATE_SUCCEEDED,
                output_path=out_path,
                frame=expected_frames,
                finished_at=datetime.now().astimezone(),
                message=f"1080p高品質版を作成しました（{out_path.name}）",
                error=None,
            )
            log.info("高品質化が完了しました: %s", out_path.name)
        except BaseException as e:  # noqa: BLE001 - スレッド内で握りつぶさない
            cancelled = self._is_cancelled()
            message = self._error_message(e, cancelled=cancelled)
            self._set(
                key,
                state=STATE_CANCELLED if cancelled else STATE_FAILED,
                output_path=None,
                finished_at=datetime.now().astimezone(),
                message=message,
                error=None if cancelled else message,
            )
            if cancelled:
                log.info("高品質化を中止しました: %s", request.source_key)
            else:
                log.warning("高品質化に失敗しました（%s）: %s", request.source_key, message)
        finally:
            self._cleanup(video_only, partial)
            with self._lock:
                self._process = None

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _probe_source(self, request: UpscaleRequest) -> fo.ProbeResult:
        """入力を1回だけフルデコードして実測値を得る（検証の基準。§26.5）。

        `decode_probe()`（`-f null -`）は fps 変換や同期処理を挟まないため、
        計数はワーカーが PyAV の `container.decode(video=0)` で読む映像
        ストリームの枚数と同じ意味の値になる。計測できない場合は名目値へ
        **黙ってフォールバックせず**、ここで日本語エラーにして中止する。
        """
        name = request.source_path.name
        try:
            probe = fo.decode_probe(self.ffmpeg, request.source_path)
        except (FfmpegError, OSError) as e:
            raise UpscaleError(f"元の動画を読み取れませんでした（{name}: {e}）") from e
        if not probe.has_video:
            raise UpscaleError(f"元の動画に映像が入っていません: {name}")
        if probe.frames is None or probe.frames <= 0:
            raise UpscaleError(f"元の動画のフレーム数を計測できませんでした: {name}")
        if probe.duration_sec is None:
            raise UpscaleError(f"元の動画の再生時間を計測できませんでした: {name}")
        if request.fps > 0:
            # duration_sec は音声を含むコンテナ全体（最長ストリーム）の実測。
            # V1 の成果物は音声が AAC の量子化ぶんだけ映像より長いのが正常で、
            # その差は連結でも 0.1 秒未満に収まる。±0.5 秒はそれを許容しつつ、
            # fps の違う動画（実測枚数÷fps が秒数と合わない）を開始前に弾く。
            expected_sec = probe.frames / request.fps
            if abs(probe.duration_sec - expected_sec) > fo.DURATION_TOLERANCE_SEC:
                raise UpscaleError(
                    f"元の動画が {request.fps}fps ではないようです: {name}"
                    f"（実測 {probe.frames}フレーム・{probe.duration_sec:.2f}s / "
                    f"{request.fps}fps なら {expected_sec:.2f}s）"
                )
        return probe

    def _run_worker(
        self, key: str, request: UpscaleRequest, video_only: Path, expected_frames: int
    ) -> None:
        """ワーカーを一発起動して進捗を拾う（**引数配列・`shell=False`**）。

        `expected_frames` は入力の実測値。ワーカー自身も読み取った枚数と
        突き合わせるので、デコーダ間で計数がずれた場合も出力前に検出できる。
        """
        args = [
            str(self._worker_python),
            str(self._worker_script),
            "--source", str(request.source_path),
            "--destination", str(video_only),
            "--weights", str(self._weights),
            "--expected-frames", str(expected_frames),
        ]
        env = dict(os.environ)
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",   # DiffSynth 側に __pycache__ を作らない
                "PYTHONUNBUFFERED": "1",          # 進捗を即座に受け取る
                "PYTORCH_ENABLE_MPS_FALLBACK": "1",
            }
        )
        try:
            process = subprocess.Popen(  # noqa: S603 - 引数配列・固定スクリプトのみ
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except OSError as e:
            raise UpscaleError(f"高品質化を開始できませんでした（{e}）") from e

        with self._lock:
            self._process = process

        result: dict | None = None
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if line.startswith(PROGRESS_PREFIX):
                payload = self._parse(line[len(PROGRESS_PREFIX):])
                if payload is not None:
                    frame = payload.get("frame")
                    if isinstance(frame, int) and frame >= 0:
                        self._set(key, frame=frame)
                continue
            if line.startswith(RESULT_PREFIX):
                result = self._parse(line[len(RESULT_PREFIX):])
                continue
            if line.strip():
                log.debug("高品質化ワーカー: %s", line.strip())

        stderr = (process.stderr.read() if process.stderr else "") or ""
        code = process.wait()

        if self._is_cancelled():
            raise UpscaleError("高品質化を中止しました。")
        if code != 0 or not (result and result.get("ok")):
            detail = (result or {}).get("error") if isinstance(result, dict) else None
            if not detail:
                tail = [ln for ln in stderr.strip().splitlines() if ln.strip()]
                detail = tail[-1] if tail else f"終了コード {code}"
            raise UpscaleError(f"高品質化に失敗しました（{detail}）")
        if not video_only.is_file() or video_only.stat().st_size <= 0:
            raise UpscaleError("高品質化した映像が作られませんでした")

    @staticmethod
    def _parse(text: str) -> dict | None:
        """壊れた行が来ても落ちない（進捗は表示だけの情報なので無視して進む）。"""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _mux_audio(
        self,
        request: UpscaleRequest,
        video_only: Path,
        partial: Path,
        source_probe: fo.ProbeResult,
    ) -> None:
        """元動画の音声を**再エンコードせず**に足す（音声が無ければ映像だけ）。

        `source_probe` は `_probe_source()` の結果。同じジョブの中で使い回し、
        入力を余分にデコードし直さない（ジョブをまたぐキャッシュは持たない）。
        """
        args = [
            self.ffmpeg, "-y", "-nostdin",
            "-i", str(video_only),
            "-i", str(request.source_path),
            "-map", "0:v:0",
        ]
        if source_probe.has_audio:
            # 複数の音声があればすべて拾う。`-shortest` は使わない（末尾を切らない）
            args += ["-map", "1:a?", "-c:a", "copy"]
        args += ["-c:v", "copy", "-movflags", "+faststart", "-f", "mp4", str(partial)]
        try:
            fo._run(args, timeout=self._timeout)
        except FfmpegError as e:
            raise UpscaleError(f"音声を付けられませんでした（{e}）") from e

    def _verify_and_promote(
        self,
        request: UpscaleRequest,
        partial: Path,
        out_path: Path,
        source_probe: fo.ProbeResult,
    ) -> None:
        """1920×1080・フレーム数・音声の有無を確かめてから正式名へ昇格する。

        フレーム数と再生時間の基準は**入力の実測値**（`source_probe`）。
        「高品質化が入力のフレームを増減させていない」ことを厳密一致で保証する。
        台帳の名目値と比べないのは、§10.6.2 が連結成果物の超過を正当と
        定めているため（名目値と比べると正常な連結動画で必ず失敗する）。
        """
        from app.core.fileops import promote

        expected_frames = source_probe.frames
        expected_duration = (
            expected_frames / request.fps if request.fps else None
        )

        def validate(path: Path) -> None:
            probe = fo.decode_probe(self.ffmpeg, path)
            if not probe.has_video:
                raise FileopsError(f"映像を読み取れません: {path.name}")
            if f"{OUT_WIDTH}x{OUT_HEIGHT}" not in probe.video_desc:
                raise FileopsError(
                    f"解像度が {OUT_WIDTH}×{OUT_HEIGHT} になっていません: {path.name}"
                )
            if probe.frames is None or probe.frames != expected_frames:
                raise FileopsError(
                    f"フレーム数が変わっています: {path.name}"
                    f"（実測 {probe.frames} / 元 {expected_frames}）"
                )
            if source_probe.has_audio and not probe.has_audio:
                raise FileopsError(f"音声が失われています: {path.name}")
            if expected_duration is not None:
                if probe.duration_sec is None:
                    raise FileopsError(f"再生時間を確認できません: {path.name}")
                if abs(probe.duration_sec - expected_duration) > fo.DURATION_TOLERANCE_SEC:
                    raise FileopsError(
                        f"再生時間が元と違います: {path.name}"
                        f"（実測 {probe.duration_sec:.2f}s / 元 {expected_duration:.2f}s）"
                    )

        promote(partial, out_path, (validate,))

    @staticmethod
    def _error_message(e: BaseException, *, cancelled: bool) -> str:
        if cancelled:
            return "高品質化を中止しました。"
        text = str(e).strip()
        if isinstance(e, (UpscaleError, FfmpegError, FileopsError)):
            return text or "高品質化に失敗しました"
        detail = f"{type(e).__name__}: {text}" if text else type(e).__name__
        return f"高品質化に失敗しました（{detail}）"

    @staticmethod
    def _cleanup(*paths: Path) -> None:
        """中間ファイルを残さない（正式名は promote が成功したときだけ作られる）。"""
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 実行環境依存
                log.warning("一時ファイルを削除できませんでした: %s", path)
