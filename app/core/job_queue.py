"""ジョブキュー: 直列ディスパッチ・状態遷移・ワーカー自動再起動（設計書 §9・§13.2・§13.3）。

責務:
- 投入された JobSpec を FIFO で1件ずつ Execution Engine へ渡す（同時実行は常に1件）。
- `contracts.JobStatus` の状態機械を守り、不正遷移を拒否する。
- 履歴更新は `contracts.JobRecorder` 越しに委譲する（HistoryStore へ直接依存しない）。
  統合層（AppService）が HistoryStore を配線する。
- P3: fatal エラー／ワーカー異常終了後の自動再起動（バックオフ・連続失敗カウント・HALTED）、
  停滞警告（watchdog）、空き容量による受付停止。

Engine への依存:
- `app.engine` を import せず、ダックタイピングで受ける。必要な操作は
  `state()` / `start()` / `submit(spec)` / `poll_event(timeout)` / `shutdown(timeout)` /
  `restart()` の6つ。
- これにより mock / real どちらの Execution Engine でも同じキューが使える。

スレッド構成:
- ディスパッチャは daemon スレッド1本のみ。RUNNING を保持できるのはこのスレッドだけなので
  「同時実行1」は構造的に保証される（並列化のコードパスを作らない）。
- **再起動もディスパッチャスレッドだけが実行する**（`restart_worker()` は要求を置くだけ）。
  こうすると「再起動中に次のジョブが走る」「二重に再起動する」経路が構造的に存在しない。
- 可変状態はすべて RLock で保護し、外部へは frozen dataclass のコピーだけを返す。
- 待機は `engine.poll_event(timeout)` と `Event.wait()` に限定し、ビジーウェイトしない。
- 外部から渡されたコールバック（intake_guard / estimate_fn / sleep）はロックの外で呼ぶ。

engine_state の扱い:
- READY / 初期化中の STAGE イベントで即時更新する（UI の「モデル初期化中…」表示のため）。
- ディスパッチ判定の直前・ジョブ投入直後・ジョブ終了直後は `engine.state()` で確定値へ同期する。
  ディスパッチの可否判定には必ずこの確定値を使う（キャッシュでは判定しない）。

エラー方針（§13.2）:
- 1件の失敗でキューを止めない。エンジンが例外を投げても当該ジョブを FAILED にして次へ進む。
- JobRecorder の失敗はログのみ（履歴が書けなくても生成キューは止めない）。
- **正常な生成を絶対に止めない**：停滞判定は「ジョブの総経過時間 対 目安時間×係数」で行い、
  イベント間隔では判定しない（実機では 4/4 到達後に約150秒イベントが来ないため）。
  既定では警告フラグを立てるだけで、生成は止めない（`stall_abort_factor=0.0`）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.core.contracts import (
    FATAL_CATEGORIES,
    EngineEvent,
    EngineState,
    ErrorCategory,
    EventType,
    JobRecorder,
    JobSpec,
    JobStage,
    JobStatus,
    JobView,
    QueueFullError,
    QueueSnapshot,
    RestartState,
    ValidationError,
    can_transition,
    validate_job_spec,
)

logger = logging.getLogger("atelier.queue")

#: ディスパッチャスレッドの名前（終了確認・ログ用）
DISPATCHER_THREAD_NAME = "atelier-queue-dispatcher"

#: 待機ジョブが無いときに空き容量を再確認する最短間隔（秒）。
#: UI の受付停止バナーを新しく保ちつつ、statvfs を叩き続けないための下限。
_INTAKE_IDLE_INTERVAL = 1.0

#: エンジン初期化中のステージ → EngineState（設計書 §9.2）
_ENGINE_INIT_STAGES: dict[JobStage, EngineState] = {
    JobStage.LOADING_MODEL: EngineState.INITIALIZING_MODEL,
    JobStage.LOADING_LORA: EngineState.INITIALIZING_LORA,
}


def _is_fatal(event: EngineEvent) -> bool:
    """ERROR が fatal か（設計書 §13.3）。fatal 未指定なら category から判定する。"""
    if event.fatal is not None:
        return bool(event.fatal)
    return event.category in FATAL_CATEGORIES


@dataclass
class _JobEntry:
    """キュー内部の可変ジョブ状態。外部へは必ず `to_view()` のコピーで渡す。"""

    spec: JobSpec
    status: JobStatus
    queued_at: datetime
    stage: JobStage | None = None
    step: int | None = None
    total_steps: int | None = None
    seed_used: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_sec: float | None = None
    error: str | None = None
    error_category: str | None = None
    output_path: Path | None = None
    last_frame_path: Path | None = None
    #: 実行開始時の monotonic 時刻（watchdog 用。壁時計の巻き戻りに影響されない）
    started_monotonic: float | None = None
    #: このジョブの目安秒（`estimate_fn` を開始時に1回だけ呼んで保持する）
    estimate_sec: float | None = None
    #: 最後にエンジンからイベントが届いた時刻（UI の「最終処理中」推定に使う）
    last_event_at: datetime | None = None
    #: 停滞警告中か（設計書 §13.2。警告のみで自動停止はしない）
    stalled: bool = False

    @property
    def job_id(self) -> str:
        return self.spec.job_id

    def transition(self, new: JobStatus) -> bool:
        """許可された遷移のみ適用する（設計書 §9.1）。不正なら False を返し状態を変えない。"""
        if not can_transition(self.status, new):
            logger.warning(
                "不正な状態遷移を拒否しました: job=%s %s -> %s",
                self.job_id,
                self.status.value,
                new.value,
            )
            return False
        self.status = new
        return True

    def to_view(self) -> JobView:
        return JobView(
            job_id=self.spec.job_id,
            status=self.status,
            prompt_head=self.spec.prompt_head,
            num_frames=self.spec.num_frames,
            steps=self.spec.steps,
            duration_label=self.spec.duration_label,
            seed_requested=self.spec.seed_requested,
            seed_used=self.seed_used,
            stage=self.stage,
            step=self.step,
            total_steps=self.total_steps,
            queued_at=self.queued_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            elapsed_sec=self.elapsed_sec,
            error=self.error,
            error_category=self.error_category,
            output_path=self.output_path,
            last_frame_path=self.last_frame_path,
            last_event_at=self.last_event_at,
            stalled=self.stalled,
        )


class JobQueue:
    """直列ジョブキュー（同時実行は常に1件・FIFO）。

    Args:
        engine: Execution Engine（ダックタイピング。上記の6操作を持つオブジェクト）
        recorder: 履歴更新の委譲先（`contracts.JobRecorder`）
        max_queued_jobs: 待機列に積める上限（config `queue.max_queued_jobs`）
        allow_cancel_queued: QUEUED の取消を許可するか（config `queue.allow_cancel_queued`）
        poll_interval: エンジンイベント待ちのタイムアウト秒（ディスパッチ遅延の上限でもある）
        clock: 時刻取得（テスト差し替え用）
        max_auto_restarts: 連続失敗が何回までなら自動再起動するか（config `max_auto_restarts`）
        restart_backoff_sec: 再起動前の待機秒（`[min(連続失敗-1, len-1)]` で引く）
        auto_restart_worker: False なら自動再起動せず HALTED にする（手動再起動のみ）
        estimate_fn: `(num_frames, steps) -> 目安秒`。None なら watchdog 無効
        stall_warn_factor: 目安時間の何倍で「停滞警告」を出すか（**停止はしない**）
        stall_abort_factor: 目安時間の何倍で強制終了するか。**0.0 = 無効（既定）**
        intake_guard: `() -> 受付停止理由 or None`。submit とディスパッチ直前に呼ぶ
        monotonic: 単調時計（watchdog・バックオフ残り秒。テスト差し替え用）
        sleep: `(秒) -> True=経過 / False=中断`。None なら内部の Event.wait を使う
    """

    def __init__(
        self,
        engine: Any,
        recorder: JobRecorder,
        *,
        max_queued_jobs: int = 20,
        allow_cancel_queued: bool = True,
        poll_interval: float = 0.2,
        clock: Callable[[], datetime] = datetime.now,
        # --- P3（すべてキーワード専用・既定値つき。既存の呼び出しは無変更で動く） ---
        max_auto_restarts: int = 2,
        restart_backoff_sec: tuple[int, ...] = (5, 30),
        auto_restart_worker: bool = True,
        estimate_fn: Callable[[int, int], float] | None = None,
        stall_warn_factor: float = 3.0,
        stall_abort_factor: float = 0.0,
        intake_guard: Callable[[], str | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self._engine = engine
        self._recorder = recorder
        self._max_queued_jobs = max(1, int(max_queued_jobs))
        self._allow_cancel_queued = bool(allow_cancel_queued)
        self._poll_interval = float(poll_interval) if poll_interval > 0 else 0.2
        self._clock = clock

        self._max_auto_restarts = max(0, int(max_auto_restarts))
        backoff = tuple(max(0.0, float(v)) for v in (restart_backoff_sec or ()))
        self._restart_backoff_sec: tuple[float, ...] = backoff or (0.0,)
        self._auto_restart_worker = bool(auto_restart_worker)
        self._estimate_fn = estimate_fn
        self._stall_warn_factor = max(0.0, float(stall_warn_factor))
        self._stall_abort_factor = max(0.0, float(stall_abort_factor))
        self._intake_guard = intake_guard
        self._monotonic = monotonic
        self._sleep: Callable[[float], bool] = (
            sleep if sleep is not None else self._default_sleep
        )

        self._lock = threading.RLock()
        self._queued: deque[_JobEntry] = deque()
        self._current: _JobEntry | None = None
        self._last_finished: JobView | None = None
        #: 直近に終了させたジョブID（engine.restart() が後追いで出す中断 ERROR を
        #: 「新たなワーカー障害」と誤認して二重に再起動しないための照合用）
        self._finished_ids: deque[str] = deque(maxlen=8)
        self._engine_state: EngineState = EngineState.STARTING

        self._stop = threading.Event()
        #: バックオフ待機を起こすためのイベント（停止要求・手動再起動要求で set する）
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._shutdown_done = False

        self._accepted_total = 0
        self._succeeded_total = 0
        self._failed_total = 0

        # --- P3: 再起動制御（すべて _lock 配下） ---
        self._restart_state = RestartState.IDLE
        self._consecutive_failures = 0
        self._restart_total = 0
        self._halted_reason: str | None = None
        self._restart_pending = False  # 自動再起動の予約
        self._manual_restart_reason: str | None = None  # 手動再起動の要求
        self._backoff_deadline: float | None = None
        self._intake_blocked_reason: str | None = None
        self._last_intake_check: float | None = None

    # ------------------------------------------------------------ ライフサイクル

    def start(self) -> None:
        """engine.start() とディスパッチャ（daemon スレッド）を開始する。二重呼び出し安全。"""
        with self._lock:
            if self._shutdown_done:
                raise RuntimeError("shutdown 済みの JobQueue は再開できません")
            if self._started:
                return
            self._started = True
            self._stop.clear()
            self._wake.clear()
            thread = threading.Thread(
                target=self._dispatch_loop, name=DISPATCHER_THREAD_NAME, daemon=True
            )
            self._thread = thread

        try:
            self._engine.start()
        except Exception:
            logger.exception("生成エンジンの起動に失敗しました")
            with self._lock:
                self._started = False
                self._thread = None
            raise

        thread.start()
        self._refresh_engine_state()

    def shutdown(self, timeout: float = 5.0) -> None:
        """ディスパッチャを停止し engine.shutdown() を呼ぶ。二重呼び出し安全。

        join に失敗しても daemon スレッドなのでプロセス終了は妨げない。
        """
        with self._lock:
            already = self._shutdown_done
            self._shutdown_done = True
            self._started = False
            thread = self._thread
        self._stop.set()
        self._wake.set()  # バックオフ待機中でも即座に抜ける（契約 §2.1-6）

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout if timeout and timeout > 0 else None)
            if thread.is_alive():
                logger.warning(
                    "ディスパッチャの停止を待てませんでした"
                    "（daemon スレッドのためプロセス終了は妨げません）"
                )
        with self._lock:
            if thread is None or not thread.is_alive():
                self._thread = None

        if already:
            return
        try:
            self._engine.shutdown(timeout)
        except Exception:
            logger.exception("生成エンジンの停止に失敗しました")

    # ------------------------------------------------------------ 投入・取消

    def submit(self, spec: JobSpec) -> JobView:
        """検証 → 履歴 QUEUED 通知 → 待機列へ FIFO 追加（非ブロッキング）。

        Raises:
            ValidationError: 入力値が不正、または空き容量不足で受付停止中（日本語メッセージ）
            QueueFullError: 待機列が上限に達している（日本語メッセージ）
        """
        validate_job_spec(spec)
        # 空き容量ガード（設計書 §13.2）。ここで断ったジョブは履歴レコードを作らない。
        blocked = self._check_intake_guard()
        if blocked is not None:
            raise ValidationError(blocked)
        queued_at = self._clock()

        with self._lock:
            if self._shutdown_done:
                raise ValidationError(
                    "終了処理中のため、新しい生成を受け付けられません"
                )
            if len(self._queued) >= self._max_queued_jobs:
                raise QueueFullError(
                    f"生成キューが上限（{self._max_queued_jobs}件）に達しています。"
                    "完了を待ってから追加してください"
                )
            if self._find_locked(spec.job_id) is not None:
                raise ValidationError(
                    f"同じIDのジョブがすでにキューにあります: {spec.job_id}"
                )

            entry = _JobEntry(spec=spec, status=JobStatus.QUEUED, queued_at=queued_at)
            # 履歴 QUEUED 通知は待機列へ入れる前に行う（RUNNING 通知より先を保証する）。
            self._notify("on_queued", spec, queued_at)
            self._queued.append(entry)
            self._accepted_total += 1
            view = entry.to_view()

        logger.info(
            "ジョブを受け付けました: job=%s frames=%s steps=%s 待機=%s件",
            spec.job_id,
            spec.num_frames,
            spec.steps,
            len(self._queued),
        )
        return view

    def cancel_queued(self, job_id: str) -> bool:
        """QUEUED のみ取消可（設計書 §9.1 決定D14a）。RUNNING・終了済み・未知IDは False。"""
        canceled_at = self._clock()
        with self._lock:
            if not self._allow_cancel_queued:
                return False
            target = None
            for entry in self._queued:
                if entry.job_id == job_id:
                    target = entry
                    break
            if target is None:
                return False
            if not target.transition(JobStatus.CANCELED):
                return False
            self._queued.remove(target)
            target.finished_at = canceled_at

        self._notify("on_canceled", job_id, canceled_at)
        logger.info("待機中のジョブを取り消しました: job=%s", job_id)
        return True

    # ------------------------------------------------------------ 参照（不変コピー）

    def current_job(self) -> JobView | None:
        with self._lock:
            return self._current.to_view() if self._current is not None else None

    def queued_jobs(self) -> list[JobView]:
        with self._lock:
            return [entry.to_view() for entry in self._queued]

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queued)

    def snapshot(self) -> QueueSnapshot:
        now = self._now_monotonic()  # ロックの外で（注入された時計を握ったまま呼ばない）
        with self._lock:
            remaining = 0.0
            if (
                self._restart_state is RestartState.BACKOFF
                and self._backoff_deadline is not None
            ):
                remaining = max(0.0, self._backoff_deadline - now)
            return QueueSnapshot(
                engine_state=self._engine_state,
                current=self._current.to_view() if self._current is not None else None,
                queued=tuple(entry.to_view() for entry in self._queued),
                queue_size=len(self._queued),
                last_finished=self._last_finished,
                running=(
                    self._thread is not None
                    and self._thread.is_alive()
                    and not self._stop.is_set()
                ),
                accepted_total=self._accepted_total,
                succeeded_total=self._succeeded_total,
                failed_total=self._failed_total,
                restart_state=self._restart_state,
                consecutive_failures=self._consecutive_failures,
                backoff_remaining_sec=remaining,
                halted_reason=self._halted_reason,
                restart_total=self._restart_total,
                intake_blocked_reason=self._intake_blocked_reason,
            )

    # ------------------------------------------------------------ 手動再起動

    def restart_worker(self, *, reason: str = "手動再起動") -> bool:
        """UI の [ワーカーを再起動] から呼ぶ（設計書 §13.3）。要求を置いて即座に戻る。

        実際の再起動はディスパッチャスレッドが行う（再起動経路を1本に保つため）。
        実行中ジョブは fatal 扱いで FAILED になり、HALTED からの復帰も兼ねる
        （連続失敗カウントを 0 に戻す）。二重呼び出し安全。shutdown 後は False。

        Returns:
            要求を受け付けたら True。停止済み・未開始で受け付けられないなら False。
        """
        text = str(reason).strip() or "手動再起動"
        with self._lock:
            if self._shutdown_done or self._stop.is_set():
                logger.info("停止処理中のため再起動要求を受け付けません")
                return False
            if not self._started or self._thread is None or not self._thread.is_alive():
                logger.warning(
                    "ディスパッチャが動いていないためワーカーを再起動できません"
                )
                return False
            if self._manual_restart_reason is not None:
                logger.info("再起動はすでに要求済みです（重複要求を無視しました）")
                return True
            self._manual_restart_reason = text
        self._wake.set()  # バックオフ待機中なら即座に起こす
        logger.warning("ワーカーの再起動を要求しました: %s", text)
        return True

    # ------------------------------------------------------------ ディスパッチャ

    def _dispatch_loop(self) -> None:
        logger.info("ディスパッチャを開始しました")
        try:
            while not self._stop.is_set():
                # 再起動はジョブ開始より優先する（READY まで次を始めない・契約 §2.1-2）。
                if self._restart_due():
                    self._perform_restart()
                    continue
                entry = self._take_next_ready()
                if entry is not None:
                    self._run_job(entry)
                    continue
                self._idle_step()
        except Exception:  # pragma: no cover - 想定外。落ちてもプロセスは維持する
            logger.exception("ディスパッチャが異常終了しました")
        finally:
            logger.info("ディスパッチャを停止しました")

    def _take_next_ready(self) -> _JobEntry | None:
        """engine が READY・実行中ジョブなし・待機列が空でないときだけ次を取り出す。

        再起動中（BACKOFF / RESTARTING）・HALTED・空き容量不足のときは取り出さず、
        待機ジョブを QUEUED のまま保持する（捨てない・失敗させない）。
        """
        with self._lock:
            if self._current is not None or not self._queued:
                return None
            if self._restart_state is not RestartState.IDLE:
                return None
            if self._restart_pending or self._manual_restart_reason is not None:
                return None

        # ディスパッチ直前の空き容量ガード（設計書 §13.2）。
        if self._check_intake_guard() is not None:
            return None

        if self._refresh_engine_state() is not EngineState.READY:
            return None

        with self._lock:
            if self._current is not None or not self._queued:
                return None
            if self._restart_state is not RestartState.IDLE:
                return None
            entry = self._queued.popleft()
            started_at = self._clock()
            if not entry.transition(JobStatus.RUNNING):
                # 起こらない想定（待機列は QUEUED のみ）。取り出した分を戻して
                # ジョブが UI からもキューからも消える事態を避ける。
                logger.error(
                    "待機列のジョブを開始できませんでした（状態=%s）: job=%s",
                    entry.status.value,
                    entry.job_id,
                )
                self._queued.appendleft(entry)
                return None
            entry.started_at = started_at
            entry.started_monotonic = self._now_monotonic()
            entry.last_event_at = started_at
            entry.stage = JobStage.PREPARING
            self._current = entry

        # estimate_fn は外部コード。ロックの外で1回だけ呼んで結果を持ち回る。
        estimate = self._estimate_for(entry.spec)
        with self._lock:
            entry.estimate_sec = estimate

        self._notify("on_running", entry.job_id, started_at)
        logger.info("ジョブを開始しました: job=%s", entry.job_id)
        return entry

    def _idle_step(self) -> None:
        """待機中もエンジンイベントを消費する（初期化ステージ・READY の反映）。"""
        with self._lock:
            idle = self._current is None
            cached = self._engine_state
        # ここでは engine.state() を覗くだけで無条件には反映しない。
        # 初期化中の STAGE イベント（INITIALIZING_*）を STARTING で塗り潰さないため。
        state = self._peek_engine_state()
        if idle and state is EngineState.DEAD:
            # 実行中ジョブが無いままワーカーが死ぬとイベントが来ない（RealEngine の仕様）。
            # ここで拾わないとキューは永久に READY を待ち続ける。
            self._set_engine_state(state)
            self._note_worker_failure("生成ワーカーが停止しました")
            if self._restart_due():
                return  # 待たずに再起動へ進む
        elif idle and cached is EngineState.BUSY:
            # エンジンが DONE を出してから READY へ戻すまでの間に同期した場合の是正。
            # 実行中ジョブが無いのに BUSY 表示のままにしない。
            self._set_engine_state(state)
        self._refresh_intake_guard_if_idle()
        try:
            event = self._engine.poll_event(self._poll_interval)
        except Exception:
            logger.exception("エンジンイベントの取得に失敗しました（待機中）")
            self._stop.wait(self._poll_interval)  # 例外連発でも CPU を回さない
            return
        if event is not None:
            self._handle_event(event, None)

    def _run_job(self, entry: _JobEntry) -> None:
        """1件を実行しきる（同時実行1の保証点。ここを抜けるまで次は取り出さない）。"""
        try:
            self._run_job_inner(entry)
        finally:
            # 実行前後でエンジン状態（BUSY→READY 等）を確定させる。
            self._refresh_engine_state()

    def _run_job_inner(self, entry: _JobEntry) -> None:
        if self._stop.is_set():
            # 停止処理中はエンジンへ渡さない。RUNNING のまま残し、次回起動時に
            # INTERRUPTED として確定させる（「終了しただけ」を FAILED にしない）。
            return
        try:
            self._engine.submit(entry.spec)
        except ValidationError as e:
            # 入力起因（非 fatal）。ワーカーは健全なので再起動しない（契約 §2.1-7）。
            self._finish_failed(entry, str(e), ErrorCategory.INPUT)
            return
        except Exception as e:
            logger.exception("エンジンへのジョブ投入に失敗しました: job=%s", entry.job_id)
            message = f"生成エンジンにジョブを渡せませんでした: {e}"
            if self._finish_failed(entry, message, ErrorCategory.PIPELINE):
                self._note_worker_failure(message)
            return
        self._refresh_engine_state()  # 実行中は BUSY（UI 表示用）

        while not self._stop.is_set():
            if self._abort_for_manual_restart(entry):
                return
            try:
                event = self._engine.poll_event(self._poll_interval)
            except Exception as e:
                logger.exception(
                    "エンジンイベントの取得に失敗しました: job=%s", entry.job_id
                )
                message = f"生成エンジンとの通信に失敗しました: {e}"
                if self._finish_failed(entry, message, ErrorCategory.WORKER_DEAD):
                    self._note_worker_failure(message)
                return
            if event is not None and self._handle_event(event, entry):
                return
            if event is None:
                # イベントが来ない周回でだけ、ワーカーの生存を確認する。
                # 終端イベントの合成（RealEngine の worker_dead）が失われた場合に
                # ジョブが RUNNING のまま永久に残るのを防ぐ（_idle_step と対称）。
                if self._peek_engine_state() is EngineState.DEAD:
                    message = "生成ワーカーが停止しました（終端イベントが届きませんでした）"
                    logger.error("%s: job=%s", message, entry.job_id)
                    if self._finish_failed(entry, message, ErrorCategory.WORKER_DEAD):
                        self._note_worker_failure(message)
                    return
                # 生成中も受付可否を更新する（バナーが1ジョブ分古くならないように）
                self._refresh_intake_guard_if_idle()
            # 停滞監視（設計書 §13.2）。既定では警告フラグを立てるだけで停止しない。
            if self._check_stall(entry):
                return
        # 停止要求で抜けた場合は RUNNING のまま残す。
        # 「アプリ終了による中断（INTERRUPTED）」の確定は次回起動時に履歴側が行う（§9.1）。

    # ------------------------------------------------------------ イベント処理

    def _handle_event(self, event: EngineEvent, entry: _JobEntry | None) -> bool:
        """イベントを反映する。戻り値は「実行中ジョブが終了状態に達したか」。"""
        etype = event.type
        # DONE / PROGRESS は job_id の厳密一致を要求する。
        # job_id を欠いた DONE を実行中ジョブの成功として扱うと、
        # （P2 のプロセス間通信で job_id が落ちた場合に）誤 SUCCESS を生む。
        # ERROR はワーカー全体の死を job_id なしで通知しうるため寛容に扱う。
        if etype in (EventType.DONE, EventType.PROGRESS):
            matches = entry is not None and event.job_id == entry.job_id
        else:
            matches = entry is not None and (
                event.job_id is None or event.job_id == entry.job_id
            )

        if matches and entry is not None:
            # UI が「最終処理中（4/4 の後の無音区間）」を推定するための材料。
            with self._lock:
                entry.last_event_at = self._clock()

        if etype is EventType.READY:
            self._set_engine_state(EngineState.READY)
            return False

        if etype is EventType.STAGE:
            init_state = _ENGINE_INIT_STAGES.get(event.stage) if event.stage else None
            if init_state is not None:
                # エンジン初期化中のステージ。ジョブには影響しない（§9.2）。
                self._set_engine_state(init_state)
                return False
            if matches and event.stage is not None:
                with self._lock:
                    entry.stage = event.stage  # type: ignore[union-attr]
                return False
            logger.debug("対象外の STAGE イベントを無視しました: %s", event)
            return False

        if etype is EventType.PROGRESS:
            if matches:
                with self._lock:
                    entry.step = event.step  # type: ignore[union-attr]
                    entry.total_steps = event.total  # type: ignore[union-attr]
                    # PROGRESS が届いた＝デノイズループに入っている。
                    # ディスパッチ時に PREPARING を入れているため条件付きにすると
                    # 永久に GENERATING にならず、UI のステップ表示が出ない。
                    entry.stage = JobStage.GENERATING  # type: ignore[union-attr]
                return False
            logger.debug("対象外の PROGRESS イベントを無視しました: %s", event)
            return False

        if etype is EventType.DONE:
            if matches:
                self._finish_success(entry, event)  # type: ignore[arg-type]
                return True
            logger.warning(
                "実行中でないジョブの DONE を無視しました: job=%s", event.job_id
            )
            return False

        if etype is EventType.ERROR:
            if matches:
                self._finish_error(entry, event)  # type: ignore[arg-type]
                return True
            logger.error(
                "実行中ジョブと無関係なエンジンエラー: job=%s fatal=%s category=%s %s",
                event.job_id,
                event.fatal,
                event.category.value if event.category else None,
                event.message,
            )
            if _is_fatal(event):
                with self._lock:
                    stale = (
                        event.job_id is not None and event.job_id in self._finished_ids
                    )
                if stale:
                    # engine.restart() が「中断しました」を後追いで届けたもの。
                    # すでに FAILED 済みなので、新たな障害として数えない。
                    logger.info(
                        "終了済みジョブ宛の fatal エラーを無視しました: job=%s",
                        event.job_id,
                    )
                elif self._is_stale_fatal_during_restart():
                    logger.info(
                        "再起動処理中に届いた旧ワーカーの fatal エラーを無視しました: %s",
                        event.message,
                    )
                else:
                    # ワーカー全体の死亡通知（job_id なし等）。再起動経路へ入る。
                    self._note_worker_failure(
                        event.message or "生成ワーカーで致命的なエラーが発生しました"
                    )
            return False

        logger.warning("未知のイベント種別を無視しました: %s", etype)
        return False

    # ------------------------------------------------------------ 終了処理

    def _finish_success(self, entry: _JobEntry, event: EngineEvent) -> None:
        finished_at = self._clock()
        # 成功は「エンジンが昇格済みの成果物を報告した」ことが前提（設計書 §10.7）。
        # 予定パスで代替すると、実在しないファイルで SUCCESS を確定してしまう。
        output_path = event.output_path
        if output_path is None:
            self._finish_failed(
                entry,
                "生成エンジンが出力ファイルを報告しませんでした",
                ErrorCategory.PIPELINE,
            )
            return
        if not Path(output_path).is_file():
            self._finish_failed(
                entry,
                f"生成された動画が見つかりません: {Path(output_path).name}",
                ErrorCategory.PIPELINE,
            )
            return
        if event.last_frame_path is None:
            logger.warning(
                "DONE に last_frame_path がありません（継続生成に使えません）: job=%s",
                entry.job_id,
            )
        self._warn_identity_mismatch(entry, event)
        with self._lock:
            if not entry.transition(JobStatus.SUCCESS):
                return
            entry.finished_at = finished_at
            entry.elapsed_sec = (
                event.elapsed_sec
                if event.elapsed_sec is not None
                else self._elapsed(entry, finished_at)
            )
            if event.seed_used is not None:
                entry.seed_used = event.seed_used
            entry.output_path = output_path
            entry.last_frame_path = event.last_frame_path
            entry.stage = None
            entry.stalled = False
            view = entry.to_view()
            self._current = None
            self._last_finished = view
            self._finished_ids.append(entry.job_id)
            self._succeeded_total += 1
            # ジョブが1本通ったらワーカーは健全とみなす（設計書 §13.3・契約 §2.1-4）。
            if self._consecutive_failures:
                logger.info(
                    "生成が成功したため連続失敗カウントを 0 に戻します（%s → 0）",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0

        for warning in event.warnings:
            logger.warning("生成の警告: job=%s %s", entry.job_id, warning)

        self._notify(
            "on_success",
            entry.job_id,
            output_path=output_path,
            last_frame_path=event.last_frame_path,
            seed_used=view.seed_used,
            elapsed_sec=view.elapsed_sec,
            finished_at=finished_at,
        )
        logger.info(
            "ジョブが完了しました: job=%s 経過=%.1f秒",
            entry.job_id,
            view.elapsed_sec or 0.0,
        )

    def _warn_identity_mismatch(self, entry: _JobEntry, event: EngineEvent) -> None:
        """DONE が運ぶバックエンド識別が投入時と食い違っていたら警告する。

        履歴には config 由来の identity（投入時に確定）が入る。P2 では
        「config に書いた revision」と「ワーカーが実際に読み込んだ revision」が
        ずれうるため、気付けるようにしておく。
        """
        if event.backend_id and event.backend_id != entry.spec.backend_id:
            logger.warning(
                "DONE のバックエンドが投入時と異なります: job=%s 投入=%s 完了=%s",
                entry.job_id,
                entry.spec.backend_id,
                event.backend_id,
            )

    def _finish_error(self, entry: _JobEntry, event: EngineEvent) -> None:
        message = event.message or "生成に失敗しました"
        if event.detail:
            logger.error("生成エラーの詳細: job=%s %s", entry.job_id, event.detail)
        fatal = _is_fatal(event)
        failed = self._finish_failed(
            entry, message, event.category, elapsed_sec=event.elapsed_sec
        )
        if not failed:
            return
        if fatal:
            # fatal 後のワーカーは内部状態を信頼できないので再利用しない（§13.3）。
            # このジョブは自動再実行しない。
            logger.error(
                "fatal なエンジンエラーです（ワーカーを再起動します）: job=%s category=%s",
                entry.job_id,
                event.category.value if event.category else None,
            )
            self._note_worker_failure(message)

    def _finish_failed(
        self,
        entry: _JobEntry,
        message: str,
        category: ErrorCategory | None,
        *,
        elapsed_sec: float | None = None,
    ) -> bool:
        """ジョブを FAILED で確定する。戻り値は「実際に遷移したか」。"""
        finished_at = self._clock()
        category_value = category.value if category is not None else None
        with self._lock:
            if not entry.transition(JobStatus.FAILED):
                return False
            entry.finished_at = finished_at
            entry.elapsed_sec = (
                elapsed_sec
                if elapsed_sec is not None
                else self._elapsed(entry, finished_at)
            )
            entry.error = message
            entry.error_category = category_value
            entry.stage = None
            view = entry.to_view()
            self._current = None
            self._last_finished = view
            self._finished_ids.append(entry.job_id)
            self._failed_total += 1

        self._notify(
            "on_failed",
            entry.job_id,
            error=message,
            category=category_value,
            elapsed_sec=view.elapsed_sec,
            finished_at=finished_at,
        )
        logger.error(
            "ジョブが失敗しました: job=%s category=%s %s",
            entry.job_id,
            category_value,
            message,
        )
        return True

    # ------------------------------------------------------------ 自動再起動（§13.3）

    def _restart_due(self) -> bool:
        with self._lock:
            return self._restart_pending or self._manual_restart_reason is not None

    def _abort_for_manual_restart(self, entry: _JobEntry) -> bool:
        """手動再起動が要求されていたら実行中ジョブを FAILED にして抜ける。

        engine.restart() が発行する中断 ERROR を待たずにここで確定させる
        （終端イベントなしでジョブが消えるのを防ぎ、再起動経路も1本に保つ）。
        """
        with self._lock:
            reason = self._manual_restart_reason
        if reason is None:
            return False
        message = f"{reason}のため生成を中断しました（このジョブは自動再実行されません）"
        # 手動再起動は「連続失敗」に数えない（カウントは再起動時に 0 へ戻す）。
        return self._finish_failed(entry, message, ErrorCategory.WORKER_DEAD)

    def _note_worker_failure(self, message: str) -> None:
        """fatal 障害を1件記録し、必要なら自動再起動を予約する（設計書 §13.3）。

        すでに再起動が予約済み／HALTED のときは何もしない。
        engine.restart() が発行する中断 ERROR を二重に数えないための要でもある。
        """
        halt_reason: str | None = None
        with self._lock:
            if self._stop.is_set() or self._shutdown_done:
                return  # 停止処理中は再起動しない（契約 §2.1-6）
            if (
                self._restart_pending
                or self._manual_restart_reason is not None
                or self._restart_state in (RestartState.BACKOFF, RestartState.HALTED)
            ):
                logger.info("再起動は予約済みのため追加の障害通知を無視しました")
                return
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if not self._auto_restart_worker:
                halt_reason = (
                    f"{message}。自動再起動が無効に設定されているため生成を停止しました。"
                    "[ワーカーを再起動] を押してください"
                )
            elif count > self._max_auto_restarts:
                halt_reason = (
                    f"{message}。自動再起動の上限（{self._max_auto_restarts}回）を"
                    "超えたため生成を停止しました。"
                    "[ワーカーを再起動] を押すか、アプリまたは Mac の再起動をお試しください"
                )
            else:
                self._restart_pending = True
                self._restart_state = RestartState.BACKOFF
                self._backoff_deadline = None
            if halt_reason is not None:
                self._restart_state = RestartState.HALTED
                self._halted_reason = halt_reason
                self._restart_pending = False
                self._backoff_deadline = None

        if halt_reason is not None:
            # 待機ジョブは QUEUED のまま保持する（捨てない・失敗させない）。
            logger.error("生成エンジンを停止状態にしました: %s", halt_reason)
        else:
            logger.warning(
                "ワーカー障害を検知しました（連続 %s 回目 / 上限 %s）: %s",
                count,
                self._max_auto_restarts,
                message,
            )

    def _is_stale_fatal_during_restart(self) -> bool:
        """再起動処理中に届いた fatal を「旧ワーカーの残骸」とみなせるか。

        RealEngine はワーカーの起動に失敗すると
        `ERROR(fatal, worker_dead, job_id なし)` をイベントキューへ残す。
        次の再起動が成功した後にこれを消費して、動き始めた新しいワーカーを
        再び落とさないよう、**いまのワーカーが健全なら**無視する。
        いまが DEAD / HALTED（起動タイムアウト等）なら「現在の障害」として扱う
        （ここで見逃すと RESTARTING のままキューが止まる）。
        """
        with self._lock:
            restarting = (
                self._restart_pending
                or self._manual_restart_reason is not None
                or self._restart_state is not RestartState.IDLE
            )
        if not restarting:
            return False
        return self._peek_engine_state() not in (
            EngineState.DEAD,
            EngineState.HALTED,
        )

    def _perform_restart(self) -> None:
        """予約された再起動を実行する（ディスパッチャスレッド専用）。"""
        with self._lock:
            if self._stop.is_set() or self._shutdown_done:
                return
            manual = self._consume_manual_request_locked()
            if not manual and not self._restart_pending:
                return
            self._restart_pending = False
            wait = 0.0 if manual else self._backoff_for(self._consecutive_failures)

        if wait > 0 and not self._backoff_wait(wait):
            return  # 停止要求で中断（再起動しない）
        with self._lock:
            # バックオフ中に手動要求が来ていたら取り込む（カウントを 0 に戻す）
            self._consume_manual_request_locked()
        self._do_restart()

    def _consume_manual_request_locked(self) -> bool:
        """ロック保持前提。手動再起動要求があれば消費して HALTED からも復帰する。"""
        reason = self._manual_restart_reason
        if reason is None:
            return False
        self._manual_restart_reason = None
        self._restart_pending = False
        self._consecutive_failures = 0
        self._halted_reason = None
        logger.warning("手動再起動を実行します: %s", reason)
        return True

    def _backoff_for(self, failures: int) -> float:
        """連続失敗 n 回目のバックオフ秒（1回目5秒・2回目以降30秒。設計書 §13.3）。"""
        index = min(max(failures, 1) - 1, len(self._restart_backoff_sec) - 1)
        return self._restart_backoff_sec[index]

    def _backoff_wait(self, wait: float) -> bool:
        """バックオフ待機。戻り値 False は「停止要求で再起動を中止した」。"""
        with self._lock:
            self._restart_state = RestartState.BACKOFF
            self._backoff_deadline = self._now_monotonic() + wait
        logger.warning("%.0f 秒後にワーカーを再起動します", wait)
        try:
            self._sleep(wait)
        except Exception:  # 注入された sleep が壊れていても再起動は進める
            logger.exception("再起動前の待機に失敗しました")
        finally:
            with self._lock:
                self._backoff_deadline = None
        if self._stop.is_set():
            logger.info("停止要求のため再起動を中止しました")
            return False
        return True

    def _do_restart(self) -> None:
        with self._lock:
            if self._stop.is_set() or self._shutdown_done:
                return
            self._restart_state = RestartState.RESTARTING
            self._restart_total += 1
            attempt = self._restart_total
        logger.warning("ワーカーを再起動します（累計 %s 回目）", attempt)
        try:
            self._engine.restart()
        except Exception as e:
            logger.exception("ワーカーの再起動に失敗しました")
            self._note_worker_failure(f"ワーカーの再起動に失敗しました: {e}")
            return
        state = self._refresh_engine_state()
        if state is EngineState.DEAD:
            self._note_worker_failure("再起動後もワーカーが停止しています")
            return
        # READY 到達で RestartState は IDLE へ戻る（_set_engine_state）。
        # それまで次のジョブは開始しない（_take_next_ready が IDLE を要求する）。
        if state is EngineState.READY:
            self._clear_restart_state()

    def _clear_restart_state(self) -> None:
        with self._lock:
            if self._restart_state is RestartState.RESTARTING:
                self._restart_state = RestartState.IDLE
                self._halted_reason = None
                logger.info("ワーカーが READY に戻りました（再起動完了）")

    def _default_sleep(self, seconds: float) -> bool:
        """既定のバックオフ待機。停止要求・手動再起動要求で即座に抜ける。"""
        if seconds <= 0:
            return not self._stop.is_set()
        self._wake.clear()
        with self._lock:
            # clear と要求のすれ違い（lost wakeup）を防ぐ
            interrupted = self._stop.is_set() or self._manual_restart_reason is not None
        if interrupted:
            return False
        return not self._wake.wait(seconds)

    # ------------------------------------------------------------ 停滞監視（§13.2）

    def _estimate_for(self, spec: JobSpec) -> float | None:
        if self._estimate_fn is None:
            return None
        try:
            value = float(self._estimate_fn(spec.num_frames, spec.steps))
        except Exception:
            logger.exception("目安時間の計算に失敗しました: job=%s", spec.job_id)
            return None
        return value if value > 0 else None

    def _check_stall(self, entry: _JobEntry) -> bool:
        """停滞を判定する。戻り値は「ジョブを強制終了したか」。

        判定は **ジョブの総経過時間** 対 **目安時間×係数**。イベント間隔では判定しない
        （実機では 4/4 到達後に約150秒イベントが来ない正常区間があるため）。
        既定（`stall_abort_factor=0.0`）では警告フラグを立てるだけで生成は止めない。
        """
        with self._lock:
            estimate = entry.estimate_sec
            started = entry.started_monotonic
            already_warned = entry.stalled
        if estimate is None or estimate <= 0 or started is None:
            return False
        elapsed = self._now_monotonic() - started

        if self._stall_abort_factor > 0 and elapsed > estimate * self._stall_abort_factor:
            limit = estimate * self._stall_abort_factor
            message = (
                f"生成が目安時間の{self._stall_abort_factor:g}倍"
                f"（約{limit / 60:.0f}分）を超えても終わらないため中断しました。"
                "生成エンジンを再起動します"
            )
            if self._finish_failed(entry, message, ErrorCategory.PIPELINE):
                self._note_worker_failure(message)
            return True

        if (
            self._stall_warn_factor > 0
            and not already_warned
            and elapsed > estimate * self._stall_warn_factor
        ):
            with self._lock:
                entry.stalled = True
            logger.warning(
                "生成に通常より時間がかかっています（経過 %.0f 秒 / 目安 %.0f 秒）: job=%s"
                "（自動停止はしません）",
                elapsed,
                estimate,
                entry.job_id,
            )
        return False

    # ------------------------------------------------------------ 空き容量ガード（§13.2）

    def _check_intake_guard(self) -> str | None:
        """`intake_guard()` を呼び、受付停止理由を更新して返す（ロックの外で呼ぶこと）。"""
        if self._intake_guard is None:
            return None
        try:
            raw = self._intake_guard()
        except Exception:
            # 空き容量を確認できないことを理由に生成を止めない（§13.2 の3原則）。
            logger.exception("空き容量の確認に失敗しました（受付は継続します）")
            return None
        reason = str(raw).strip() if raw else ""
        reason_or_none = reason or None
        with self._lock:
            changed = reason_or_none != self._intake_blocked_reason
            self._intake_blocked_reason = reason_or_none
            self._last_intake_check = self._now_monotonic()
        if changed:
            if reason_or_none:
                logger.error("空き容量不足のため受付を停止しました: %s", reason_or_none)
            else:
                logger.info("空き容量が回復したため受付を再開しました")
        return reason_or_none

    def _refresh_intake_guard_if_idle(self) -> None:
        """待機ジョブが無いときも、UI のバナー用に控えめな間隔で再確認する。"""
        if self._intake_guard is None:
            return
        now = self._now_monotonic()
        with self._lock:
            last = self._last_intake_check
            if last is not None and (now - last) < _INTAKE_IDLE_INTERVAL:
                return
        self._check_intake_guard()

    # ------------------------------------------------------------ 補助

    def _find_locked(self, job_id: str) -> _JobEntry | None:
        """ロック保持前提。待機中または実行中の同一IDを探す。"""
        if self._current is not None and self._current.job_id == job_id:
            return self._current
        for entry in self._queued:
            if entry.job_id == job_id:
                return entry
        return None

    @staticmethod
    def _elapsed(entry: _JobEntry, finished_at: datetime) -> float | None:
        if entry.started_at is None:
            return None
        try:
            return max(0.0, (finished_at - entry.started_at).total_seconds())
        except TypeError:  # pragma: no cover - clock 差し替え時の保険
            return None

    def _now_monotonic(self) -> float:
        """単調時計（テスト注入可）。壊れた注入でもディスパッチャを落とさない。"""
        try:
            return float(self._monotonic())
        except Exception:  # pragma: no cover - 注入ミスの保険
            logger.exception("monotonic の取得に失敗しました")
            return time.monotonic()

    def _set_engine_state(self, state: EngineState) -> None:
        with self._lock:
            if self._engine_state is not state:
                logger.info(
                    "エンジン状態: %s -> %s", self._engine_state.value, state.value
                )
            self._engine_state = state
        if state is EngineState.READY:
            # 再起動は「READY に戻った時点」で完了とする（§13.3-5）。
            self._clear_restart_state()

    def _peek_engine_state(self) -> EngineState:
        """engine.state() を読むだけ（キャッシュを更新しない）。取得失敗時は現在値。"""
        try:
            state = self._engine.state()
        except Exception:
            logger.exception("エンジン状態の取得に失敗しました")
            with self._lock:
                return self._engine_state
        if not isinstance(state, EngineState):
            try:
                state = EngineState(state)
            except (ValueError, TypeError):
                logger.warning("未知のエンジン状態を無視しました: %r", state)
                with self._lock:
                    return self._engine_state
        return state

    def _refresh_engine_state(self) -> EngineState:
        """ディスパッチ判定の直前に engine.state() で確定値へ同期する。"""
        state = self._peek_engine_state()
        self._set_engine_state(state)
        return state

    def _notify(self, name: str, *args: Any, **kwargs: Any) -> None:
        """JobRecorder への通知。失敗してもキューを止めない（§13.2）。"""
        method = getattr(self._recorder, name, None)
        if method is None:
            logger.warning("JobRecorder に %s がありません（記録をスキップします）", name)
            return
        try:
            method(*args, **kwargs)
        except Exception:
            logger.exception("履歴の記録に失敗しました（キューは継続します）: %s", name)
