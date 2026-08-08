"""MockEngine: 実機と同形のイベントを発行する Execution Engine の別実装（設計書 §16）。

- UIプロセス内のデーモンスレッドで動き、別プロセスは使わない（決定D12）。
- イベント順序・フィールドは実機（付録A）と同一:
  STAGE(loading_model) → STAGE(loading_lora) → READY
  → ジョブごとに STAGE(preparing) → PROGRESS(1..N) → STAGE(saving) → DONE / ERROR
- ペースは `[estimates]` の目安秒 ÷ `[mock] speed_factor`。待機は注入可能な
  `sleep_fn` で行うため、テストでは `sleep_fn=lambda s: None` で実時間ゼロで完走できる。
- 成果物は `app/assets/mock/` の実素材をコピーし、§10.7 の
  「partial 書き込み → 検証 → os.replace() 昇格」を経て正式名にする。
  **DONE は MP4・PNG 両方の昇格が完了した後にのみ発行する**。
- 継続生成（P4・§10.2）も同じ契約で扱う: `keyframe_path` を投入時に検証し
  （RealEngine と同一の日本語文言）、生成中に画像として開けるかを再確認する。
  無効なら ERROR(fatal=False, category=input)。**成果物の形は単発生成と同じ**。
- `restart()` は RealEngine と同一契約（§13.3・P3）: 実行中ジョブへ
  ERROR(fatal, worker_dead) を**先に**出してから再初期化し、
  STAGE(loading_model) → STAGE(loading_lora) → READY を再送する。
  イベントキューは作り直さない（中断 ERROR を確実に届けるため）。
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from app.core import ffmpeg_ops, fileops
from app.core.contracts import (
    MINIMAX_H3_CAPABILITIES,
    MOCK_FAIL_PREFIX,
    BackendIdentity,
    Capabilities,
    EngineBusyError,
    EngineEvent,
    EngineState,
    ErrorCategory,
    EventType,
    JobSpec,
    JobStage,
    ValidationError,
    resolve_seed,
    validate_job_spec,
)
from app.engine.base import backend_identity

log = logging.getLogger("atelier.engine.mock")

INIT_THREAD_NAME = "mock-engine-init"
JOB_THREAD_NAME = "mock-engine-job"

#: config `[estimates]` が欠けている場合の既定値（§12.2 と同じ値）
DEFAULT_ESTIMATES: dict[str, float] = {
    "init_sec": 300.0,
    "f56_s4_sec": 390.0,
    "f124_s4_sec": 810.0,
    "step8_factor": 2.0,
}

# 待機時間の配分（合計1.0）。実機の体感（初期化はモデル読込が大半、
# 生成は step が大半、保存は短い）に合わせた表示上の目安。
INIT_MODEL_RATIO = 0.7
INIT_LORA_RATIO = 0.3
PREPARING_RATIO = 0.08
GENERATING_RATIO = 0.84
SAVING_RATIO = 0.08

#: 待機を刻む単位（shutdown 要求へ即応するため。ビジーウェイトではない）
SLEEP_SLICE_SEC = 0.1

#: restart() が内部停止に使う待ち上限（秒）
RESTART_TIMEOUT_SEC = 5.0

#: 再起動で中断されたジョブへ返すメッセージ（RealEngine と同一。上位層は real/mock を区別しない）
RESTART_INTERRUPT_MESSAGE = "生成ワーカーの再起動により生成を中断しました"
RESTART_INTERRUPT_DETAIL = "このジョブは自動再実行されません"

#: 継続生成のキーフレーム検証メッセージ（RealEngine と同一文言。real/mock で拒否理由を揃える）
KEYFRAME_LABEL = "継続元のキーフレーム画像"
KEYFRAME_NOT_ABSOLUTE = KEYFRAME_LABEL + "は絶対パスで指定してください"
KEYFRAME_OUTSIDE_ROOT = KEYFRAME_LABEL + "がアプリのデータ領域の外です"
KEYFRAME_NOT_FOUND = KEYFRAME_LABEL + "が見つかりません"
#: ワーカー相当の再検証（画像として開けるか）に失敗したときのメッセージ
KEYFRAME_UNREADABLE = KEYFRAME_LABEL + "を開けませんでした"


def _one_line(e: BaseException) -> str:
    text = str(e).strip() or e.__class__.__name__
    return " ".join(text.split())


def _keyframe_problem(path: Path) -> str | None:
    """キーフレーム画像の不備を日本語で返す（問題なければ None）。

    実機ワーカー（`h3_worker.open_keyframe_image`）と**同じ条件・同じ文言**で判定する。
    ここを実機より甘くすると「モックでは通るのに実機で失敗する」乖離が生まれ、
    モックでの動作確認が実機の失敗を予見できなくなる（設計書 §16.2）。
    """
    from app.core.config import FIXED_HEIGHT, FIXED_WIDTH

    try:
        from PIL import Image

        with Image.open(str(path)) as image:
            image.load()  # 壊れた PNG は load() で初めて失敗することがある
            image_format = image.format
            size = tuple(image.size)
    except Exception as e:  # noqa: BLE001 - 破損・非画像・権限はすべて input 扱い
        # 実機ワーカーと同じ書式にそろえる（`…を開けませんでした（名前）: 種類: 詳細`）
        return (
            f"{KEYFRAME_UNREADABLE}（{path.name}）: "
            f"{type(e).__name__}: {_one_line(e)}"
        )

    if image_format != "PNG":
        return (
            f"{KEYFRAME_LABEL}が PNG ではありません"
            f"（{path.name}: {image_format or '不明な形式'}）"
        )
    if size != (FIXED_WIDTH, FIXED_HEIGHT):
        return (
            f"{KEYFRAME_LABEL}の大きさが違います"
            f"（{path.name}: {size[0]}×{size[1]}）。"
            f"{FIXED_WIDTH}×{FIXED_HEIGHT} の画像が必要です"
        )
    return None


def validate_keyframe(data_root: Path, keyframe_path: Path) -> Path:
    """継続生成のキーフレームを**投入時に**検証する（契約 §2・設計書 §10.2）。

    **`app/engine/real_engine.py` の同名関数と同一の判定・同一の日本語文言**にすること
    （上位層から見た real / mock の契約を完全に一致させる。§16.2）。
    実機では「エンジン層で経路と実在を確かめ、画像として開けるかはワーカーが確かめる」
    という2段構えになっており、モックも `_run_job` で後段を再現する。
    """
    path = Path(keyframe_path)
    if not path.is_absolute():
        raise ValidationError(f"{KEYFRAME_NOT_ABSOLUTE}: {path}")
    try:
        resolved = fileops.ensure_within(data_root, path)
    except fileops.FileopsError as e:
        raise ValidationError(f"{KEYFRAME_OUTSIDE_ROOT}: {path}") from e
    if not resolved.is_file():
        raise ValidationError(f"{KEYFRAME_NOT_FOUND}: {path}")
    return path


class MockEngine:
    """`app.engine.base.Engine` 契約を満たすモック実装（設計書 §16.2）。

    スレッド構成:
    - 初期化スレッド（`start()` で1本）と生成スレッド（`submit()` ごとに1本）。
      いずれも daemon。同時に走る生成は常に1本のみ（READY 以外の submit は拒否）。
    - 状態は `RLock` で保護し、イベントは `queue.Queue` で単一消費者へ渡す。
    - `restart()` は世代を作り直す。旧世代のスレッドには起動時の `stop` イベントを
      引き渡してあるため、新世代の状態やイベントへ干渉しない（§13.3）。

    終端イベントの単一性:
    - 実行中ジョブは `_current_job` が1つだけ保持し、DONE / ERROR を出す側が
      `_take_current_job()` で取り出してから発行する。restart() が先に取り出して
      中断 ERROR を出した場合、生成スレッドは自分の終端イベントを発行しない。
    """

    def __init__(
        self,
        *,
        identity: BackendIdentity,
        assets_dir: Path,
        data_root: Path,
        capabilities: Capabilities = MINIMAX_H3_CAPABILITIES,
        ffmpeg_path: str = "",
        speed_factor: float = 40.0,
        estimates: dict | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng=None,
    ) -> None:
        self._identity = identity
        self._capabilities = capabilities
        self._assets_dir = Path(assets_dir)
        self._data_root = Path(data_root)
        self._ffmpeg_path = ffmpeg_path
        if speed_factor <= 0:
            log.warning(
                "mock.speed_factor が不正です（%s）。等倍として扱います", speed_factor
            )
            speed_factor = 1.0
        self._speed_factor = float(speed_factor)
        self._estimates: dict[str, float] = dict(DEFAULT_ESTIMATES)
        for key, value in (estimates or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._estimates[key] = float(value)
        self._sleep_fn = sleep_fn
        self._rng = rng

        self._lock = threading.RLock()
        #: restart() を直列化する専用ロック（join 中に `_lock` を保持しないため別にする）
        self._restart_lock = threading.Lock()
        # None はシャットダウン時にブロック中の poll_event を起こすための番兵
        self._events: queue.Queue[EngineEvent | None] = queue.Queue()
        self._state = EngineState.STARTING
        self._stop = threading.Event()
        self._closed = False
        #: 利用者が shutdown() を呼んだか（restart() 内部の一時停止と区別する。一度立てたら戻さない）
        self._terminated = False
        self._init_thread: threading.Thread | None = None
        self._job_thread: threading.Thread | None = None
        #: 実行中ジョブ（RealEngine の `_current_job` と同じ役割。終端イベントの二重発行を防ぐ）
        self._current_job: JobSpec | None = None
        self._ffmpeg: str | None = None

    # ------------------------------------------------------------ 生成

    @classmethod
    def from_config(cls, cfg, *, sleep_fn=time.sleep, rng=None) -> MockEngine:
        """AppConfig から生成する（identity は `[backends.<id>]` から作る。§22.2）。"""
        return cls(
            identity=backend_identity(cfg.backend),
            assets_dir=cfg.assets_mock_dir,
            data_root=cfg.data_root,
            ffmpeg_path=cfg.ffmpeg_path,
            speed_factor=cfg.mock_speed_factor,
            estimates=cfg.estimates,
            sleep_fn=sleep_fn,
            rng=rng,
        )

    # ------------------------------------------------------------ 契約

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def state(self) -> EngineState:
        with self._lock:
            return self._state

    def start(self) -> None:
        with self._lock:
            if self._closed or self._terminated:
                raise EngineBusyError(
                    "停止済みのエンジンは start() できません"
                    "（新しいエンジンを作り直してください）"
                )
            if self._init_thread is not None and self._init_thread.is_alive():
                return
            if self._state in (
                EngineState.INITIALIZING_MODEL,
                EngineState.INITIALIZING_LORA,
                EngineState.READY,
                EngineState.BUSY,
            ):
                return  # 二重呼び出しは無視する
            self._state = EngineState.STARTING
            # stop はスレッド起動時のものを引き渡す（restart() が差し替えても
            # 旧世代のスレッドが新しい stop を見て走り続けないようにする）
            stop = self._stop
            thread = threading.Thread(
                target=self._run_init, args=(stop,), name=INIT_THREAD_NAME, daemon=True
            )
            self._init_thread = thread
        thread.start()

    def submit(self, spec: JobSpec) -> None:
        # UI を迂回した不正値を下位層でも止める（設計書 §15・CLAUDE.md）
        validate_job_spec(spec, data_root=self._data_root)
        # backend_id 不一致は ValidationError で返す。
        # 理由: MockEngine はプロセス内呼び出しなので、submit の同期例外で
        # 呼び出し元へ返すのが最短かつ検証経路を1本化できる（validate_job_spec と同じ扱い）。
        # プロセス境界を越える RealEngine（P2）は付録Aどおり
        # fatal=false / category=input の ERROR イベントで返す。
        if spec.backend_id != self._identity.backend_id:
            raise ValidationError(
                f"このエンジンでは扱えない生成バックエンドです"
                f"（指定: {spec.backend_id}、このエンジン: {self._identity.backend_id}）"
            )
        # 継続生成（P4）: RealEngine と同じ検証・同じ拒否理由。
        # 単発生成（keyframe_path=None）はここを素通りする。
        if spec.keyframe_path is not None:
            validate_keyframe(self._data_root, spec.keyframe_path)

        with self._lock:
            if (
                self._closed
                or self._terminated
                or self._state in (EngineState.DEAD, EngineState.HALTED)
            ):
                raise EngineBusyError("エンジンが停止しています")
            if self._state is not EngineState.READY:
                raise EngineBusyError(
                    f"エンジンが生成を受け付けられる状態ではありません"
                    f"（現在: {self._state.value}）"
                )
            self._state = EngineState.BUSY
            self._current_job = spec
            stop = self._stop
            thread = threading.Thread(
                target=self._run_job,
                args=(spec, stop),
                name=JOB_THREAD_NAME,
                daemon=True,
            )
            self._job_thread = thread
        thread.start()

    def poll_event(self, timeout: float | None = None) -> EngineEvent | None:
        if timeout is not None and timeout < 0:
            timeout = 0.0
        try:
            return self._events.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self, timeout: float = 5.0) -> None:
        """エンジンを恒久的に停止する（二重呼び出し安全・デッドロックしない）。

        `_terminated` を立てるのはここだけで、以後 `start()` / `restart()` は拒否される。
        """
        with self._lock:
            self._terminated = True
        self._teardown(timeout=timeout, wake_pollers=True)

    def _teardown(self, *, timeout: float, wake_pollers: bool) -> None:
        """内部スレッドを止める（shutdown と restart で共有する停止処理）。

        `wake_pollers=False` は restart 経由の一時停止で使う。番兵 `None` は
        「エンジンが終わった」の合図（`base.Engine` の契約）なので、
        再初期化して続く再起動では流さない。
        """
        with self._lock:
            already_closed = self._closed
            self._closed = True
            self._state = EngineState.HALTED
            stop = self._stop
            threads = [
                t for t in (self._init_thread, self._job_thread) if t is not None
            ]
        # ロックを保持したまま join しない（生成スレッドが状態更新でロックを取るため）
        stop.set()
        if wake_pollers and not already_closed:
            self._events.put(None)  # ブロック中の poll_event を起こす
        deadline = time.monotonic() + max(float(timeout), 0.0)
        current = threading.current_thread()
        for thread in threads:
            if thread is current or not thread.is_alive():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():  # pragma: no cover - 通常は起きない
                log.warning("モックエンジンのスレッドが停止しません: %s", thread.name)

    def restart(self) -> None:
        """再初期化する（設計書 §13.3 の fatal 後再起動。RealEngine と同一契約）。

        1. 実行中ジョブがあれば、**停止する前に**
           ERROR(fatal=True, category=worker_dead) を発行する。無言で終端イベントを
           失わせると、ディスパッチャが DONE / ERROR を待ち続けてキューが永久停止する。
        2. 内部スレッドを止めて再初期化し、STAGE(loading_model) →
           STAGE(loading_lora) → READY を改めて発行する。
        3. **イベントキューは作り直さない。** 1 の中断 ERROR と未消費のイベントを
           確実に上位層へ届けるため（P1 実装はここで作り直しており、それが
           「実行中ジョブが終端イベントなしで消える」問題の一因だった）。
        4. `shutdown()` 済みなら `EngineBusyError`。no-op にしない理由は、
           停止済みエンジンの復活を許すと RealEngine では終了処理と競合して
           ワーカープロセスが孤児として残りうるため。real / mock で挙動を揃え、
           呼び出し側の不具合を握り潰さない（復活はインスタンス作り直しで行う）。
        5. `_restart_lock` で直列化するため二重呼び出し・並行呼び出しでも安全。
           join はロック外の `_teardown` が行うのでデッドロックしない。
        """
        with self._restart_lock:
            if self._terminated:
                raise EngineBusyError(
                    "停止済みのエンジンは restart() できません"
                    "（新しいエンジンを作り直してください）"
                )

            # 停止より先に中断を通知する（イベントキューは作り直さないので確実に届く）
            job = self._take_current_job(None)
            if job is not None:
                log.warning(
                    "再起動のため実行中ジョブを中断します: job=%s", job.job_id
                )
                self._emit(
                    EngineEvent(
                        type=EventType.ERROR,
                        job_id=job.job_id,
                        fatal=True,
                        category=ErrorCategory.WORKER_DEAD,
                        message=RESTART_INTERRUPT_MESSAGE,
                        detail=RESTART_INTERRUPT_DETAIL,
                    )
                )

            self._teardown(timeout=RESTART_TIMEOUT_SEC, wake_pollers=False)

            with self._lock:
                self._stop = threading.Event()
                self._closed = False
                self._state = EngineState.STARTING
                self._init_thread = None
                self._job_thread = None
                self._current_job = None
                self._ffmpeg = None
                terminated = self._terminated
            if terminated:  # 再起動中に shutdown() が入った
                self._teardown(timeout=RESTART_TIMEOUT_SEC, wake_pollers=True)
                return
            self.start()

        if self._terminated:
            # start() と shutdown() が競合した場合の後始末（スレッドを残さない）
            self._teardown(timeout=RESTART_TIMEOUT_SEC, wake_pollers=True)

    # ------------------------------------------------------------ 内部

    def _emit(self, event: EngineEvent) -> None:
        self._events.put(event)

    def _set_state(self, new_state: EngineState) -> None:
        with self._lock:
            if self._closed:
                return
            self._state = new_state

    def _take_current_job(self, job_id: str | None) -> JobSpec | None:
        """実行中ジョブを取り出して確定させる（終端イベントの二重発行を防ぐ）。

        RealEngine の同名メソッドと同じ役割。restart() が先に取り出した場合、
        生成スレッドは自分の DONE / ERROR を発行しない。
        """
        with self._lock:
            spec = self._current_job
            if spec is None:
                return None
            if job_id is not None and spec.job_id != job_id:
                return None
            self._current_job = None
            return spec

    def _sleep(self, seconds: float, stop: threading.Event) -> bool:
        """待機する。停止要求が来たら False を返す（ビジーウェイトにしない）。"""
        remaining = max(float(seconds), 0.0)
        while remaining > 0:
            if stop.is_set():
                return False
            slice_sec = min(SLEEP_SLICE_SEC, remaining)
            self._sleep_fn(slice_sec)
            remaining -= slice_sec
        return not stop.is_set()

    def _init_seconds(self) -> float:
        return self._estimates["init_sec"] / self._speed_factor

    def _job_seconds(self, spec: JobSpec) -> float:
        base = self._estimates.get(
            f"f{spec.num_frames}_s4_sec", self._estimates["f56_s4_sec"]
        )
        if spec.steps == 8:
            base *= self._estimates.get("step8_factor", 2.0)
        return base / self._speed_factor

    def _run_init(self, stop: threading.Event) -> None:
        total = self._init_seconds()
        self._set_state(EngineState.INITIALIZING_MODEL)
        self._emit(EngineEvent(type=EventType.STAGE, stage=JobStage.LOADING_MODEL))
        if not self._sleep(total * INIT_MODEL_RATIO, stop):
            return
        self._set_state(EngineState.INITIALIZING_LORA)
        self._emit(EngineEvent(type=EventType.STAGE, stage=JobStage.LOADING_LORA))
        if not self._sleep(total * INIT_LORA_RATIO, stop):
            return
        self._set_state(EngineState.READY)
        self._emit(
            EngineEvent(
                type=EventType.READY,
                backend_id=self._identity.backend_id,
                capabilities=self._capabilities,
            )
        )
        log.info("モックエンジンが準備完了になりました（%s）", self._identity.backend_id)

    def _run_job(self, spec: JobSpec, stop: threading.Event) -> None:
        started = time.monotonic()
        total = self._job_seconds(spec)
        try:
            self._emit(
                EngineEvent(
                    type=EventType.STAGE,
                    job_id=spec.job_id,
                    stage=JobStage.PREPARING,
                )
            )
            if not self._sleep(total * PREPARING_RATIO, stop):
                return

            # 継続生成（P4）: 実機ワーカーは preparing の直後にキーフレームを
            # PIL で開いて RGB へ変換する。開けなければ非 fatal の input エラーで
            # 報告してワーカーは生き残る。モックも同じ位置・同じ分類で再現する。
            if spec.keyframe_path is not None:
                # 実機ワーカーは PNG 形式と 576×320 ちょうどまで見る（P5 §5.1）。
                # モックがここを見ないと「モックでは通るのに実機で失敗する」乖離が
                # 生まれ、モックでの動作確認が実機の失敗を予見できなくなる。
                problem = _keyframe_problem(spec.keyframe_path)
                if problem is not None:
                    self._emit_error(
                        spec,
                        problem,
                        detail=f"keyframe_path={spec.keyframe_path}",
                    )
                    return

            seed_used = resolve_seed(spec.seed_requested, rng=self._rng)

            per_step = (total * GENERATING_RATIO) / max(spec.steps, 1)
            for step in range(1, spec.steps + 1):
                if not self._sleep(per_step, stop):
                    return
                self._emit(
                    EngineEvent(
                        type=EventType.PROGRESS,
                        job_id=spec.job_id,
                        step=step,
                        total=spec.steps,
                    )
                )

            # 失敗注入（§16.2）。生成の進捗を出し切ってから失敗させ、
            # 「進捗表示 → エラー表示 → キュー継続」のUI経路を試験できるようにする。
            if spec.prompt.lstrip().startswith(MOCK_FAIL_PREFIX):
                self._emit_error(
                    spec,
                    "モック失敗注入プロンプトが指定されました",
                    detail=f"prompt に {MOCK_FAIL_PREFIX} が指定されています（§16.2 の隠し仕様）",
                )
                return

            self._emit(
                EngineEvent(
                    type=EventType.STAGE, job_id=spec.job_id, stage=JobStage.SAVING
                )
            )
            if not self._sleep(total * SAVING_RATIO, stop):
                return

            # 昇格の**前に**ジョブを確定させる（RealEngine._handle_done と同じ順序）。
            # restart() が先に中断 ERROR を出していたら成果物を作らずに抜ける
            # （FAILED のジョブの動画が正式名で残る＝履歴に載らない孤児を作らない）。
            if self._take_current_job(spec.job_id) is None:
                log.warning(
                    "確定済みのジョブなので成果物を作らずに終了します（再起動と競合）: %s",
                    spec.job_id,
                )
                return

            try:
                output_path, last_frame_path = self._write_artifacts(spec)
            except Exception as e:  # noqa: BLE001 - 昇格失敗はジョブ単位の失敗
                self._emit_error_taken(spec, _one_line(e), detail=_tail_traceback())
                return

            self._emit(
                EngineEvent(
                    type=EventType.DONE,
                    job_id=spec.job_id,
                    elapsed_sec=round(time.monotonic() - started, 3),
                    output_path=output_path,
                    last_frame_path=last_frame_path,
                    seed_used=seed_used,
                    backend_id=self._identity.backend_id,
                    model_id=self._identity.model_id,
                    model_revision=self._identity.model_revision,
                    warnings=(),
                )
            )
        except Exception as e:  # noqa: BLE001 - エンジンは死なずに ERROR を返す
            self._emit_error(spec, _one_line(e), detail=_tail_traceback())
        finally:
            with self._lock:
                # stop 済み＝この世代は破棄されている（shutdown / restart）。
                # 新しい世代の BUSY を勝手に READY へ戻さない。
                if not stop.is_set() and self._state is EngineState.BUSY:
                    self._state = EngineState.READY

    def _emit_error(self, spec: JobSpec, message: str, detail: str = "") -> None:
        """実行中ジョブを確定させてから非 fatal な ERROR を発行する。"""
        if self._take_current_job(spec.job_id) is None:
            # restart() が中断 ERROR を出した後のジョブ。終端イベントは1回だけにする。
            log.warning(
                "確定済みのジョブなので ERROR を発行しません（再起動と競合）: %s",
                spec.job_id,
            )
            return
        self._emit_error_taken(spec, message, detail=detail)

    def _emit_error_taken(self, spec: JobSpec, message: str, detail: str = "") -> None:
        """確定済み（`_take_current_job` 済み）のジョブへ非 fatal な ERROR を発行する。

        モックには再起動すべきワーカープロセスが無いため、失敗しても
        エンジンは生存し READY に戻る（後続ジョブを続けて処理できる）。

        分類は `input`（非fatal）。`[MOCK_FAIL]` はプロンプト（入力）が原因の失敗であり、
        §13.3 の分類表でも「入力起因＝非fatal・ワーカーは再利用してよい」に当たる。
        `pipeline` は同表で fatal（ワーカーを捨てて再初期化する）と定義されているため、
        fatal=False と組み合わせると P3 の自動再起動の判定と食い違う。
        """
        log.warning("モック生成が失敗しました（job_id=%s）: %s", spec.job_id, message)
        self._emit(
            EngineEvent(
                type=EventType.ERROR,
                job_id=spec.job_id,
                fatal=False,
                category=ErrorCategory.INPUT,
                message=message,
                detail=detail,
            )
        )

    def _resolve_ffmpeg(self) -> str:
        with self._lock:
            if self._ffmpeg is None:
                self._ffmpeg = ffmpeg_ops.resolve_ffmpeg(self._ffmpeg_path)
            return self._ffmpeg

    def _write_artifacts(self, spec: JobSpec) -> tuple[Path, Path]:
        """モック素材を partial 経由で正式名へ昇格する（設計書 §10.7）。

        MP4・PNG の**両方の検証に合格してから**昇格し、PNG の昇格に失敗した場合は
        昇格済みの MP4 を撤去する。これにより「片方だけが正式名で残る」状態を作らない。
        DONE はこの関数が正常に返った後にのみ発行する。
        """
        src_video = self._assets_dir / f"mock_{spec.num_frames}.mp4"
        src_png = self._assets_dir / f"mock_{spec.num_frames}_last.png"
        for src in (src_video, src_png):
            if not src.is_file():
                raise fileops.FileopsError(
                    f"モック素材が見つかりません: {src}"
                    "（scripts/setup.sh を実行してください）"
                )

        # 上位層が渡したパスでも、書き込み先が data_root 配下であることを下位層で再検証する。
        # 検証は解決後のパスで行い、書き込みと DONE の報告は
        # 上位層が渡したパスのまま扱う（履歴の相対化が data_root と食い違わないようにする）。
        fileops.ensure_within(self._data_root, spec.output_path)
        fileops.ensure_within(self._data_root, spec.last_frame_path)
        out_video = spec.output_path
        out_png = spec.last_frame_path
        out_video.parent.mkdir(parents=True, exist_ok=True)
        out_png.parent.mkdir(parents=True, exist_ok=True)

        # §10.7 手順4 の動画検証は ffmpeg_ops の共通実装を使う
        # （連結・モック・P2の実機生成で検証が食い違わないようにする）
        ffmpeg = self._resolve_ffmpeg()
        validate_video = ffmpeg_ops.video_validator(
            ffmpeg, spec.num_frames / spec.fps
        )

        video_partial = fileops.partial_path(out_video)
        png_partial = fileops.partial_path(out_png)
        shutil.copyfile(src_video, video_partial)
        shutil.copyfile(src_png, png_partial)

        # PNG を先に検証しておき、「動画だけ昇格して PNG で失敗」を避ける
        fileops.verify_png(png_partial)
        fileops.promote(video_partial, out_video, (validate_video,))
        try:
            fileops.promote(png_partial, out_png, (fileops.verify_png,))
        except Exception:
            # 履歴に載らない孤児 MP4 を残さない（両方昇格するか、どちらも残さないか）
            out_video.unlink(missing_ok=True)
            raise
        return out_video, out_png


def _tail_traceback(lines: int = 5) -> str:
    return "\n".join(traceback.format_exc().strip().splitlines()[-lines:])
