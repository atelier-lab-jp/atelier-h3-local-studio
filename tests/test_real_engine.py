"""RealEngine のテスト（P2固定契約 §1〜§6・設計書 §10.7・§13.3・付録A/A.1）。

- **実モデルは一切起動しない**。すべて `tests/fixtures/fake_h3_worker.py` に対して行う。
- 書き込み先は必ず `tmp_path` 配下。プロジェクトの `data/` へは触れない。
- モック素材（`app/assets/mock/`）は読み取り（コピー元）のみ。
- すべての待機にタイムアウトを付け、失敗してもハングしない。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.config import load_config
from app.core.contracts import (
    MINIMAX_H3_CAPABILITIES,
    BackendIdentity,
    EngineBusyError,
    EngineEvent,
    EngineState,
    ErrorCategory,
    EventType,
    JobSpec,
    JobStage,
    ValidationError,
)
from app.engine.base import Engine
from app.engine.real_engine import THREAD_PREFIX, RealEngine
from tests.fixtures import FAKE_WORKER

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_ASSETS_DIR = PROJECT_ROOT / "app" / "assets" / "mock"

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax H3 NF4 Turbo",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)

READY_TIMEOUT = 30.0
JOB_TIMEOUT = 60.0
STATE_TIMEOUT = 20.0
#: 「これ以上イベントが来ないこと」を確かめる待ち時間
QUIET_SEC = 2.0


# ---------------------------------------------------------------- ヘルパ


@pytest.fixture
def paths(tmp_path):
    """data_root / working_directory / data_root 外ディレクトリを用意する。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return {
        "data_root": data_root,
        "outputs": data_root / "outputs",
        "work": work_dir,
        "outside": outside,
        "env_dump": tmp_path / "env_dump.json",
        "worker_log": data_root / "logs" / "worker.log",
        # 偽ワーカーの起動回数（プロセスをまたぐ再起動シナリオで使う）
        "state": tmp_path / "worker_state.txt",
        # ワーカーが受け取った keyframe_path（継続生成のワイヤ検証用）
        "keyframe_dump": tmp_path / "keyframe_dump.json",
    }


def received_keyframe(paths) -> dict:
    """偽ワーカーが最後の generate で受け取った keyframe_path を読む。"""
    return json.loads(paths["keyframe_dump"].read_text(encoding="utf-8"))


def write_png(path: Path, size: tuple[int, int] = (576, 320)) -> Path:
    """継続生成のキーフレーム（本物の PNG）を作る。"""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", size, (12, 34, 56)) as image:
        image.save(path, format="PNG")
    return path


def continuation_spec(paths, keyframe: Path, job_id: str = "v_child_0001", **overrides):
    """継続生成の JobSpec（contracts の排他条件を満たす形）。"""
    params = {
        "job_type": "continuation",
        "parent_id": "v_20260807_101530_ab3f",
        "keyframe_path": keyframe,
    }
    params.update(overrides)
    return make_spec(paths, job_id=job_id, **params)


def launch_count(paths) -> int:
    """偽ワーカーが何回起動したか（＝再起動が何回起きたか＋1）。"""
    try:
        return int(paths["state"].read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


@pytest.fixture
def make_engine(paths, monkeypatch):
    """偽ワーカーを起動する RealEngine を作る（後始末つき）。"""
    engines: list[RealEngine] = []

    def _make(scenario: str = "normal", *, env: dict | None = None, **overrides):
        monkeypatch.setenv("FAKE_WORKER_SCENARIO", scenario)
        monkeypatch.setenv("FAKE_WORKER_ASSETS", str(MOCK_ASSETS_DIR))
        monkeypatch.setenv("FAKE_WORKER_OUTSIDE_DIR", str(paths["outside"]))
        monkeypatch.setenv("FAKE_WORKER_ENV_DUMP", str(paths["env_dump"]))
        monkeypatch.setenv("FAKE_WORKER_FLOOD", "0")
        monkeypatch.setenv("FAKE_WORKER_STATE", str(paths["state"]))
        monkeypatch.setenv("FAKE_WORKER_BAD_RUNS", "1")
        monkeypatch.setenv("FAKE_WORKER_KEYFRAME_DUMP", str(paths["keyframe_dump"]))
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)

        params = {
            "identity": IDENTITY,
            "worker_python": Path(sys.executable),
            "worker_script": FAKE_WORKER,
            "working_directory": paths["work"],
            "data_root": paths["data_root"],
            "model_id": IDENTITY.model_id,
            "model_revision": IDENTITY.model_revision,
            "processor_id": "MiniMax/MiniMax-H3",
            "lora_path": paths["work"] / "models" / "loras" / "turbo.safetensors",
            "lora_alpha": 1.0,
            "worker_log_path": paths["worker_log"],
            "startup_timeout": 60.0,
            "shutdown_grace": 1.0,
        }
        params.update(overrides)
        engine = RealEngine(**params)
        engines.append(engine)
        return engine

    yield _make

    for engine in engines:
        engine.shutdown(timeout=5.0)


def make_spec(paths, job_id: str = "v_20260807_101530_ab3f", **overrides) -> JobSpec:
    outputs = paths["outputs"]
    params = {
        "job_id": job_id,
        "prompt": "花畑で少女が歌う。カメラはゆっくり前進する。",
        "num_frames": 56,
        "steps": 4,
        "seed_requested": 42,
        "output_path": outputs / f"{job_id}.mp4",
        "last_frame_path": outputs / f"{job_id}_last.png",
    }
    params.update(overrides)
    return JobSpec(**params)


def wait_for(
    engine: RealEngine,
    predicate,
    *,
    timeout: float,
    collected: list[EngineEvent] | None = None,
) -> EngineEvent:
    """条件を満たすイベントが来るまで待つ（必ずタイムアウトする）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = engine.poll_event(0.2)
        if event is None:
            continue
        if collected is not None:
            collected.append(event)
        if predicate(event):
            return event
    seen = [e.type.value for e in (collected or [])]
    raise AssertionError(
        f"期待したイベントが {timeout} 秒以内に届きませんでした（受信: {seen}）"
    )


def wait_type(engine, etype, *, timeout, collected=None) -> EngineEvent:
    return wait_for(
        engine, lambda e: e.type is etype, timeout=timeout, collected=collected
    )


def wait_terminal(engine, *, timeout=JOB_TIMEOUT, collected=None) -> EngineEvent:
    return wait_for(
        engine,
        lambda e: e.type in (EventType.DONE, EventType.ERROR),
        timeout=timeout,
        collected=collected,
    )


def wait_state(engine, *states, timeout=STATE_TIMEOUT) -> EngineState:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = engine.state()
        if state in states:
            return state
        time.sleep(0.05)
    raise AssertionError(
        f"状態が {[s.value for s in states]} になりません（現在: {engine.state().value}）"
    )


def drain(engine, timeout: float = 5.0) -> list[EngineEvent]:
    """キューが空になるまでイベントを取り出す。"""
    events: list[EngineEvent] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = engine.poll_event(0.05)
        if event is None:
            break
        events.append(event)
    return events


def quiet(engine, seconds: float = QUIET_SEC) -> list[EngineEvent]:
    """指定秒のあいだに届いたイベントを集める（何も来ないことの確認用）。"""
    events: list[EngineEvent] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        event = engine.poll_event(0.1)
        if event is not None:
            events.append(event)
    return events


def start_ready(engine, *, timeout=READY_TIMEOUT) -> list[EngineEvent]:
    collected: list[EngineEvent] = []
    engine.start()
    wait_type(engine, EventType.READY, timeout=timeout, collected=collected)
    return collected


def run_to_terminal(engine, spec, *, timeout=JOB_TIMEOUT) -> tuple:
    """READY まで進めて submit し、終端イベントまでのイベント列を返す。"""
    start_ready(engine)
    engine.submit(spec)
    collected: list[EngineEvent] = []
    terminal = wait_terminal(engine, timeout=timeout, collected=collected)
    return terminal, collected


def engine_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(THREAD_PREFIX)]


def settled_threads(expected: int, timeout: float = 5.0) -> list[str]:
    """エンジンのスレッド数が落ち着くのを待って、その名前を返す。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = [t.name for t in engine_threads()]
        if len(names) == expected:
            return names
        time.sleep(0.05)
    return [t.name for t in engine_threads()]


def types(events) -> list[str]:
    return [e.type.value for e in events]


# ---------------------------------------------------------------- 起動と契約


def test_worker_is_launched_with_contract_env_and_cwd(make_engine, paths):
    """契約 §1: argv・cwd・環境変数がそのままワーカーへ渡る。"""
    engine = make_engine("normal")
    start_ready(engine)

    dump = json.loads(paths["env_dump"].read_text(encoding="utf-8"))
    assert Path(dump["cwd"]).resolve() == paths["work"].resolve()
    assert Path(dump["argv"][0]).resolve() == FAKE_WORKER.resolve()

    env = dump["env"]
    assert env["DIFFSYNTH_SKIP_DOWNLOAD"] == "True"
    assert env["DIFFSYNTH_MODEL_BASE_PATH"] == str(paths["work"] / "models")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert Path(env["ATELIER_DATA_ROOT"]) == paths["data_root"].resolve()
    assert env["ATELIER_BACKEND_ID"] == "minimax_h3"
    assert env["ATELIER_MODEL_ID"] == IDENTITY.model_id
    assert env["ATELIER_MODEL_REVISION"] == IDENTITY.model_revision
    assert env["ATELIER_PROCESSOR_ID"] == "MiniMax/MiniMax-H3"
    assert env["ATELIER_LORA_PATH"].endswith("turbo.safetensors")
    assert env["ATELIER_LORA_ALPHA"] == "1.0"
    # MODELSCOPE_DOMAIN は設定しない（契約 §1）
    assert env["MODELSCOPE_DOMAIN"] is None


def test_satisfies_engine_protocol(make_engine):
    """P1 の Engine 契約（base.Engine）を満たす。"""
    engine = make_engine("normal")
    assert isinstance(engine, Engine)
    assert isinstance(engine.identity, BackendIdentity)
    assert isinstance(engine.state(), EngineState)
    for name in ("start", "submit", "poll_event", "shutdown", "restart"):
        assert callable(getattr(engine, name))


def test_start_is_idempotent(make_engine):
    engine = make_engine("normal")
    start_ready(engine)
    pid = engine.worker_pid
    engine.start()  # 二重呼び出しは無視される
    assert engine.worker_pid == pid


def test_handshake_reaches_ready(make_engine):
    """契約 §5: stage 2回 → ready（identity / capabilities 照合）。"""
    engine = make_engine("normal")
    collected = start_ready(engine)

    stages = [e for e in collected if e.type is EventType.STAGE]
    assert [e.stage for e in stages] == [JobStage.LOADING_MODEL, JobStage.LOADING_LORA]
    ready = collected[-1]
    assert ready.type is EventType.READY
    assert ready.backend_id == "minimax_h3"
    assert ready.capabilities == MINIMAX_H3_CAPABILITIES
    assert engine.state() is EngineState.READY
    assert engine.identity == IDENTITY
    assert engine.capabilities == MINIMAX_H3_CAPABILITIES


def test_from_config_resolves_worker_and_lora_paths(tmp_path):
    """config からの生成（実際の起動はしない・data_root は tmp へ差し替える）。"""
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp_path / "data")
    engine = RealEngine.from_config(cfg)

    assert engine.identity.backend_id == "minimax_h3"
    assert engine.identity.model_id == cfg.backend.model_id
    env = engine._build_env()
    assert env["ATELIER_LORA_PATH"] == str(
        cfg.backend.working_directory / cfg.backend.lora_relpath
    )
    assert env["ATELIER_PROCESSOR_ID"] == cfg.backend.processor_id
    assert env["DIFFSYNTH_MODEL_BASE_PATH"] == str(
        cfg.backend.working_directory / "models"
    )
    assert engine._worker_script == PROJECT_ROOT / cfg.backend.worker_script
    assert engine.state() is EngineState.STARTING
    assert engine.worker_pid is None


# ---------------------------------------------------------------- 正常系


def test_normal_job_promotes_artifacts_and_emits_done(make_engine, paths):
    """契約 §4: progress 4回 → done → 正式昇格。partial は残らない。"""
    engine = make_engine("normal")
    spec = make_spec(paths)
    terminal, collected = run_to_terminal(engine, spec)

    assert terminal.type is EventType.DONE, terminal.message
    progress = [e for e in collected if e.type is EventType.PROGRESS]
    assert [e.step for e in progress] == [1, 2, 3, 4]
    assert {e.total for e in progress} == {4}
    assert all(e.job_id == spec.job_id for e in progress)
    stages = [e.stage for e in collected if e.type is EventType.STAGE]
    assert JobStage.PREPARING in stages and JobStage.SAVING in stages

    assert terminal.job_id == spec.job_id
    assert terminal.output_path == spec.output_path
    assert terminal.last_frame_path == spec.last_frame_path
    assert terminal.seed_used == 42
    assert terminal.backend_id == "minimax_h3"
    assert terminal.model_id == IDENTITY.model_id
    assert terminal.model_revision == IDENTITY.model_revision
    assert terminal.elapsed_sec is not None and terminal.elapsed_sec >= 0
    assert terminal.warnings == ()

    assert spec.output_path.is_file() and spec.output_path.stat().st_size > 0
    assert spec.last_frame_path.is_file()
    assert list(paths["outputs"].glob("*.partial")) == []
    assert wait_state(engine, EngineState.READY) is EngineState.READY


def test_final_artifacts_do_not_exist_before_done(make_engine, paths):
    """DONE 前に正式成果物が存在しないこと（§10.7）。"""
    engine = make_engine("normal")
    spec = make_spec(paths)
    start_ready(engine)
    engine.submit(spec)

    deadline = time.monotonic() + JOB_TIMEOUT
    saw_done = False
    while time.monotonic() < deadline:
        event = engine.poll_event(0.1)
        if event is None:
            # DONE 前はどの瞬間でも正式名は存在してはならない
            assert not spec.output_path.exists()
            assert not spec.last_frame_path.exists()
            continue
        if event.type is EventType.DONE:
            saw_done = True
            break
        assert event.type is not EventType.ERROR, event.message
        assert not spec.output_path.exists()
        assert not spec.last_frame_path.exists()
    assert saw_done, "DONE が届きませんでした"
    assert spec.output_path.is_file() and spec.last_frame_path.is_file()


def test_ping_is_consumed_internally(make_engine):
    """契約 §3: pong は EngineEvent に変換しない。"""
    engine = make_engine("normal")
    start_ready(engine)
    engine.ping()
    assert quiet(engine, 1.5) == []
    assert engine.last_pong_monotonic is not None


# ---------------------------------------------------------------- 投入の拒否


def test_submit_before_ready_is_rejected(make_engine, paths):
    engine = make_engine("stall_before_ready")
    engine.start()
    wait_state(
        engine, EngineState.INITIALIZING_LORA, EngineState.INITIALIZING_MODEL
    )
    with pytest.raises(EngineBusyError):
        engine.submit(make_spec(paths))


def test_submit_without_start_is_rejected(make_engine, paths):
    engine = make_engine("normal")
    with pytest.raises(EngineBusyError):
        engine.submit(make_spec(paths))


def test_concurrent_submit_is_rejected(make_engine, paths):
    engine = make_engine("normal")
    start_ready(engine)
    engine.submit(make_spec(paths, job_id="v_first"))
    with pytest.raises(EngineBusyError):
        engine.submit(make_spec(paths, job_id="v_second"))


def test_submit_validates_job_spec(make_engine, paths):
    """UI を迂回した不正値は下位層でも止まる（同期 ValidationError）。"""
    engine = make_engine("normal")
    with pytest.raises(ValidationError):
        engine.submit(make_spec(paths, num_frames=243))
    with pytest.raises(ValidationError):
        engine.submit(make_spec(paths, steps=1))
    with pytest.raises(ValidationError):
        engine.submit(make_spec(paths, prompt="   "))
    with pytest.raises(ValidationError):
        engine.submit(make_spec(paths, backend_id="other_backend"))
    with pytest.raises(ValidationError):
        engine.submit(
            make_spec(paths, output_path=paths["outside"] / "escape.mp4")
        )


# ---------------------------------------------------------------- 継続生成（P4）


def test_continuation_sends_keyframe_path_on_the_wire(make_engine, paths):
    """契約 §2: keyframe_path が generate コマンドへ絶対パスで載る。"""
    engine = make_engine("normal")
    keyframe = write_png(paths["outputs"] / "v_parent_last.png")
    spec = continuation_spec(paths, keyframe)

    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.DONE, terminal.message
    dump = received_keyframe(paths)
    assert dump["job_id"] == spec.job_id
    assert dump["keyframe_path"] == str(keyframe)
    assert Path(dump["keyframe_path"]).is_absolute()
    # 成果物の形は単発生成と同じ（昇格済み・partial なし）
    assert spec.output_path.is_file() and spec.last_frame_path.is_file()
    assert list(paths["outputs"].glob("*.partial")) == []
    # 親のキーフレームは読むだけで書き換えない
    assert keyframe.is_file()


def test_single_generation_sends_null_keyframe(make_engine, paths):
    """非回帰: 単発生成では従来どおり keyframe_path=null を送る。"""
    engine = make_engine("normal")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.DONE, terminal.message
    assert received_keyframe(paths)["keyframe_path"] is None


def test_worker_requiring_keyframe_rejects_single_generation(make_engine, paths):
    """ワイヤに keyframe が本当に載っているかの対照試験（載らなければ input エラー）。"""
    engine = make_engine("keyframe_required")
    terminal, _ = run_to_terminal(engine, make_spec(paths))
    assert terminal.type is EventType.ERROR
    assert terminal.category is ErrorCategory.INPUT
    assert terminal.fatal is False

    keyframe = write_png(paths["outputs"] / "v_parent_last.png")
    spec = continuation_spec(paths, keyframe, job_id="v_child_ok")
    engine.submit(spec)
    assert wait_terminal(engine).type is EventType.DONE


def test_missing_keyframe_is_rejected_before_sending(make_engine, paths):
    """実在しないキーフレームは submit で同期拒否（ワーカーへ送らない）。"""
    engine = make_engine("normal")
    start_ready(engine)
    missing = paths["outputs"] / "v_missing_last.png"

    with pytest.raises(ValidationError, match="見つかりません"):
        engine.submit(continuation_spec(paths, missing))

    assert not paths["keyframe_dump"].exists()  # generate は送られていない
    assert engine.state() is EngineState.READY


def test_keyframe_outside_data_root_is_rejected(make_engine, paths):
    """data_root 外のキーフレームは submit で同期拒否（§15）。"""
    engine = make_engine("normal")
    outside = write_png(paths["outside"] / "stolen_last.png")

    with pytest.raises(ValidationError, match="データ領域の外"):
        engine.submit(continuation_spec(paths, outside))


def test_relative_keyframe_is_rejected(make_engine, paths):
    engine = make_engine("normal")
    with pytest.raises(ValidationError):
        engine.submit(continuation_spec(paths, Path("outputs/v_parent_last.png")))


def test_single_job_with_keyframe_is_rejected(make_engine, paths):
    """単発生成にキーフレームは指定できない（contracts の排他条件）。"""
    engine = make_engine("normal")
    keyframe = write_png(paths["outputs"] / "v_parent_last.png")
    with pytest.raises(ValidationError):
        engine.submit(make_spec(paths, keyframe_path=keyframe))


def test_continuation_without_parent_id_is_rejected(make_engine, paths):
    engine = make_engine("normal")
    keyframe = write_png(paths["outputs"] / "v_parent_last.png")
    with pytest.raises(ValidationError):
        engine.submit(continuation_spec(paths, keyframe, parent_id=None))


def test_broken_keyframe_is_rejected_by_the_worker(make_engine, paths):
    """エンジンを通った壊れた PNG はワーカーが非 fatal な input エラーで返す。"""
    engine = make_engine("normal")
    broken = paths["outputs"] / "v_broken_last.png"
    broken.write_bytes(b"this is definitely not a png file\n" * 8)
    spec = continuation_spec(paths, broken)

    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.job_id == spec.job_id
    assert terminal.fatal is False
    assert terminal.category is ErrorCategory.INPUT
    assert not spec.output_path.exists()
    # ワーカーは生きたままで、次のジョブを続けて処理できる
    assert wait_state(engine, EngineState.READY) is EngineState.READY
    good = write_png(paths["outputs"] / "v_parent_last.png")
    engine.submit(continuation_spec(paths, good, job_id="v_child_after_error"))
    assert wait_terminal(engine).type is EventType.DONE


# ---------------------------------------------------------------- handshake 不一致


@pytest.mark.parametrize(
    "scenario", ["bad_backend_id", "bad_model", "bad_capabilities"]
)
def test_handshake_mismatch_does_not_become_ready(make_engine, scenario):
    """契約 §5: 不一致なら READY にせず日本語の fatal エラー・HALTED。"""
    engine = make_engine(scenario)
    collected: list[EngineEvent] = []
    engine.start()
    error = wait_type(
        engine, EventType.ERROR, timeout=READY_TIMEOUT, collected=collected
    )

    assert not any(e.type is EventType.READY for e in collected)
    assert error.fatal is True
    assert error.category is ErrorCategory.MODEL_STATE
    assert "一致しません" in (error.message or "")
    assert engine.state() is EngineState.HALTED


def test_halted_engine_rejects_submit(make_engine, paths):
    engine = make_engine("bad_capabilities")
    engine.start()
    wait_type(engine, EventType.ERROR, timeout=READY_TIMEOUT)
    with pytest.raises(EngineBusyError):
        engine.submit(make_spec(paths))


# ---------------------------------------------------------------- 頑健性


@pytest.mark.parametrize("scenario", ["stdout_noise", "stderr_flood"])
def test_noisy_worker_does_not_deadlock(make_engine, paths, scenario):
    """数千行の stdout / stderr を流しても完走する（パイプの常時 drain）。"""
    engine = make_engine(scenario, env={"FAKE_WORKER_FLOOD": "3000"})
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.DONE, terminal.message
    assert spec.output_path.is_file()
    assert paths["worker_log"].is_file()
    assert paths["worker_log"].stat().st_size > 0


def test_broken_json_does_not_break_engine(make_engine, paths):
    """不正 JSON・非オブジェクト・未知種別を無視して動き続ける（契約 §3）。"""
    engine = make_engine("bad_json")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)
    assert terminal.type is EventType.DONE, terminal.message
    assert spec.output_path.is_file()


# ---------------------------------------------------------------- 検証と昇格の失敗


@pytest.mark.parametrize(
    "scenario",
    ["missing_mp4", "missing_png", "invalid_mp4", "invalid_png", "missing_paths"],
)
def test_broken_artifacts_fail_without_promoting(make_engine, paths, scenario):
    """契約 §4-8: 検証失敗は非 fatal ERROR。正式ファイルは作られない。"""
    engine = make_engine(scenario)
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.job_id == spec.job_id
    assert terminal.fatal is False
    assert terminal.category is ErrorCategory.PIPELINE
    assert not spec.output_path.exists()
    assert not spec.last_frame_path.exists()
    assert wait_state(engine, EngineState.READY) is EngineState.READY


def test_partial_outside_data_root_is_rejected(make_engine, paths):
    """data_root 外の partial は昇格しない（§15）。"""
    engine = make_engine("outside_data_root")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.fatal is False
    assert not spec.output_path.exists()
    assert not spec.last_frame_path.exists()
    # ワーカーが data_root 外に書いたファイルはそのまま（アプリは触らない）
    assert list(paths["outside"].glob("*.mp4.partial"))


def test_png_promotion_failure_rolls_back_mp4(make_engine, paths):
    """PNG 昇格に失敗したら昇格済み MP4 を撤去する（孤児を残さない）。"""
    engine = make_engine("normal")
    spec = make_spec(paths)
    # 正式名の位置をディレクトリにして os.replace() を失敗させる
    spec.last_frame_path.mkdir(parents=True)

    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.fatal is False
    assert terminal.category is ErrorCategory.PIPELINE
    assert not spec.output_path.exists(), "ロールバックされずに MP4 が残っています"
    assert spec.last_frame_path.is_dir()


# ---------------------------------------------------------------- job_id と二重 done


def test_job_id_mismatch_is_ignored(make_engine, paths):
    """契約 §5: 実行中ジョブと一致しない progress/done/error は適用しない。"""
    engine = make_engine("job_id_mismatch")
    spec = make_spec(paths)
    start_ready(engine)
    engine.submit(spec)

    assert quiet(engine, 3.0) == []
    assert engine.state() is EngineState.BUSY
    assert not spec.output_path.exists()
    assert not spec.last_frame_path.exists()


def test_double_done_is_ignored(make_engine, paths):
    engine = make_engine("double_done")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.DONE, terminal.message
    assert quiet(engine, 2.0) == []
    assert spec.output_path.is_file()
    assert list(paths["outputs"].glob("*.partial")) == []


# ---------------------------------------------------------------- エラー分類


def test_nonfatal_error_keeps_engine_usable(make_engine, paths):
    engine = make_engine("error_nonfatal")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.job_id == spec.job_id
    assert terminal.fatal is False
    assert terminal.category is ErrorCategory.INPUT
    assert wait_state(engine, EngineState.READY) is EngineState.READY
    assert not spec.output_path.exists()


def test_fatal_error_halts_engine_without_duplicate_event(make_engine, paths):
    engine = make_engine("error_fatal")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.job_id == spec.job_id
    assert terminal.fatal is True
    assert terminal.category is ErrorCategory.MPS
    # ワーカーは fatal 後に終了するが、確定済みジョブへ二重にエラーを出さない
    assert quiet(engine, 2.0) == []
    assert wait_state(engine, EngineState.HALTED, EngineState.DEAD) in (
        EngineState.HALTED,
        EngineState.DEAD,
    )
    with pytest.raises(EngineBusyError):
        engine.submit(make_spec(paths, job_id="v_next"))


# ---------------------------------------------------------------- ワーカーの終了


@pytest.mark.parametrize("scenario", ["exit_before_ready", "exit_after_ready"])
def test_idle_worker_exit_marks_dead(make_engine, scenario):
    """契約 §6: アイドル時のワーカー終了は DEAD。"""
    engine = make_engine(scenario)
    engine.start()
    assert wait_state(engine, EngineState.DEAD) is EngineState.DEAD
    assert engine._proc is not None and engine._proc.poll() is not None


def test_crash_during_generation_synthesizes_worker_dead(make_engine, paths):
    """契約 §6: 実行中の終了は fatal ERROR(worker_dead) を合成する。"""
    engine = make_engine("crash_running")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)

    assert terminal.type is EventType.ERROR
    assert terminal.job_id == spec.job_id
    assert terminal.fatal is True
    assert terminal.category is ErrorCategory.WORKER_DEAD
    assert wait_state(engine, EngineState.DEAD) is EngineState.DEAD
    assert not spec.output_path.exists()


def test_startup_timeout_halts_engine(make_engine):
    engine = make_engine("stall_before_ready", startup_timeout=1.0)
    engine.start()
    error = wait_type(engine, EventType.ERROR, timeout=15.0)
    assert error.fatal is True
    assert error.category is ErrorCategory.MODEL_STATE
    assert engine.state() in (EngineState.HALTED, EngineState.DEAD)


# ---------------------------------------------------------------- shutdown


def _assert_no_engine_threads(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not engine_threads():
            return
        time.sleep(0.05)
    raise AssertionError(
        "エンジンのスレッドが残っています: "
        + ", ".join(t.name for t in engine_threads())
    )


@pytest.mark.parametrize(
    "scenario", ["normal", "ignore_shutdown", "ignore_sigterm"]
)
def test_shutdown_leaves_no_worker_process_or_thread(make_engine, scenario):
    """契約 §6: shutdown → terminate → kill のエスカレーションで orphan を残さない。"""
    engine = make_engine(scenario, shutdown_grace=0.5)
    start_ready(engine)
    proc = engine._proc

    engine.shutdown(timeout=5.0)

    assert proc is not None and proc.poll() is not None
    assert engine.state() is EngineState.HALTED
    _assert_no_engine_threads()


def test_shutdown_is_idempotent_and_unblocks_poll(make_engine):
    engine = make_engine("normal")
    start_ready(engine)
    engine.shutdown(timeout=5.0)
    engine.shutdown(timeout=5.0)  # 二重呼び出し安全
    # ブロック中の poll_event が起こされている（番兵の None）
    assert engine.poll_event(1.0) is None
    with pytest.raises(EngineBusyError):
        engine.start()


def test_shutdown_before_start_is_safe(make_engine):
    engine = make_engine("normal")
    engine.shutdown(timeout=2.0)
    assert engine.state() is EngineState.HALTED
    _assert_no_engine_threads()


# ---------------------------------------------------------------- restart


#: 稼働中の RealEngine が持つスレッド数（stdout / stderr / handler / waiter）
WORKER_THREADS = 4


def test_restart_reports_running_job_as_failed(make_engine, paths):
    """実行中の restart は無言でジョブを失わせない（fatal ERROR を出してから再起動）。"""
    engine = make_engine("normal")
    spec = make_spec(paths)
    start_ready(engine)
    engine.submit(spec)

    engine.restart()

    error = wait_for(
        engine,
        lambda e: e.type is EventType.ERROR,
        timeout=15.0,
    )
    assert error.job_id == spec.job_id
    assert error.fatal is True
    assert error.category is ErrorCategory.WORKER_DEAD

    # 再起動後は改めて READY まで進む
    wait_type(engine, EventType.READY, timeout=READY_TIMEOUT)
    assert engine.state() is EngineState.READY
    assert not spec.output_path.exists()


def test_restart_during_generation_is_deterministic(make_engine, paths):
    """生成の途中（応答が返らない状態）で restart しても終端イベントが必ず届く。

    `hang_in_generate` は progress 1 を出したきり無応答になるため、
    「実行中に再起動した」条件を確実に作れる。
    """
    engine = make_engine("hang_in_generate", shutdown_grace=0.3)
    spec = make_spec(paths)
    start_ready(engine)
    engine.submit(spec)
    wait_for(
        engine,
        lambda e: e.type is EventType.PROGRESS and e.step == 1,
        timeout=JOB_TIMEOUT,
    )
    hung = engine._proc

    engine.restart()

    error = wait_type(engine, EventType.ERROR, timeout=15.0)
    assert error.job_id == spec.job_id
    assert error.fatal is True
    assert error.category is ErrorCategory.WORKER_DEAD
    assert "再起動" in (error.message or "") and "中断" in (error.message or "")

    # 無応答のワーカーは terminate まで落として置き去りにしない
    assert hung is not None and hung.poll() is not None
    stages = []
    wait_type(engine, EventType.READY, timeout=READY_TIMEOUT, collected=stages)
    assert types(stages) == ["stage", "stage", "ready"]
    assert engine.state() is EngineState.READY
    assert not spec.output_path.exists()
    assert launch_count(paths) == 2


def test_restart_while_idle_emits_no_error(make_engine, paths):
    """実行中ジョブが無い再起動では余計な ERROR を出さない（stage×2 → ready のみ）。"""
    engine = make_engine("normal")
    spec = make_spec(paths)
    terminal, _ = run_to_terminal(engine, spec)
    assert terminal.type is EventType.DONE, terminal.message
    old_pid = engine.worker_pid

    engine.restart()
    collected: list[EngineEvent] = []
    ready = wait_type(
        engine, EventType.READY, timeout=READY_TIMEOUT, collected=collected
    )

    assert types(collected) == ["stage", "stage", "ready"]
    assert [e.stage for e in collected[:2]] == [
        JobStage.LOADING_MODEL,
        JobStage.LOADING_LORA,
    ]
    assert ready.capabilities == MINIMAX_H3_CAPABILITIES
    assert quiet(engine, 1.0) == []
    # ワーカープロセスは作り直される（古いプロセスは残さない）
    assert engine.worker_pid != old_pid
    assert launch_count(paths) == 2


def test_restart_processes_a_new_job(make_engine, paths):
    """再起動後のエンジンで新しいジョブを最後まで処理できる。"""
    engine = make_engine("normal")
    start_ready(engine)
    engine.restart()
    wait_type(engine, EventType.READY, timeout=READY_TIMEOUT)

    spec = make_spec(paths, job_id="v_after_restart")
    engine.submit(spec)
    terminal = wait_terminal(engine)

    assert terminal.type is EventType.DONE, terminal.message
    assert terminal.job_id == spec.job_id
    assert spec.output_path.is_file() and spec.last_frame_path.is_file()
    assert list(paths["outputs"].glob("*.partial")) == []
    assert wait_state(engine, EngineState.READY) is EngineState.READY


def test_restart_does_not_recreate_event_queue(make_engine):
    """未消費イベントを捨てない（中断 ERROR を確実に届けるための必須条件）。"""
    engine = make_engine("normal")
    engine.start()
    wait_state(engine, EngineState.READY)  # イベントを消費せずに初期化完了を待つ

    engine.restart()
    wait_state(engine, EngineState.READY)

    events = drain(engine)
    # 1回目の stage×2 → ready が残ったまま、2回目の stage×2 → ready が続く
    assert types(events) == ["stage", "stage", "ready"] * 2


def test_restart_after_shutdown_raises_busy(make_engine):
    """shutdown 済みのエンジンは restart しない（MockEngine と同じ扱い）。

    復活を許すと、アプリ終了処理と競合したときにワーカープロセスが孤児として残る。
    """
    engine = make_engine("normal")
    start_ready(engine)
    proc = engine._proc
    engine.shutdown(timeout=5.0)

    with pytest.raises(EngineBusyError, match="停止済み"):
        engine.restart()

    assert proc is not None and proc.poll() is not None
    assert engine.state() is EngineState.HALTED
    _assert_no_engine_threads()


def test_double_restart_is_safe(make_engine, paths):
    """連続した restart でも例外・デッドロック・ワーカーの取りこぼしを起こさない。"""
    engine = make_engine("normal")
    start_ready(engine)

    engine.restart()
    engine.restart()
    wait_state(engine, EngineState.READY)

    events = drain(engine)
    assert not [e for e in events if e.type is EventType.ERROR]
    assert events[-1].type is EventType.READY
    assert launch_count(paths) == 3

    spec = make_spec(paths, job_id="v_after_double_restart")
    engine.submit(spec)
    assert wait_terminal(engine).type is EventType.DONE
    assert len(settled_threads(WORKER_THREADS)) == WORKER_THREADS


def test_repeated_restarts_do_not_leak_threads_or_processes(make_engine, paths):
    """再起動を繰り返してもスレッド・ワーカープロセスが増殖しない。"""
    engine = make_engine("normal")
    start_ready(engine)
    assert len(settled_threads(WORKER_THREADS)) == WORKER_THREADS
    seen = [engine._proc]

    for i in range(3):
        engine.restart()
        wait_type(engine, EventType.READY, timeout=READY_TIMEOUT)
        assert len(settled_threads(WORKER_THREADS)) == WORKER_THREADS, (
            "再起動でスレッドが増えています"
        )
        assert all(p is not None and p.poll() is not None for p in seen), (
            "古いワーカープロセスが残っています"
        )
        seen.append(engine._proc)

        spec = make_spec(paths, job_id=f"v_loop_{i}")
        engine.submit(spec)
        assert wait_terminal(engine).type is EventType.DONE
        assert spec.output_path.is_file()

    assert launch_count(paths) == 4


# ---------------------------------------------------------------- 再起動シナリオ


def test_worker_that_dies_before_ready_recovers_after_restart(make_engine, paths):
    """1回目の起動が ready 前に落ちても、restart でやり直せる（§13.3 の自動再起動の土台）。"""
    engine = make_engine("dead_then_ok")
    engine.start()
    assert wait_state(engine, EngineState.DEAD) is EngineState.DEAD

    engine.restart()
    wait_type(engine, EventType.READY, timeout=READY_TIMEOUT)
    assert engine.state() is EngineState.READY

    spec = make_spec(paths, job_id="v_recovered")
    engine.submit(spec)
    assert wait_terminal(engine).type is EventType.DONE
    assert spec.output_path.is_file()
    assert launch_count(paths) == 2


def test_fatal_then_ok_worker_recovers_after_restart(make_engine, paths):
    """1回目の起動は fatal、再起動後は正常（連続失敗カウントのリセット試験の土台）。"""
    engine = make_engine("fatal_then_ok")
    first = make_spec(paths, job_id="v_fatal_run")
    terminal, _ = run_to_terminal(engine, first)

    assert terminal.type is EventType.ERROR
    assert terminal.fatal is True
    assert terminal.category is ErrorCategory.MPS
    assert engine.state() is EngineState.HALTED
    with pytest.raises(EngineBusyError):
        engine.submit(make_spec(paths, job_id="v_blocked"))

    engine.restart()
    wait_type(engine, EventType.READY, timeout=READY_TIMEOUT)

    second = make_spec(paths, job_id="v_ok_run")
    engine.submit(second)
    assert wait_terminal(engine).type is EventType.DONE
    assert second.output_path.is_file()
    assert launch_count(paths) == 2


def test_always_fatal_worker_keeps_failing_across_restarts(make_engine, paths):
    """何度再起動しても fatal を返すワーカー（連続失敗 → HALTED の試験の土台）。"""
    engine = make_engine("always_fatal")
    start_ready(engine)

    for i in range(2):
        spec = make_spec(paths, job_id=f"v_always_fatal_{i}")
        engine.submit(spec)
        terminal = wait_terminal(engine)
        assert terminal.type is EventType.ERROR
        assert terminal.job_id == spec.job_id
        assert terminal.fatal is True
        assert terminal.category is ErrorCategory.MPS
        assert wait_state(engine, EngineState.HALTED) is EngineState.HALTED
        assert not spec.output_path.exists()

        engine.restart()
        wait_type(engine, EventType.READY, timeout=READY_TIMEOUT)

    assert launch_count(paths) == 3
