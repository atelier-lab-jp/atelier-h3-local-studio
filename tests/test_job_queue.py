"""JobQueue のユニットテスト（設計書 §17.1 job_queue・§17.2-2,3）。

方針:
- 実エンジン・実ファイルは使わない。フェイクエンジン／フェイクレコーダで検証する。
- 実時間 sleep で待たず、Queue.get(timeout) / Condition.wait(timeout) で同期する。
  すべての待機にタイムアウトを付け、失敗時にハングしない。
- **バックオフの 5秒・30秒を実時間で待たない**：`sleep` と `monotonic` を注入し、
  「何秒待とうとしたか」を呼び出し引数で検証する（P3・設計書 §13.3）。
- 出力パスは tmp_path のみ（プロジェクトの data/ には一切書き込まない。ファイルも作らない）。
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from app.core.contracts import (
    EngineEvent,
    EngineState,
    ErrorCategory,
    EventType,
    JobSpec,
    JobStage,
    JobStatus,
    QueueFullError,
    QueueSnapshot,
    RestartState,
    ValidationError,
)
from app.core.job_queue import DISPATCHER_THREAD_NAME, JobQueue, _JobEntry

#: 各待機の上限（十分に長く、かつ失敗時に必ず終わる）
WAIT = 5.0
#: テスト用のポーリング間隔（ディスパッチ遅延の上限）
POLL = 0.01


# ---------------------------------------------------------------- フェイク


class FakeEngine:
    """テスト用 Execution Engine。イベントを任意に発行でき、実ファイルは作らない。

    JobQueue が使う操作: state / start / submit / poll_event / shutdown / restart。
    同時に2件 submit されたら `concurrent_submit` を立てて例外を投げる。

    `restart()` は RealEngine と同じ契約で振る舞う（P3 契約 §3）:
    実行中ジョブがあれば**先に** ERROR(fatal, worker_dead) を出し、その後に
    STAGE(loading_model) → READY を再送する。イベントキューは作り直さない。
    """

    def __init__(
        self, *, ready_on_start: bool = True, artifacts_dir: Path | None = None
    ) -> None:
        # DONE で報告する成果物の置き場（tmp_path 配下のみ）
        self.artifacts_dir = artifacts_dir
        self._lock = threading.Lock()
        self._events: queue.Queue[EngineEvent] = queue.Queue()
        self._submitted: queue.Queue[str] = queue.Queue()
        self._restarted: queue.Queue[int] = queue.Queue()
        self._state = EngineState.STARTING
        self._active: str | None = None
        self._ready_on_start = ready_on_start

        self.submitted_ids: list[str] = []
        self.submit_errors: dict[str, Exception] = {}
        self.poll_error: Exception | None = None
        self.start_calls = 0
        self.shutdown_calls = 0
        self.restart_calls = 0
        self.concurrent_submit = False
        #: restart() で毎回投げる例外（None なら投げない）
        self.restart_error: Exception | None = None
        #: True なら restart() 後も DEAD のまま（再起動失敗の再現）
        self.restart_dead = False
        #: True なら restart() が READY を出さずに初期化中で止まる（READY 待ちの検証用）
        self.restart_hold = False

    # --- JobQueue から呼ばれる ---

    def state(self) -> EngineState:
        with self._lock:
            return self._state

    def start(self) -> None:
        self.start_calls += 1
        if self._ready_on_start:
            self.become_ready()

    def submit(self, spec: JobSpec) -> None:
        with self._lock:
            if self._active is not None:
                self.concurrent_submit = True
                raise AssertionError(
                    f"同時に2件 submit されました: {self._active} / {spec.job_id}"
                )
            self._active = spec.job_id
            self._state = EngineState.BUSY
            self.submitted_ids.append(spec.job_id)
        self._submitted.put(spec.job_id)
        error = self.submit_errors.pop(spec.job_id, None)
        if error is not None:
            self._release()
            raise error

    def poll_event(self, timeout: float | None = None) -> EngineEvent | None:
        if self.poll_error is not None:
            error, self.poll_error = self.poll_error, None
            raise error
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self, timeout: float = 5.0) -> None:
        self.shutdown_calls += 1
        with self._lock:
            self._state = EngineState.HALTED

    def restart(self) -> None:
        """RealEngine.restart() と同じ契約（中断 ERROR を先に出してから再初期化）。"""
        with self._lock:
            self.restart_calls += 1
            count = self.restart_calls
            active, self._active = self._active, None
            self._state = EngineState.STARTING
        if active is not None:
            self._events.put(
                EngineEvent(
                    type=EventType.ERROR,
                    job_id=active,
                    fatal=True,
                    category=ErrorCategory.WORKER_DEAD,
                    message="生成ワーカーの再起動により生成を中断しました",
                )
            )
        self._restarted.put(count)
        if self.restart_error is not None:
            raise self.restart_error
        if self.restart_dead:
            with self._lock:
                self._state = EngineState.DEAD
            return
        self._events.put(
            EngineEvent(type=EventType.STAGE, stage=JobStage.LOADING_MODEL)
        )
        if self.restart_hold:
            return  # READY は出さない（テストが become_ready() を呼ぶまで待つ）
        self.become_ready()

    # --- テスト用ヘルパ ---

    def wait_restart(self, timeout: float = WAIT) -> int:
        try:
            return self._restarted.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("ワーカーの再起動を待てませんでした") from None

    def assert_no_restart(self, timeout: float = 0.2) -> None:
        try:
            count = self._restarted.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError(f"再起動されないはずのワーカーが再起動しました: {count}回目")

    def _release(self) -> None:
        with self._lock:
            self._active = None
            self._state = EngineState.READY

    def set_state(self, state: EngineState) -> None:
        with self._lock:
            self._state = state

    def become_ready(self) -> None:
        with self._lock:
            self._state = EngineState.READY
        self._events.put(EngineEvent(type=EventType.READY, backend_id="minimax_h3"))

    def emit(self, event: EngineEvent, *, release: bool = False) -> None:
        """任意のイベントを発行する。release=True で実行中扱いを解いて READY に戻す。"""
        if release:
            self._release()
        self._events.put(event)

    def wait_submitted(self, timeout: float = WAIT) -> str:
        try:
            return self._submitted.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("エンジンへのジョブ投入を待てませんでした") from None

    def assert_no_submit(self, timeout: float = 0.2) -> None:
        try:
            job_id = self._submitted.get(timeout=timeout)
        except queue.Empty:
            return
        raise AssertionError(f"投入されないはずのジョブが投入されました: {job_id}")

    def finish_success(
        self,
        job_id: str,
        *,
        output_path: Path | None = None,
        last_frame_path: Path | None = None,
        seed_used: int = 12345,
        elapsed_sec: float = 1.5,
    ) -> None:
        """DONE を発行する。

        実機・モックとも「昇格済みの実ファイル」を報告してから DONE を出すため、
        既定でも実ファイルを作る（JobQueue は存在しない成果物を SUCCESS にしない）。
        """
        if output_path is None and self.artifacts_dir is not None:
            output_path = self.artifacts_dir / f"{job_id}.mp4"
            last_frame_path = last_frame_path or (
                self.artifacts_dir / f"{job_id}_last.png"
            )
        for path, content in ((output_path, b"fake mp4"), (last_frame_path, b"fake png")):
            if path is not None and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        self._release()
        self._events.put(
            EngineEvent(
                type=EventType.DONE,
                job_id=job_id,
                output_path=output_path,
                last_frame_path=last_frame_path,
                seed_used=seed_used,
                elapsed_sec=elapsed_sec,
            )
        )

    def finish_error(
        self,
        job_id: str,
        *,
        message: str = "モック失敗を検出しました",
        category: ErrorCategory = ErrorCategory.INPUT,
        fatal: bool = False,
    ) -> None:
        self._release()
        self._events.put(
            EngineEvent(
                type=EventType.ERROR,
                job_id=job_id,
                message=message,
                category=category,
                fatal=fatal,
            )
        )


class FakeRecorder:
    """呼び出しを記録するだけの JobRecorder。"""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, name: str, job_id: str, **kwargs) -> None:
        with self._cv:
            self.calls.append((name, job_id, kwargs))
            self._cv.notify_all()

    # --- JobRecorder プロトコル ---

    def on_queued(self, spec: JobSpec, queued_at: datetime) -> None:
        self._record("queued", spec.job_id, queued_at=queued_at)

    def on_running(self, job_id: str, started_at: datetime) -> None:
        self._record("running", job_id, started_at=started_at)

    def on_success(self, job_id: str, **kwargs) -> None:
        self._record("success", job_id, **kwargs)

    def on_failed(self, job_id: str, **kwargs) -> None:
        self._record("failed", job_id, **kwargs)

    def on_canceled(self, job_id: str, canceled_at: datetime) -> None:
        self._record("canceled", job_id, canceled_at=canceled_at)

    # --- テスト用ヘルパ ---

    def names(self, job_id: str) -> list[str]:
        with self._cv:
            return [name for name, jid, _ in self.calls if jid == job_id]

    def kwargs_of(self, name: str, job_id: str) -> dict:
        with self._cv:
            for n, jid, kw in self.calls:
                if n == name and jid == job_id:
                    return kw
        raise AssertionError(f"{name}({job_id}) は記録されていません")

    def count(self, name: str, job_id: str | None = None) -> int:
        with self._cv:
            return sum(
                1
                for n, jid, _ in self.calls
                if n == name and (job_id is None or jid == job_id)
            )

    def wait_for(self, name: str, job_id: str, timeout: float = WAIT) -> dict:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for n, jid, kw in self.calls:
                    if n == name and jid == job_id:
                        return kw
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"{name}({job_id}) を待てませんでした: 記録済み="
                        f"{[(n, j) for n, j, _ in self.calls]}"
                    )
                self._cv.wait(remaining)


class RecordingSleep:
    """注入用の sleep。実時間を待たず、要求された秒数だけを記録する。

    JobQueue の契約: 戻り値 True = そのまま経過 / False = 停止要求で中断。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> bool:
        with self._cv:
            self.calls.append(seconds)
            self._cv.notify_all()
        return True

    def wait_calls(self, count: int, timeout: float = WAIT) -> list[float]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while len(self.calls) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"バックオフ待機が {count} 回に達しません: {self.calls}"
                    )
                self._cv.wait(remaining)
            return list(self.calls)


class BlockingSleep(RecordingSleep):
    """バックオフ待機中の割り込み（手動再起動・停止要求）を試すための sleep。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = True

    def __call__(self, seconds: float) -> bool:
        with self._cv:
            self.calls.append(seconds)
            self._cv.notify_all()
        self.entered.set()
        if not self.release.wait(WAIT):  # pragma: no cover - 失敗時のハング防止
            raise AssertionError("バックオフ待機の解除を待てませんでした")
        return self.result


class FakeClock:
    """注入用の単調時計（watchdog 検証。実時間を進めない）。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self._lock = threading.Lock()
        self._value = start

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds


class BrokenRecorder(FakeRecorder):
    """すべての通知で例外を投げるレコーダ（キューが止まらないことの確認用）。"""

    def _record(self, name: str, job_id: str, **kwargs) -> None:
        super()._record(name, job_id, **kwargs)
        raise RuntimeError(f"履歴の書き込みに失敗しました: {name}")


# ---------------------------------------------------------------- 補助


def make_spec(tmp_path: Path, job_id: str, **overrides) -> JobSpec:
    params = dict(
        job_id=job_id,
        prompt=f"テスト用プロンプト {job_id}",
        num_frames=56,
        steps=4,
        seed_requested=42,
        output_path=tmp_path / "outputs" / f"{job_id}.mp4",
        last_frame_path=tmp_path / "outputs" / f"{job_id}_last.png",
    )
    params.update(overrides)
    return JobSpec(**params)  # type: ignore[arg-type]


def wait_until(predicate, timeout: float = WAIT, message: str = "条件を満たしませんでした"):
    """述語が真になるまで待つ（必ずタイムアウトする）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError(message)


@pytest.fixture
def engine(tmp_path) -> FakeEngine:
    # 成果物は tmp_path 配下にのみ作る（プロジェクトの data/ には触れない）
    return FakeEngine(artifacts_dir=tmp_path / "outputs")


@pytest.fixture
def recorder() -> FakeRecorder:
    return FakeRecorder()


@pytest.fixture
def fast_sleep() -> RecordingSleep:
    """既定の注入 sleep（実時間のバックオフを一切待たない）。"""
    return RecordingSleep()


@pytest.fixture
def make_queue(fast_sleep):
    """テスト終了時に必ず shutdown する JobQueue ファクトリ。

    バックオフを実時間で待たないよう、既定で `sleep` を注入する
    （テスト側で明示的に渡した場合はそちらを使う）。
    """
    created: list[JobQueue] = []

    def _make(engine, recorder, **kwargs) -> JobQueue:
        kwargs.setdefault("poll_interval", POLL)
        kwargs.setdefault("sleep", fast_sleep)
        q = JobQueue(engine, recorder, **kwargs)
        created.append(q)
        return q

    yield _make
    for q in created:
        q.shutdown(timeout=WAIT)


# ---------------------------------------------------------------- 直列性・順序


def test_three_jobs_run_in_fifo_order(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    for i in (1, 2, 3):
        q.submit(make_spec(tmp_path, f"j{i}"))

    for i in (1, 2, 3):
        job_id = engine.wait_submitted()
        assert job_id == f"j{i}", "FIFO 順で処理されていません"
        engine.finish_success(job_id)
        recorder.wait_for("success", job_id)

    assert engine.submitted_ids == ["j1", "j2", "j3"]
    assert engine.concurrent_submit is False
    snap = q.snapshot()
    assert snap.succeeded_total == 3
    assert snap.accepted_total == 3
    assert snap.queue_size == 0
    assert snap.current is None
    assert snap.last_finished is not None and snap.last_finished.job_id == "j3"


def test_only_one_job_runs_at_a_time(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    for i in (1, 2, 3):
        q.submit(make_spec(tmp_path, f"j{i}"))

    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)
    # 1件目の完了通知を出さない間、2件目は絶対に投入されない
    engine.assert_no_submit()
    current = q.current_job()
    assert current is not None and current.status is JobStatus.RUNNING
    assert [v.status for v in q.queued_jobs()] == [JobStatus.QUEUED, JobStatus.QUEUED]

    engine.finish_success(job_id)
    recorder.wait_for("success", job_id)
    second = engine.wait_submitted()
    assert second == "j2"
    engine.assert_no_submit()
    assert engine.concurrent_submit is False


def test_queue_size_decreases_2_1_0(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    for i in (1, 2, 3):
        q.submit(make_spec(tmp_path, f"j{i}"))

    recorder.wait_for("running", "j1")
    assert q.queue_size() == 2

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("running", "j2")
    assert q.queue_size() == 1

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("running", "j3")
    assert q.queue_size() == 0


def test_recorder_notification_order(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")
    engine.finish_error(engine.wait_submitted())
    recorder.wait_for("failed", "j2")

    assert recorder.names("j1") == ["queued", "running", "success"]
    assert recorder.names("j2") == ["queued", "running", "failed"]


# ---------------------------------------------------------------- 取消


def test_cancel_queued_job(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))
    recorder.wait_for("running", "j1")

    assert q.cancel_queued("j2") is True
    recorder.wait_for("canceled", "j2")
    assert q.queue_size() == 0
    assert [v.job_id for v in q.queued_jobs()] == []

    # 取消済みは処理対象から外れ、後から積んだ j3 が次に走る
    q.submit(make_spec(tmp_path, "j3"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")
    assert engine.wait_submitted() == "j3"
    assert "j2" not in engine.submitted_ids


def test_cancel_running_job_is_rejected(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.wait_submitted()
    recorder.wait_for("running", "j1")

    assert q.cancel_queued("j1") is False
    current = q.current_job()
    assert current is not None and current.status is JobStatus.RUNNING
    assert recorder.count("canceled") == 0


def test_cancel_unknown_and_finished_job_returns_false(
    tmp_path, engine, recorder, make_queue
):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")

    assert q.cancel_queued("j1") is False  # 終了済み
    assert q.cancel_queued("unknown") is False  # 未知ID
    assert recorder.count("canceled") == 0


def test_cancel_disabled_by_config(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder, allow_cancel_queued=False)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))
    recorder.wait_for("running", "j1")

    assert q.cancel_queued("j2") is False
    assert q.queue_size() == 1


# ---------------------------------------------------------------- 受付の検証


def test_queue_full_raises_japanese_error(tmp_path, recorder, make_queue):
    engine = FakeEngine(ready_on_start=False)  # READY にしないので待機列に溜まる
    q = make_queue(engine, recorder, max_queued_jobs=2)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    with pytest.raises(QueueFullError) as excinfo:
        q.submit(make_spec(tmp_path, "j3"))
    message = str(excinfo.value)
    assert "生成キューが上限" in message and "2件" in message
    assert q.queue_size() == 2
    assert recorder.count("queued") == 2


@pytest.mark.parametrize(
    "overrides, keyword",
    [
        ({"prompt": "   "}, "プロンプト"),
        ({"num_frames": 243}, "動画の長さ"),
        ({"steps": 5}, "ステップ数"),
        ({"seed_requested": -1}, "シード値"),
        ({"seed_requested": 2_147_483_648}, "シード値"),
        ({"backend_id": "unknown_model"}, "生成バックエンド"),
        ({"width": 1024}, "解像度"),
        ({"fps": 30}, "fps"),
    ],
)
def test_submit_rejects_invalid_spec(
    tmp_path, engine, recorder, make_queue, overrides, keyword
):
    q = make_queue(engine, recorder)
    q.start()
    with pytest.raises(ValidationError) as excinfo:
        q.submit(make_spec(tmp_path, "bad", **overrides))
    assert keyword in str(excinfo.value)
    assert q.queue_size() == 0
    assert recorder.count("queued") == 0
    engine.assert_no_submit()


def test_submit_rejects_duplicate_job_id(tmp_path, recorder, make_queue):
    engine = FakeEngine(ready_on_start=False)
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    with pytest.raises(ValidationError) as excinfo:
        q.submit(make_spec(tmp_path, "j1"))
    assert "同じID" in str(excinfo.value)
    assert q.queue_size() == 1


def test_submit_returns_queued_view_without_blocking(tmp_path, recorder, make_queue):
    engine = FakeEngine(ready_on_start=False)
    q = make_queue(engine, recorder)
    q.start()
    view = q.submit(make_spec(tmp_path, "j1", seed_requested=7))

    assert view.job_id == "j1"
    assert view.status is JobStatus.QUEUED
    assert view.seed_requested == 7
    assert view.duration_label == "2.33秒"
    assert view.queued_at is not None
    assert view.finished_at is None
    engine.assert_no_submit()  # 生成完了を待たずに戻っている


# ---------------------------------------------------------------- 失敗しても止まらない


def test_failed_job_does_not_block_the_queue(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    engine.finish_error(engine.wait_submitted(), message="[MOCK_FAIL] 指定による失敗")
    failed = recorder.wait_for("failed", "j1")
    assert "[MOCK_FAIL]" in failed["error"]
    assert failed["category"] == ErrorCategory.INPUT.value
    assert failed["finished_at"] is not None

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")
    snap = q.snapshot()
    assert snap.failed_total == 1 and snap.succeeded_total == 1


def test_engine_submit_exception_fails_job_and_continues(
    tmp_path, engine, recorder, make_queue
):
    engine.submit_errors["j1"] = RuntimeError("MPS backend out of memory")
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    assert engine.wait_submitted() == "j1"  # 投入直後に例外
    failed = recorder.wait_for("failed", "j1")
    assert "生成エンジンにジョブを渡せませんでした" in failed["error"]
    assert failed["category"] == ErrorCategory.PIPELINE.value

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")
    assert engine.submitted_ids == ["j1", "j2"]


def test_fatal_error_does_not_stop_the_queue(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    engine.finish_error(
        engine.wait_submitted(),
        message="Metal 内部エラー",
        category=ErrorCategory.MPS,
        fatal=True,
    )
    failed = recorder.wait_for("failed", "j1")
    assert failed["category"] == ErrorCategory.MPS.value

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")


def test_broken_recorder_does_not_stop_the_queue(tmp_path, engine, make_queue):
    recorder = BrokenRecorder()
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")

    assert engine.submitted_ids == ["j1", "j2"]
    assert q.snapshot().succeeded_total == 2


def test_poll_event_exception_fails_job_and_continues(
    tmp_path, engine, recorder, make_queue
):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))
    engine.wait_submitted()
    recorder.wait_for("running", "j1")

    engine._release()
    engine.poll_error = RuntimeError("ワーカーが応答しません")
    failed = recorder.wait_for("failed", "j1")
    assert failed["category"] == ErrorCategory.WORKER_DEAD.value

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")


# ---------------------------------------------------------------- 進捗・エンジン状態


def test_progress_and_stage_events_update_view(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)

    engine.emit(
        EngineEvent(type=EventType.STAGE, job_id=job_id, stage=JobStage.GENERATING)
    )
    engine.emit(EngineEvent(type=EventType.PROGRESS, job_id=job_id, step=2, total=4))

    def _progressed():
        current = q.current_job()
        return current if current is not None and current.step == 2 else None

    view = wait_until(_progressed, message="PROGRESS が反映されませんでした")
    assert view.stage is JobStage.GENERATING
    assert view.total_steps == 4
    assert view.status is JobStatus.RUNNING

    out = tmp_path / "outputs" / "j1.mp4"
    last = tmp_path / "outputs" / "j1_last.png"
    engine.finish_success(
        job_id, output_path=out, last_frame_path=last, seed_used=999, elapsed_sec=12.5
    )
    kwargs = recorder.wait_for("success", job_id)
    assert kwargs["output_path"] == out
    assert kwargs["last_frame_path"] == last
    assert kwargs["seed_used"] == 999
    assert kwargs["elapsed_sec"] == 12.5

    finished = q.snapshot().last_finished
    assert finished is not None
    assert finished.status is JobStatus.SUCCESS
    assert finished.output_path == out
    assert finished.seed_used == 999
    assert finished.elapsed_sec == 12.5
    assert finished.finished_at is not None


def test_engine_init_events_update_engine_state_only(recorder, make_queue):
    """初期化中の STAGE / READY は engine_state にだけ反映される（jobs に影響しない）。"""
    engine = FakeEngine(ready_on_start=False)
    q = make_queue(engine, recorder)
    q.start()

    engine.emit(EngineEvent(type=EventType.STAGE, stage=JobStage.LOADING_MODEL))
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.INITIALIZING_MODEL,
        message="engine_state が INITIALIZING_MODEL になりません",
    )
    engine.emit(EngineEvent(type=EventType.STAGE, stage=JobStage.LOADING_LORA))
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.INITIALIZING_LORA,
        message="engine_state が INITIALIZING_LORA になりません",
    )
    engine.emit(EngineEvent(type=EventType.READY, backend_id="minimax_h3"))
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.READY,
        message="engine_state が READY になりません",
    )
    # ジョブ側には一切影響しない
    snap = q.snapshot()
    assert snap.current is None
    assert snap.queued == ()
    assert snap.accepted_total == 0


def test_job_waits_until_engine_is_ready(tmp_path, recorder, make_queue):
    engine = FakeEngine(ready_on_start=False)
    engine.set_state(EngineState.INITIALIZING_MODEL)
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))

    engine.assert_no_submit()  # READY になるまでディスパッチしない
    assert q.current_job() is None
    assert [v.status for v in q.queued_jobs()] == [JobStatus.QUEUED]
    assert q.snapshot().engine_state is EngineState.INITIALIZING_MODEL

    engine.become_ready()
    assert engine.wait_submitted() == "j1"
    recorder.wait_for("running", "j1")
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.BUSY,
        message="実行中に engine_state が BUSY になりません",
    )


def test_stale_busy_state_is_corrected_while_idle(
    tmp_path, engine, recorder, make_queue
):
    """DONE の後に READY へ戻すエンジン（MockEngine と同順）でも BUSY 表示が残らない。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()

    # エンジンは BUSY のまま DONE を先に発行する（成果物は昇格済み）
    promoted = tmp_path / "outputs" / "j1.mp4"
    promoted.parent.mkdir(parents=True, exist_ok=True)
    promoted.write_bytes(b"fake mp4")
    engine.emit(
        EngineEvent(
            type=EventType.DONE,
            job_id=job_id,
            output_path=promoted,
            seed_used=1,
            elapsed_sec=0.5,
        )
    )
    recorder.wait_for("success", job_id)
    assert q.current_job() is None

    engine.set_state(EngineState.READY)  # 遅れて READY へ戻る
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.READY,
        message="待機中に engine_state が READY へ是正されません",
    )


def test_stray_event_after_finish_is_ignored(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")

    # 終了済みジョブへの重複イベントは無視され、通知も状態も増えない
    engine.finish_success("j1")
    engine.finish_error("j1")
    q.submit(make_spec(tmp_path, "j2"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")

    assert recorder.count("success", "j1") == 1
    assert recorder.count("failed", "j1") == 0
    assert q.snapshot().succeeded_total == 2


# ---------------------------------------------------------------- 状態遷移


def test_invalid_transitions_are_rejected(tmp_path):
    entry = _JobEntry(
        spec=make_spec(tmp_path, "j1"),
        status=JobStatus.QUEUED,
        queued_at=datetime.now(),
    )
    assert entry.transition(JobStatus.SUCCESS) is False  # QUEUED → SUCCESS は不正
    assert entry.status is JobStatus.QUEUED

    assert entry.transition(JobStatus.RUNNING) is True
    assert entry.transition(JobStatus.QUEUED) is False  # 逆行しない
    assert entry.transition(JobStatus.CANCELED) is False  # RUNNING の取消は不可

    assert entry.transition(JobStatus.SUCCESS) is True
    assert entry.transition(JobStatus.FAILED) is False  # 終了状態から動かない
    assert entry.status is JobStatus.SUCCESS


# ---------------------------------------------------------------- 不変性


def test_snapshot_is_immutable(tmp_path, recorder, make_queue):
    engine = FakeEngine(ready_on_start=False)
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    snap = q.snapshot()
    assert isinstance(snap, QueueSnapshot)
    assert isinstance(snap.queued, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.queue_size = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.queued[0].status = JobStatus.SUCCESS  # type: ignore[misc]

    jobs = q.queued_jobs()
    jobs.clear()  # 返り値のリストを壊しても内部は無事
    assert q.queue_size() == 2
    assert [v.job_id for v in q.snapshot().queued] == ["j1", "j2"]


# ---------------------------------------------------------------- 起動・停止


def test_start_is_idempotent(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.start()
    assert engine.start_calls == 1
    assert _dispatcher_threads() == 1

    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")


def test_shutdown_stops_dispatcher_thread(tmp_path, engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")
    assert q.snapshot().running is True

    q.shutdown(timeout=WAIT)
    assert _dispatcher_threads() == 0, "ディスパッチャスレッドが残っています"
    assert engine.shutdown_calls == 1
    assert q.snapshot().running is False

    q.shutdown(timeout=WAIT)  # 二重呼び出し安全
    assert engine.shutdown_calls == 1
    assert _dispatcher_threads() == 0


def test_shutdown_without_start_is_safe(engine, recorder, make_queue):
    q = make_queue(engine, recorder)
    q.shutdown(timeout=WAIT)
    assert engine.shutdown_calls == 1
    assert _dispatcher_threads() == 0
    with pytest.raises(RuntimeError):
        q.start()


def test_shutdown_while_job_running_returns_promptly(
    tmp_path, engine, recorder, make_queue
):
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.wait_submitted()
    recorder.wait_for("running", "j1")

    started = time.monotonic()
    q.shutdown(timeout=WAIT)  # DONE を出さないまま停止してもデッドロックしない
    assert time.monotonic() - started < WAIT
    assert _dispatcher_threads() == 0
    assert engine.shutdown_calls == 1
    # 停止中の投入は受け付けない
    with pytest.raises(ValidationError):
        q.submit(make_spec(tmp_path, "j2"))


def _dispatcher_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == DISPATCHER_THREAD_NAME)


# ---------------------------------------------------------------- 成果物の前提
# 「履歴 SUCCESS は成果物の正式昇格後のみ」（設計書 §10.7）を、キュー側でも守る。


def test_done_without_output_path_is_failed(tmp_path, engine, recorder, make_queue):
    """DONE が成果物を報告しない場合、予定パスで代替せず FAILED にする。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    engine.emit(
        EngineEvent(type=EventType.DONE, job_id=job_id, elapsed_sec=1.0), release=True
    )

    kwargs = recorder.wait_for("failed", job_id)
    assert "出力ファイルを報告しません" in kwargs["error"]
    assert not [n for n, _j, _k in recorder.calls if n == "success"]
    # 次のジョブは通常どおり処理される
    q.submit(make_spec(tmp_path, "j2"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")


def test_done_with_missing_file_is_failed(tmp_path, engine, recorder, make_queue):
    """報告された成果物が実在しない場合も SUCCESS にしない。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    engine.emit(
        EngineEvent(
            type=EventType.DONE,
            job_id=job_id,
            output_path=tmp_path / "outputs" / "存在しない.mp4",
            elapsed_sec=1.0,
        ),
        release=True,
    )
    kwargs = recorder.wait_for("failed", job_id)
    assert "見つかりません" in kwargs["error"]


def test_done_without_job_id_is_not_treated_as_success(
    tmp_path, engine, recorder, make_queue
):
    """job_id を欠いた DONE を実行中ジョブの成功として扱わない。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    artifact = tmp_path / "outputs" / "j1.mp4"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"fake mp4")

    engine.emit(EngineEvent(type=EventType.DONE, output_path=artifact, elapsed_sec=1.0))
    time.sleep(0.3)
    assert not [n for n, _j, _k in recorder.calls if n == "success"]
    assert q.current_job() is not None  # まだ実行中扱いのまま

    engine.finish_success(job_id)  # 正しい job_id なら成功する
    recorder.wait_for("success", job_id)


# ================================================================ P3
# 自動再起動・停滞監視・空き容量ガード（設計書 §13.2・§13.3、P3契約 §2）
# 実時間の 5秒/30秒は待たない（sleep と monotonic を注入して検証する）。


def wait_current(q: JobQueue, predicate, message: str):
    """実行中ジョブが条件を満たすまで待ち、そのビューを返す。"""

    def _check():
        view = q.current_job()
        return view if view is not None and predicate(view) else None

    return wait_until(_check, message=message)


def fail_fatal(
    engine: FakeEngine,
    recorder: FakeRecorder,
    job_id: str,
    *,
    category: ErrorCategory = ErrorCategory.MPS,
    message: str = "Metal 内部エラー",
) -> dict:
    """fatal エラーで1件失敗させ、履歴の failed 通知を待つ。"""
    engine.finish_error(job_id, message=message, category=category, fatal=True)
    return recorder.wait_for("failed", job_id)


# ---------------------------------------------------------------- 自動再起動


def test_fatal_error_restarts_worker_and_continues(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """fatal → 実行中ジョブ FAILED（自動再実行なし）→ バックオフ → 再起動 → 次へ進む。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    failed = fail_fatal(engine, recorder, engine.wait_submitted())
    assert failed["category"] == ErrorCategory.MPS.value

    assert engine.wait_restart() == 1
    assert fast_sleep.wait_calls(1) == [5.0], "1回目のバックオフは5秒"

    assert engine.wait_submitted() == "j2"
    assert engine.submitted_ids.count("j1") == 1, "失敗したジョブを自動再実行しない"
    engine.finish_success("j2")
    recorder.wait_for("success", "j2")

    snap = q.snapshot()
    assert snap.restart_state is RestartState.IDLE
    assert snap.restart_total == 1
    assert snap.consecutive_failures == 0
    assert snap.halted_reason is None


def test_non_fatal_error_does_not_restart_worker(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """非 fatal（input）は当該ジョブのみ FAILED。再起動もカウントもしない（契約 §2.1-7）。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    engine.finish_error(
        engine.wait_submitted(),
        message="キーフレーム画像を開けません",
        category=ErrorCategory.INPUT,
        fatal=False,
    )
    recorder.wait_for("failed", "j1")

    engine.assert_no_restart()
    assert fast_sleep.calls == []
    assert engine.restart_calls == 0
    snap = q.snapshot()
    assert snap.consecutive_failures == 0
    assert snap.restart_state is RestartState.IDLE

    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j2")


def test_backoff_is_5_then_30_seconds(tmp_path, engine, recorder, make_queue, fast_sleep):
    """バックオフ秒は restart_backoff_sec[min(n-1, len-1)]（1回目5秒・2回目以降30秒）。"""
    q = make_queue(engine, recorder, max_auto_restarts=3)
    q.start()
    for i in (1, 2, 3):
        q.submit(make_spec(tmp_path, f"j{i}"))

    for i in (1, 2, 3):
        job_id = engine.wait_submitted()
        assert job_id == f"j{i}"
        fail_fatal(engine, recorder, job_id)
        assert engine.wait_restart() == i

    assert fast_sleep.wait_calls(3) == [5.0, 30.0, 30.0]


def test_success_resets_consecutive_failures(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """ジョブが1本 SUCCESS したら連続失敗カウントは 0 に戻る（契約 §2.1-4）。"""
    q = make_queue(engine, recorder)
    q.start()
    for i in (1, 2, 3):
        q.submit(make_spec(tmp_path, f"j{i}"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    engine.wait_restart()
    wait_until(
        lambda: q.snapshot().consecutive_failures == 1,
        message="連続失敗カウントが 1 になりません",
    )

    engine.finish_success(engine.wait_submitted())  # j2 成功
    recorder.wait_for("success", "j2")
    wait_until(
        lambda: q.snapshot().consecutive_failures == 0,
        message="成功後に連続失敗カウントが 0 に戻りません",
    )

    fail_fatal(engine, recorder, engine.wait_submitted())  # j3 も fatal
    engine.wait_restart()
    assert fast_sleep.wait_calls(2) == [5.0, 5.0], "リセット後は再び1回目扱い（5秒）"


def test_halted_after_exceeding_max_auto_restarts(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """上限超過で HALTED。待機ジョブは QUEUED のまま保持する（捨てない・失敗させない）。"""
    q = make_queue(engine, recorder, max_auto_restarts=2)
    q.start()
    for i in (1, 2, 3, 4):
        q.submit(make_spec(tmp_path, f"j{i}"))

    for i in (1, 2):
        fail_fatal(engine, recorder, engine.wait_submitted())
        assert engine.wait_restart() == i
    fail_fatal(engine, recorder, engine.wait_submitted())  # 3回目 = 上限超過

    wait_until(
        lambda: q.snapshot().restart_state is RestartState.HALTED,
        message="上限超過で HALTED になりません",
    )
    snap = q.snapshot()
    assert snap.consecutive_failures == 3
    assert snap.halted_reason is not None
    assert "再起動" in snap.halted_reason and "停止" in snap.halted_reason
    assert "[ワーカーを再起動]" in snap.halted_reason

    engine.assert_no_restart()
    assert engine.restart_calls == 2, "上限を超えて再起動しない"
    engine.assert_no_submit()
    assert [(v.job_id, v.status) for v in q.queued_jobs()] == [("j4", JobStatus.QUEUED)]
    assert recorder.count("failed", "j4") == 0
    assert recorder.count("canceled", "j4") == 0
    assert fast_sleep.calls == [5.0, 30.0]


def test_no_job_starts_until_engine_is_ready_after_restart(
    tmp_path, engine, recorder, make_queue
):
    """再起動後は READY になるまで次のジョブを開始しない（契約 §2.1-2）。"""
    engine.restart_hold = True  # restart() は初期化中で止まる
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    engine.wait_restart()

    engine.assert_no_submit()  # READY 前に j2 を開始しない
    assert [v.status for v in q.queued_jobs()] == [JobStatus.QUEUED]
    assert q.snapshot().restart_state is RestartState.RESTARTING

    engine.become_ready()
    assert engine.wait_submitted() == "j2"
    wait_until(
        lambda: q.snapshot().restart_state is RestartState.IDLE,
        message="READY 到達後も RESTARTING のままです",
    )


def test_no_dispatch_while_restart_is_pending(
    tmp_path, engine, recorder, make_queue
):
    """バックオフ待機中（エンジンが READY でも）次のジョブを開始しない。"""
    blocking = BlockingSleep()
    q = make_queue(engine, recorder, sleep=blocking)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert blocking.entered.wait(WAIT), "バックオフに入りません"
    assert engine.state() is EngineState.READY  # エンジン自体は受け付けられる状態

    engine.assert_no_submit()
    assert q.snapshot().restart_state is RestartState.BACKOFF

    blocking.release.set()
    engine.wait_restart()
    assert engine.wait_submitted() == "j2"


def test_backoff_remaining_sec_is_reported(tmp_path, engine, recorder, make_queue):
    """UI 表示用の backoff_remaining_sec が減っていく（注入した単調時計で検証）。"""
    clock = FakeClock()
    blocking = BlockingSleep()
    q = make_queue(engine, recorder, sleep=blocking, monotonic=clock)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert blocking.entered.wait(WAIT)
    assert q.snapshot().backoff_remaining_sec == pytest.approx(5.0)
    clock.advance(2.0)
    assert q.snapshot().backoff_remaining_sec == pytest.approx(3.0)
    clock.advance(10.0)
    assert q.snapshot().backoff_remaining_sec == 0.0

    blocking.release.set()
    engine.wait_restart()
    wait_until(
        lambda: q.snapshot().backoff_remaining_sec == 0.0
        and q.snapshot().restart_state is RestartState.IDLE,
        message="再起動後に BACKOFF 表示が残っています",
    )


def test_restart_exception_is_retried_then_halted(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """engine.restart() が例外を投げる場合も 2〜3 の規則で扱う（契約 §2.1-5）。"""
    engine.restart_error = RuntimeError("ワーカーを起動できません")
    q = make_queue(engine, recorder, max_auto_restarts=2)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert engine.wait_restart() == 1
    assert engine.wait_restart() == 2  # 失敗したので再試行

    wait_until(
        lambda: q.snapshot().restart_state is RestartState.HALTED,
        message="再起動失敗が続いても HALTED になりません",
    )
    assert fast_sleep.wait_calls(2) == [5.0, 30.0]
    assert [v.status for v in q.queued_jobs()] == [JobStatus.QUEUED]
    engine.assert_no_restart()


def test_restart_leaving_engine_dead_is_retried_then_halted(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """再起動しても DEAD のままなら再試行し、上限超過で HALTED になる。"""
    engine.restart_dead = True
    q = make_queue(engine, recorder, max_auto_restarts=2)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert engine.wait_restart() == 1
    assert engine.wait_restart() == 2

    wait_until(
        lambda: q.snapshot().restart_state is RestartState.HALTED,
        message="DEAD のままでも HALTED になりません",
    )
    assert q.snapshot().halted_reason is not None
    assert fast_sleep.wait_calls(2) == [5.0, 30.0]


def test_worker_dead_event_triggers_restart(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """RealEngine が合成する worker_dead は fatal として再起動経路に入る。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    job_id = engine.wait_submitted()
    engine.emit(
        EngineEvent(
            type=EventType.ERROR,
            job_id=job_id,
            fatal=True,
            category=ErrorCategory.WORKER_DEAD,
            message="生成ワーカーが異常終了しました（終了コード: -9）",
        ),
        release=True,
    )
    failed = recorder.wait_for("failed", job_id)
    assert failed["category"] == ErrorCategory.WORKER_DEAD.value

    assert engine.wait_restart() == 1
    assert fast_sleep.wait_calls(1) == [5.0]
    assert engine.wait_submitted() == "j2"


def test_dead_engine_without_event_triggers_restart(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """実行中ジョブが無いままワーカーが死んだ場合（イベントが来ない）も再起動する。"""
    q = make_queue(engine, recorder)
    q.start()
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.READY,
        message="READY になりません",
    )

    engine.set_state(EngineState.DEAD)
    assert engine.wait_restart() == 1
    assert fast_sleep.wait_calls(1) == [5.0]

    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")


# ---------------------------------------------------------------- 手動再起動


def test_manual_restart_while_idle(tmp_path, engine, recorder, make_queue, fast_sleep):
    """待機中の手動再起動: バックオフなしで即座に再起動し、その後も生成できる。"""
    q = make_queue(engine, recorder)
    q.start()
    wait_until(
        lambda: q.snapshot().engine_state is EngineState.READY,
        message="READY になりません",
    )

    assert q.restart_worker() is True
    assert engine.wait_restart() == 1
    assert fast_sleep.calls == [], "手動再起動はバックオフしない"

    wait_until(
        lambda: q.snapshot().restart_state is RestartState.IDLE,
        message="手動再起動後に IDLE へ戻りません",
    )
    assert q.snapshot().restart_total == 1

    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")


def test_manual_restart_fails_running_job(tmp_path, engine, recorder, make_queue):
    """実行中の手動再起動: ジョブは終端イベントなしで消えず FAILED になる。"""
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))
    engine.wait_submitted()
    recorder.wait_for("running", "j1")

    assert q.restart_worker(reason="手動再起動") is True
    failed = recorder.wait_for("failed", "j1")
    assert failed["category"] == ErrorCategory.WORKER_DEAD.value
    assert "中断" in failed["error"]

    last = q.snapshot().last_finished
    assert last is not None and last.job_id == "j1"
    assert last.status is JobStatus.FAILED

    assert engine.wait_restart() == 1
    assert engine.wait_submitted() == "j2"
    engine.finish_success("j2")
    recorder.wait_for("success", "j2")

    # engine.restart() が後追いで出す中断 ERROR で二重に再起動しない
    engine.assert_no_restart()
    assert engine.restart_calls == 1
    assert q.snapshot().consecutive_failures == 0
    assert recorder.count("failed", "j1") == 1


def test_manual_restart_during_backoff_is_deduplicated(
    tmp_path, engine, recorder, make_queue
):
    """バックオフ中の手動再起動: 二重呼び出しでも再起動は1回・カウントは 0 に戻る。"""
    blocking = BlockingSleep()
    q = make_queue(engine, recorder, sleep=blocking)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert blocking.entered.wait(WAIT), "バックオフに入りません"
    assert q.snapshot().consecutive_failures == 1

    assert q.restart_worker() is True
    assert q.restart_worker() is True  # 二重呼び出し安全
    blocking.release.set()

    assert engine.wait_restart() == 1
    assert engine.wait_submitted() == "j2"
    engine.finish_success("j2")
    recorder.wait_for("success", "j2")
    engine.assert_no_restart()
    assert engine.restart_calls == 1
    assert q.snapshot().consecutive_failures == 0


def test_manual_restart_recovers_from_halted(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """HALTED からの復帰手段。カウントを 0 に戻し、待機ジョブの処理を再開する。"""
    q = make_queue(engine, recorder, max_auto_restarts=1)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))
    q.submit(make_spec(tmp_path, "j3"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    engine.wait_restart()
    fail_fatal(engine, recorder, engine.wait_submitted())  # 上限超過
    wait_until(
        lambda: q.snapshot().restart_state is RestartState.HALTED,
        message="HALTED になりません",
    )
    assert [v.job_id for v in q.queued_jobs()] == ["j3"]

    assert q.restart_worker() is True
    assert engine.wait_restart() == 2
    wait_until(
        lambda: q.snapshot().restart_state is RestartState.IDLE,
        message="手動再起動で HALTED から復帰しません",
    )
    snap = q.snapshot()
    assert snap.consecutive_failures == 0
    assert snap.halted_reason is None

    assert engine.wait_submitted() == "j3"
    engine.finish_success("j3")
    recorder.wait_for("success", "j3")


def test_manual_restart_after_shutdown_returns_false(engine, recorder, make_queue):
    """未開始・停止後の手動再起動は False（例外にしない）。"""
    q = make_queue(engine, recorder)
    assert q.restart_worker() is False, "未開始のキューは再起動できない"

    q.start()
    q.shutdown(timeout=WAIT)
    assert q.restart_worker() is False
    assert engine.restart_calls == 0


def test_shutdown_during_backoff_returns_immediately(
    tmp_path, engine, recorder, make_queue
):
    """バックオフ待機中に停止要求が来たら即座に抜け、再起動を開始しない（契約 §2.1-6）。"""
    holder: dict[str, JobQueue] = {}
    entered = threading.Event()
    calls: list[float] = []

    def stop_aware_sleep(seconds: float) -> bool:
        # 既定実装と同じ意味（停止要求で False を返す）を実時間なしで再現する
        calls.append(seconds)
        entered.set()
        return not holder["q"]._stop.wait(WAIT)

    q = make_queue(engine, recorder, sleep=stop_aware_sleep)
    holder["q"] = q
    q.start()
    q.submit(make_spec(tmp_path, "j1"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert entered.wait(WAIT), "バックオフに入りません"
    assert calls == [5.0]

    started = time.monotonic()
    q.shutdown(timeout=WAIT)
    assert time.monotonic() - started < WAIT / 2, "停止要求で即座に抜けていません"
    assert engine.restart_calls == 0, "停止処理中に再起動を開始しない"
    assert _dispatcher_threads() == 0


# ---------------------------------------------------------------- watchdog（§13.2）
# 最優先事項: 正常な生成を絶対に止めない。判定は総経過時間 対 目安時間×係数で行い、
# イベント間隔では判定しない（実機は 4/4 到達後に約150秒イベントが来ない）。


def _estimate_400(num_frames: int, steps: int) -> float:
    """実機実測に近い目安（56f/4step ≒ 400秒）。"""
    return 400.0


def test_watchdog_does_not_flag_normal_generation(
    tmp_path, engine, recorder, make_queue
):
    """4/4 到達後に約150秒イベントが来なくても停滞扱いしない（誤検知しない）。"""
    clock = FakeClock()
    q = make_queue(engine, recorder, estimate_fn=_estimate_400, monotonic=clock)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)

    # 4/4 まで進み、その後イベントが来ないまま 150秒経過（実機の正常な挙動）
    engine.emit(EngineEvent(type=EventType.PROGRESS, job_id=job_id, step=4, total=4))
    wait_current(q, lambda v: v.step == 4, "PROGRESS が反映されません")
    clock.advance(250.0)
    time.sleep(0.1)  # ポーリングを何周もさせる（POLL=0.01）
    clock.advance(300.0)  # 合計 550秒 = 目安400秒は超えるが警告閾値(1200秒)未満
    time.sleep(0.1)

    current = q.current_job()
    assert current is not None
    assert current.status is JobStatus.RUNNING
    assert current.stalled is False, "正常な生成を停滞扱いしてはいけない"
    assert recorder.count("failed") == 0
    assert engine.restart_calls == 0

    engine.finish_success(job_id)
    recorder.wait_for("success", job_id)


def test_watchdog_warns_but_never_stops(tmp_path, engine, recorder, make_queue):
    """警告閾値超過では stalled=True にするだけ。生成は止めない（§13.2）。"""
    clock = FakeClock()
    q = make_queue(engine, recorder, estimate_fn=_estimate_400, monotonic=clock)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)

    clock.advance(1300.0)  # 400秒 × 3.0 = 1200秒 超え
    wait_current(q, lambda v: v.stalled is True, "停滞警告フラグが立ちません")

    current = q.current_job()
    assert current is not None and current.status is JobStatus.RUNNING
    assert recorder.count("failed") == 0
    engine.assert_no_restart()

    # 警告のまま完走できる（止めていない証明）
    engine.finish_success(job_id)
    recorder.wait_for("success", job_id)
    assert q.snapshot().succeeded_total == 1


def test_watchdog_aborts_only_when_abort_factor_enabled(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """stall_abort_factor > 0 のときだけ FAILED（pipeline・日本語）＋再起動する。"""
    clock = FakeClock()
    q = make_queue(
        engine,
        recorder,
        estimate_fn=_estimate_400,
        stall_abort_factor=5.0,
        monotonic=clock,
    )
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))
    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)

    clock.advance(2100.0)  # 400秒 × 5.0 = 2000秒 超え
    failed = recorder.wait_for("failed", job_id)
    assert failed["category"] == ErrorCategory.PIPELINE.value
    assert "中断" in failed["error"] and "目安" in failed["error"]

    assert engine.wait_restart() == 1
    assert engine.wait_submitted() == "j2"


def test_watchdog_disabled_without_estimate_fn(tmp_path, engine, recorder, make_queue):
    """estimate_fn が None なら watchdog は完全に無効（stalled にしない）。"""
    clock = FakeClock()
    q = make_queue(engine, recorder, monotonic=clock)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)

    clock.advance(100_000.0)
    time.sleep(0.1)
    current = q.current_job()
    assert current is not None
    assert current.stalled is False
    assert current.status is JobStatus.RUNNING
    assert recorder.count("failed") == 0


def test_broken_estimate_fn_does_not_break_generation(
    tmp_path, engine, recorder, make_queue
):
    """estimate_fn が例外を投げても生成は続行する（watchdog は黙って無効化）。"""

    def broken(num_frames: int, steps: int) -> float:
        raise RuntimeError("目安の計算に失敗")

    q = make_queue(engine, recorder, estimate_fn=broken)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")
    assert q.current_job() is None


# ---------------------------------------------------------------- 空き容量ガード（§13.2）


class Guard:
    """intake_guard のフェイク（`reason` を差し替えて受付停止を再現する）。"""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self.error: Exception | None = None
        self.calls = 0

    def __call__(self) -> str | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.reason


DISK_FULL = (
    "空き容量が不足しているため、新しい生成を受け付けられません"
    "（残り 3.2GB / 必要 5GB 以上）。Finder で不要な動画を整理してください"
)


def test_submit_rejected_when_disk_is_full(tmp_path, engine, recorder, make_queue):
    """submit は ValidationError で拒否し、履歴レコードを作らない。"""
    guard = Guard(DISK_FULL)
    q = make_queue(engine, recorder, intake_guard=guard)
    q.start()

    with pytest.raises(ValidationError) as excinfo:
        q.submit(make_spec(tmp_path, "j1"))
    assert "空き容量が不足" in str(excinfo.value)
    assert q.queue_size() == 0
    assert recorder.count("queued") == 0
    engine.assert_no_submit()
    assert q.snapshot().intake_blocked_reason == DISK_FULL


def test_dispatch_holds_job_queued_while_disk_is_full(tmp_path, recorder, make_queue):
    """ディスパッチ直前でブロックされたジョブは QUEUED のまま保持する（失敗させない）。"""
    engine = FakeEngine(ready_on_start=False, artifacts_dir=tmp_path / "outputs")
    guard = Guard(None)
    q = make_queue(engine, recorder, intake_guard=guard)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))  # 受付時は空きがある

    guard.reason = DISK_FULL  # ディスパッチ直前に枯渇
    engine.become_ready()

    engine.assert_no_submit()
    assert [v.status for v in q.queued_jobs()] == [JobStatus.QUEUED]
    wait_until(
        lambda: q.snapshot().intake_blocked_reason == DISK_FULL,
        message="intake_blocked_reason が入りません",
    )
    assert recorder.count("failed") == 0
    assert recorder.count("canceled") == 0

    guard.reason = None  # 整理して空きが戻ったら処理が進む
    assert engine.wait_submitted() == "j1"
    engine.finish_success("j1")
    recorder.wait_for("success", "j1")
    wait_until(
        lambda: q.snapshot().intake_blocked_reason is None,
        message="受付停止の表示が消えません",
    )


def test_disk_guard_exception_does_not_block_generation(
    tmp_path, engine, recorder, make_queue
):
    """空き容量を確認できないことを理由に生成を止めない。"""
    guard = Guard(None)
    guard.error = OSError("statvfs に失敗しました")
    q = make_queue(engine, recorder, intake_guard=guard)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    engine.finish_success(engine.wait_submitted())
    recorder.wait_for("success", "j1")
    assert q.snapshot().intake_blocked_reason is None


# ---------------------------------------------------------------- スナップショット


def test_snapshot_p3_defaults_and_last_event_at(tmp_path, engine, recorder, make_queue):
    """新フィールドの既定値と last_event_at の更新。"""
    q = make_queue(engine, recorder)
    snap = q.snapshot()
    assert snap.restart_state is RestartState.IDLE
    assert snap.consecutive_failures == 0
    assert snap.backoff_remaining_sec == 0.0
    assert snap.halted_reason is None
    assert snap.restart_total == 0
    assert snap.intake_blocked_reason is None

    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    job_id = engine.wait_submitted()
    recorder.wait_for("running", job_id)
    started = q.current_job()
    assert started is not None and started.last_event_at is not None
    assert started.stalled is False

    engine.emit(EngineEvent(type=EventType.PROGRESS, job_id=job_id, step=2, total=4))
    view = wait_current(q, lambda v: v.step == 2, "PROGRESS が反映されません")
    assert view.last_event_at is not None
    assert view.last_event_at >= started.last_event_at

    with pytest.raises(dataclasses.FrozenInstanceError):
        view.stalled = True  # type: ignore[misc]


# ---------------------------------------------------------------- 既定の待機実装
# 上のテストは sleep を注入して実時間を待たない。ここだけは本番の待機実装
# （Event.wait ベース）が「停止要求・手動再起動で即座に抜ける」ことを確認する。


def test_default_backoff_sleep_returns_false_after_stop(engine, recorder):
    q = JobQueue(engine, recorder, poll_interval=POLL)  # sleep を注入しない＝本番実装
    assert q._default_sleep(0.01) is True

    started = time.monotonic()
    q.shutdown(timeout=WAIT)
    assert q._default_sleep(30.0) is False, "停止後は待たずに戻る"
    assert time.monotonic() - started < WAIT


def test_manual_restart_interrupts_default_backoff(tmp_path, engine, recorder):
    """本番の待機実装でも、手動再起動でバックオフ（30秒）を待たずに再起動する。"""
    q = JobQueue(
        engine, recorder, poll_interval=POLL, restart_backoff_sec=(30,)
    )  # sleep を注入しない
    try:
        q.start()
        q.submit(make_spec(tmp_path, "j1"))
        fail_fatal(engine, recorder, engine.wait_submitted())
        wait_until(
            lambda: q.snapshot().restart_state is RestartState.BACKOFF,
            message="バックオフに入りません",
        )

        started = time.monotonic()
        assert q.restart_worker() is True
        assert engine.wait_restart() == 1
        assert time.monotonic() - started < WAIT, "30秒待たずに再起動する"
        assert q.snapshot().consecutive_failures == 0
    finally:
        q.shutdown(timeout=WAIT)


def test_auto_restart_disabled_halts_and_manual_restart_recovers(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """auto_restart_worker=False（config）なら自動再起動せず HALTED。手動でのみ復帰する。"""
    q = make_queue(engine, recorder, auto_restart_worker=False)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    wait_until(
        lambda: q.snapshot().restart_state is RestartState.HALTED,
        message="自動再起動が無効でも HALTED になりません",
    )
    engine.assert_no_restart()
    assert fast_sleep.calls == []
    reason = q.snapshot().halted_reason
    assert reason is not None and "[ワーカーを再起動]" in reason
    assert [v.job_id for v in q.queued_jobs()] == ["j2"]

    # ワーカーが死んでいても手動再起動で復帰できる（DEAD 中の重複カウントもしない）
    engine.set_state(EngineState.DEAD)
    time.sleep(0.05)
    assert q.snapshot().consecutive_failures == 1, "HALTED 中に失敗カウントを増やさない"

    assert q.restart_worker() is True
    assert engine.wait_restart() == 1
    assert engine.wait_submitted() == "j2"
    engine.finish_success("j2")
    recorder.wait_for("success", "j2")
    snap = q.snapshot()
    assert snap.restart_state is RestartState.IDLE
    assert snap.halted_reason is None
    assert snap.consecutive_failures == 0


# ---------------------------------------------------------------- 旧ワーカーの残骸イベント
# RealEngine は起動失敗時に ERROR(fatal, worker_dead, job_id なし) をイベントキューへ残す
# （app/engine/real_engine.py の start() の OSError 経路）。再起動が成功した後に
# これを消費して、動き始めたばかりのワーカーを落とし直さないこと。


def test_stale_fatal_during_restart_does_not_restart_again(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    engine.restart_hold = True  # 再起動後は初期化中（STARTING）のまま
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))
    q.submit(make_spec(tmp_path, "j2"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert engine.wait_restart() == 1
    wait_until(
        lambda: q.snapshot().restart_state is RestartState.RESTARTING,
        message="RESTARTING になりません",
    )

    # 旧世代の残骸（job_id なしの fatal）。いまのワーカーは初期化中＝健全。
    engine.emit(
        EngineEvent(
            type=EventType.ERROR,
            fatal=True,
            category=ErrorCategory.WORKER_DEAD,
            message="生成ワーカーを起動できませんでした",
        )
    )
    engine.assert_no_restart()
    assert q.snapshot().consecutive_failures == 1
    assert fast_sleep.calls == [5.0]

    engine.become_ready()
    assert engine.wait_submitted() == "j2"


def test_fatal_while_engine_is_halted_during_restart_is_handled(
    tmp_path, engine, recorder, make_queue, fast_sleep
):
    """再起動後のワーカーが起動タイムアウト（HALTED）なら、現在の障害として再試行する。"""
    engine.restart_hold = True
    q = make_queue(engine, recorder)
    q.start()
    q.submit(make_spec(tmp_path, "j1"))

    fail_fatal(engine, recorder, engine.wait_submitted())
    assert engine.wait_restart() == 1
    wait_until(
        lambda: q.snapshot().restart_state is RestartState.RESTARTING,
        message="RESTARTING になりません",
    )

    engine.set_state(EngineState.HALTED)  # RealEngine._on_startup_timeout と同じ状態
    engine.emit(
        EngineEvent(
            type=EventType.ERROR,
            fatal=True,
            category=ErrorCategory.MODEL_STATE,
            message="モデルの初期化が 900 秒以内に完了しませんでした",
        )
    )
    assert engine.wait_restart() == 2, "初期化が進まないワーカーは再試行する"
    assert fast_sleep.wait_calls(2) == [5.0, 30.0]
    assert q.snapshot().consecutive_failures == 2
