"""MockEngine のテスト（設計書 §16・§10.7・§22）。

- 書き込み先は必ず `tmp_path`（data_root として渡す）。プロジェクトの `data/` へは触れない。
- モック素材（`app/assets/mock/`）は読み取りのみ。
- `sleep_fn` を差し替えて実時間ゼロで完走させる（実待機は ffmpeg 検証のみ）。
"""

from __future__ import annotations

import dataclasses
import queue
import random
import threading
import time
from pathlib import Path

import pytest

from app.core.config import load_config
from app.core.contracts import (
    MINIMAX_H3_CAPABILITIES,
    MOCK_FAIL_PREFIX,
    SEED_MAX,
    BackendIdentity,
    EngineBusyError,
    EngineState,
    ErrorCategory,
    EventType,
    JobSpec,
    JobStage,
    ValidationError,
)
from app.engine.mock_engine import MockEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_ASSETS_DIR = PROJECT_ROOT / "app" / "assets" / "mock"

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax H3 NF4 Turbo",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)

# テスト用の短い目安時間（sleep_fn は無効化するので実時間には影響しない）
FAST_ESTIMATES = {
    "init_sec": 1.0,
    "f56_s4_sec": 1.0,
    "f124_s4_sec": 2.0,
    "step8_factor": 2.0,
}

TERMINAL = (EventType.DONE, EventType.ERROR)


# ---------------------------------------------------------------- ヘルパ


@pytest.fixture
def make_engine(tmp_path):
    """tmp_path を data_root にした MockEngine を作る（後始末つき）。"""
    engines: list[MockEngine] = []

    def _make(**overrides) -> MockEngine:
        params = {
            "identity": IDENTITY,
            "assets_dir": MOCK_ASSETS_DIR,
            "data_root": tmp_path,
            "estimates": FAST_ESTIMATES,
            "sleep_fn": lambda s: None,
        }
        params.update(overrides)
        engine = MockEngine(**params)
        engines.append(engine)
        return engine

    yield _make

    for engine in engines:
        engine.shutdown(timeout=5.0)


def _spec(tmp_path, **overrides) -> JobSpec:
    job_id = overrides.pop("job_id", "v_20260807_120000_aaaa")
    out_dir = overrides.pop("out_dir", tmp_path / "outputs")
    params = {
        "job_id": job_id,
        "prompt": "夕暮れの街を歩く女性。",
        "num_frames": 56,
        "steps": 4,
        "seed_requested": 42,
        "output_path": out_dir / f"{job_id}.mp4",
        "last_frame_path": out_dir / f"{job_id}_last.png",
    }
    params.update(overrides)
    return JobSpec(**params)


def _collect(engine: MockEngine, wanted, timeout: float = 30.0) -> list:
    """指定種別のイベントを受け取るまで収集する（受け取れなければ失敗）。"""
    wanted = tuple(wanted) if isinstance(wanted, (tuple, list, set)) else (wanted,)
    deadline = time.monotonic() + timeout
    events: list = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"{[w.value for w in wanted]} を受信できませんでした: "
                f"{[e.type.value for e in events]}"
            )
        event = engine.poll_event(timeout=min(0.2, remaining))
        if event is None:
            continue
        events.append(event)
        if event.type in wanted:
            return events


def _start_ready(engine: MockEngine) -> list:
    engine.start()
    return _collect(engine, EventType.READY)


def _run_job(engine: MockEngine, spec: JobSpec) -> list:
    engine.submit(spec)
    return _collect(engine, TERMINAL)


def _types(events) -> list[str]:
    return [e.type.value for e in events]


def _wait_state(engine: MockEngine, *states, timeout: float = 10.0) -> EngineState:
    """イベントを消費せずに状態が変わるのを待つ（キューの中身を保ちたい試験で使う）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = engine.state()
        if state in states:
            return state
        time.sleep(0.01)
    raise AssertionError(
        f"状態が {[s.value for s in states]} になりません（現在: {engine.state().value}）"
    )


def _drain(engine: MockEngine, timeout: float = 5.0) -> list:
    """キューが空になるまでイベントを取り出す。"""
    events: list = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = engine.poll_event(timeout=0.05)
        if event is None:
            break
        events.append(event)
    return events


def _mock_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("mock-engine")]


def _settled_threads(timeout: float = 5.0) -> list[str]:
    """モックエンジンのスレッドが片付くのを待って、残っているものを返す。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        names = _mock_threads()
        if not names:
            return names
        time.sleep(0.02)
    return _mock_threads()


class _ParkWhileBusy:
    """BUSY のあいだ生成スレッドを待たせる `sleep_fn`（「生成中」を保つ）。

    `engine.state()` が BUSY でなくなった時点（restart / shutdown が停止要求を
    出した時点）で待機を抜けるので、テストが実時間を長く待つことはない。
    初期化中（STARTING / INITIALIZING_*）は素通りするため READY までは通常どおり進む。
    """

    def __init__(self) -> None:
        self.engine: MockEngine | None = None
        self.parked = threading.Event()

    def __call__(self, _seconds: float) -> None:
        engine = self.engine
        if engine is None or engine.state() is not EngineState.BUSY:
            return
        self.parked.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if engine.state() is not EngineState.BUSY:
                return
            time.sleep(0.005)


# ---------------------------------------------------------------- 初期化・READY


def test_ready_event_reports_backend_and_capabilities(make_engine):
    engine = make_engine()
    events = _start_ready(engine)

    assert _types(events) == ["stage", "stage", "ready"]
    assert [e.stage for e in events[:2]] == [
        JobStage.LOADING_MODEL,
        JobStage.LOADING_LORA,
    ]

    ready = events[-1]
    assert ready.backend_id == "minimax_h3"
    caps = ready.capabilities
    assert caps == MINIMAX_H3_CAPABILITIES
    # MiniMax-H3 の固定値（設計書 §22.2）
    assert caps.num_frames == (56, 124)
    assert caps.steps == (4, 8)
    assert (caps.width, caps.height, caps.fps) == (576, 320, 24)
    assert caps.audio is True
    assert caps.continuation is True
    assert caps.seed is True
    assert caps.last_frame_output is True
    assert caps.references == {"image": False, "video": False, "audio": False}

    assert engine.state() is EngineState.READY
    assert engine.identity == IDENTITY


def test_start_is_idempotent(make_engine):
    engine = make_engine()
    _start_ready(engine)
    engine.start()  # 二重呼び出しでも初期化はやり直さない
    assert engine.poll_event(timeout=0.1) is None
    assert engine.state() is EngineState.READY


# ---------------------------------------------------------------- 生成イベント


def test_event_sequence_matches_real_worker_shape(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    events = _run_job(engine, _spec(tmp_path))

    assert _types(events) == [
        "stage",
        "progress",
        "progress",
        "progress",
        "progress",
        "stage",
        "done",
    ]
    assert events[0].stage is JobStage.PREPARING
    assert events[-2].stage is JobStage.SAVING
    assert all(e.job_id == "v_20260807_120000_aaaa" for e in events)
    assert engine.state() is EngineState.READY


@pytest.mark.parametrize("steps", [4, 8])
def test_progress_count_matches_steps(make_engine, tmp_path, steps):
    engine = make_engine()
    _start_ready(engine)
    events = _run_job(engine, _spec(tmp_path, steps=steps))

    progress = [e for e in events if e.type is EventType.PROGRESS]
    assert len(progress) == steps
    assert [e.step for e in progress] == list(range(1, steps + 1))
    assert {e.total for e in progress} == {steps}
    assert events[-1].type is EventType.DONE


@pytest.mark.parametrize("num_frames", [56, 124])
def test_asset_matches_num_frames(make_engine, tmp_path, num_frames):
    engine = make_engine()
    _start_ready(engine)
    spec = _spec(tmp_path, num_frames=num_frames)
    done = _run_job(engine, spec)[-1]

    assert done.type is EventType.DONE
    src_mp4 = MOCK_ASSETS_DIR / f"mock_{num_frames}.mp4"
    src_png = MOCK_ASSETS_DIR / f"mock_{num_frames}_last.png"
    assert done.output_path.read_bytes() == src_mp4.read_bytes()
    assert done.last_frame_path.read_bytes() == src_png.read_bytes()
    # 56f と 124f の素材が取り違えられていないことをサイズでも確認
    other = MOCK_ASSETS_DIR / f"mock_{124 if num_frames == 56 else 56}.mp4"
    assert done.output_path.stat().st_size != other.stat().st_size


def test_done_reports_backend_identity_and_elapsed(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    done = _run_job(engine, _spec(tmp_path))[-1]

    assert done.backend_id == "minimax_h3"
    assert done.model_id == "DiffSynth-Studio/MiniMax-H3-NF4"
    assert done.model_revision == "nf4-turbo4step-ckpt500"
    assert done.warnings == ()
    assert done.elapsed_sec is not None and done.elapsed_sec >= 0


# ---------------------------------------------------------------- seed


def test_requested_seed_is_returned(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    done = _run_job(engine, _spec(tmp_path, seed_requested=123456))[-1]
    assert done.seed_used == 123456


def test_random_seed_is_generated_within_range(make_engine, tmp_path):
    engine = make_engine(rng=random.Random(1234))
    _start_ready(engine)
    done = _run_job(engine, _spec(tmp_path, seed_requested=None))[-1]

    assert done.seed_used == random.Random(1234).randint(0, SEED_MAX)
    assert 0 <= done.seed_used <= SEED_MAX


def test_random_seed_without_rng_uses_secrets(make_engine, tmp_path):
    engine = make_engine()  # rng=None → secrets 採番
    _start_ready(engine)
    done = _run_job(engine, _spec(tmp_path, seed_requested=None))[-1]
    assert isinstance(done.seed_used, int)
    assert 0 <= done.seed_used <= SEED_MAX


# ---------------------------------------------------------------- 失敗注入


def test_mock_fail_prompt_errors_and_engine_keeps_running(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)

    failing = _spec(
        tmp_path,
        job_id="v_20260807_120000_bad0",
        prompt=f"{MOCK_FAIL_PREFIX} わざと失敗させる",
    )
    events = _run_job(engine, failing)
    error = events[-1]
    assert error.type is EventType.ERROR
    assert error.fatal is False
    # プロンプト起因の失敗＝入力エラー（非fatal・ワーカーは再利用してよい。§13.3）
    assert error.category is ErrorCategory.INPUT
    assert error.job_id == "v_20260807_120000_bad0"
    assert error.message
    # 失敗時は正式成果物を作らない
    assert not failing.output_path.exists()
    assert not failing.last_frame_path.exists()
    # エンジンは生存して READY に戻る
    assert engine.state() is EngineState.READY

    ok = _spec(tmp_path, job_id="v_20260807_120001_ok00")
    done = _run_job(engine, ok)[-1]
    assert done.type is EventType.DONE
    assert ok.output_path.is_file()
    assert engine.state() is EngineState.READY


def test_missing_asset_errors_without_final_files(make_engine, tmp_path):
    engine = make_engine(assets_dir=tmp_path / "no_assets")
    _start_ready(engine)
    spec = _spec(tmp_path)
    error = _run_job(engine, spec)[-1]

    assert error.type is EventType.ERROR
    assert error.fatal is False
    assert "モック素材" in error.message
    assert not spec.output_path.exists()
    assert not spec.last_frame_path.exists()
    assert engine.state() is EngineState.READY


# ---------------------------------------------------------------- 原子的保存


def test_artifacts_are_promoted_atomically(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    spec = _spec(tmp_path)
    done = _run_job(engine, spec)[-1]

    assert done.type is EventType.DONE
    assert spec.output_path.is_file()
    assert spec.last_frame_path.is_file()
    # DONE は上位層が渡した正式パスをそのまま返す（履歴の相対化がずれない）
    assert done.output_path == spec.output_path
    assert done.last_frame_path == spec.last_frame_path
    # partial は残らない
    assert list(spec.output_path.parent.glob("*.partial")) == []
    assert spec.output_path.stat().st_size > 0
    assert spec.last_frame_path.stat().st_size > 0


def test_no_final_artifact_exists_before_done(make_engine, tmp_path):
    """§10.7: 昇格前（preparing / progress / saving）に正式名のファイルが存在しない。

    観測は `sleep_fn`（エンジンスレッド内で各段階の待機時に呼ばれる）で行う。
    saving 段階の待機は成果物の書き込み直前に入るため、ここで存在しなければ
    「DONE より前に正式ファイルが現れない」ことを確認できる。
    """
    spec = _spec(tmp_path)
    observed: list[tuple[bool, bool]] = []

    def probe(_seconds: float) -> None:
        observed.append((spec.output_path.exists(), spec.last_frame_path.exists()))

    engine = make_engine(sleep_fn=probe)
    _start_ready(engine)

    saw_saving_stage = False
    engine.submit(spec)
    events = []
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        event = engine.poll_event(timeout=0.2)
        if event is None:
            continue
        events.append(event)
        if event.type is EventType.STAGE and event.stage is JobStage.SAVING:
            saw_saving_stage = True
            assert not spec.output_path.exists()
            assert not spec.last_frame_path.exists()
        if event.type in TERMINAL:
            break

    assert saw_saving_stage
    assert events[-1].type is EventType.DONE
    assert observed, "sleep_fn が呼ばれていない（ペース配分が働いていない）"
    assert all(existed == (False, False) for existed in observed)
    assert spec.output_path.is_file() and spec.last_frame_path.is_file()


# ---------------------------------------------------------------- 入力検証


def test_rejects_output_outside_data_root(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    outside = tmp_path.parent / "outside"
    spec = _spec(tmp_path, out_dir=outside)

    with pytest.raises(ValidationError, match="データ領域の外"):
        engine.submit(spec)
    assert engine.state() is EngineState.READY
    assert not (outside / f"{spec.job_id}.mp4").exists()


def test_rejects_traversal_output_path(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    spec = _spec(tmp_path, out_dir=tmp_path / "outputs" / ".." / ".." / "escaped")
    with pytest.raises(ValidationError, match="データ領域の外"):
        engine.submit(spec)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"num_frames": 100}, "動画の長さが不正"),
        ({"steps": 6}, "ステップ数が不正"),
        ({"prompt": "   "}, "プロンプトを入力"),
        ({"seed_requested": -1}, "シード値は 0"),
        ({"seed_requested": SEED_MAX + 1}, "シード値は 0"),
        ({"width": 1024}, "解像度は"),
        ({"fps": 30}, "fps は"),
    ],
)
def test_rejects_invalid_job_spec(make_engine, tmp_path, overrides, message):
    engine = make_engine()
    _start_ready(engine)
    with pytest.raises(ValidationError, match=message):
        engine.submit(_spec(tmp_path, **overrides))
    assert engine.state() is EngineState.READY
    assert engine.poll_event(timeout=0.1) is None


def test_rejects_foreign_backend_id(make_engine, tmp_path):
    engine = make_engine(
        identity=dataclasses.replace(IDENTITY, backend_id="other_backend")
    )
    _start_ready(engine)
    # minimax_h3 は SUPPORTED_BACKENDS 上は正当だが、このエンジンの identity とは異なる
    with pytest.raises(ValidationError, match="このエンジンでは扱えない"):
        engine.submit(_spec(tmp_path))
    assert engine.state() is EngineState.READY


def test_rejects_unsupported_backend_id(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    with pytest.raises(ValidationError, match="未対応の生成バックエンド"):
        engine.submit(_spec(tmp_path, backend_id="unknown_model"))


# ---------------------------------------------------------------- 継続生成（P4）


def _write_png(path: Path, size: tuple[int, int] = (576, 320)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", size, (12, 34, 56)) as image:
        image.save(path, format="PNG")
    return path


def _continuation_spec(tmp_path, keyframe: Path, **overrides) -> JobSpec:
    params = {
        "job_id": "v_20260807_130000_ch00",
        "job_type": "continuation",
        "parent_id": "v_20260807_120000_aaaa",
        "keyframe_path": keyframe,
    }
    params.update(overrides)
    return _spec(tmp_path, **params)


def test_continuation_produces_the_same_artifacts(make_engine, tmp_path):
    """継続生成でも成果物の形・イベント列は単発とまったく同じ（契約 §2）。"""
    engine = make_engine()
    _start_ready(engine)
    keyframe = _write_png(tmp_path / "outputs" / "v_parent_last.png")
    spec = _continuation_spec(tmp_path, keyframe)

    events = _run_job(engine, spec)
    done = events[-1]

    assert _types(events) == [
        "stage",
        "progress",
        "progress",
        "progress",
        "progress",
        "stage",
        "done",
    ]
    assert done.type is EventType.DONE
    assert done.output_path == spec.output_path and done.output_path.is_file()
    assert done.last_frame_path == spec.last_frame_path
    assert done.seed_used == 42
    assert done.warnings == ()
    # 親のキーフレームは読むだけ（書き換えない）
    assert keyframe.is_file() and keyframe.stat().st_size > 0
    assert engine.state() is EngineState.READY


def test_continuation_rejects_missing_keyframe(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    missing = tmp_path / "outputs" / "v_missing_last.png"

    with pytest.raises(ValidationError, match="見つかりません"):
        engine.submit(_continuation_spec(tmp_path, missing))
    assert engine.state() is EngineState.READY
    assert engine.poll_event(timeout=0.1) is None


def test_continuation_rejects_keyframe_outside_data_root(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    outside = _write_png(tmp_path.parent / "outside" / "stolen_last.png")

    with pytest.raises(ValidationError, match="データ領域の外"):
        engine.submit(_continuation_spec(tmp_path, outside))


def test_continuation_rejects_relative_keyframe(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    with pytest.raises(ValidationError):
        engine.submit(_continuation_spec(tmp_path, Path("outputs/v_parent_last.png")))


def test_single_job_with_keyframe_is_rejected(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    keyframe = _write_png(tmp_path / "outputs" / "v_parent_last.png")
    with pytest.raises(ValidationError, match="キーフレーム"):
        engine.submit(_spec(tmp_path, keyframe_path=keyframe))


def test_continuation_without_parent_id_is_rejected(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    keyframe = _write_png(tmp_path / "outputs" / "v_parent_last.png")
    with pytest.raises(ValidationError, match="継続元"):
        engine.submit(_continuation_spec(tmp_path, keyframe, parent_id=None))


def test_broken_keyframe_becomes_nonfatal_input_error(make_engine, tmp_path):
    """壊れた画像は実機ワーカーと同じく ERROR(fatal=False, input)。エンジンは生存する。"""
    engine = make_engine()
    _start_ready(engine)
    broken = tmp_path / "outputs" / "v_broken_last.png"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is definitely not a png file\n" * 8)
    spec = _continuation_spec(tmp_path, broken)

    events = _run_job(engine, spec)
    error = events[-1]

    assert error.type is EventType.ERROR
    assert error.job_id == spec.job_id
    assert error.fatal is False
    assert error.category is ErrorCategory.INPUT
    assert "開けませんでした" in (error.message or "")
    assert not spec.output_path.exists()
    assert not spec.last_frame_path.exists()
    # 生成は途中で止まる（progress を出し切らない）
    assert _types(events) == ["stage", "error"]
    # エンジンは READY に戻り、次のジョブを処理できる
    assert engine.state() is EngineState.READY
    good = _write_png(tmp_path / "outputs" / "v_parent_last.png")
    ok_spec = _continuation_spec(tmp_path, good, job_id="v_20260807_130001_ch01")
    assert _run_job(engine, ok_spec)[-1].type is EventType.DONE


def test_keyframe_rejection_matches_real_engine(tmp_path):
    """real / mock が同じ入力を**同じ日本語文言**で拒否する（上位層から見た契約の一致）。"""
    from app.engine import mock_engine as mock_mod
    from app.engine import real_engine as real_mod

    for name in (
        "KEYFRAME_LABEL",
        "KEYFRAME_NOT_ABSOLUTE",
        "KEYFRAME_OUTSIDE_ROOT",
        "KEYFRAME_NOT_FOUND",
    ):
        assert getattr(mock_mod, name) == getattr(real_mod, name), name

    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    cases = [
        Path("outputs/relative_last.png"),
        tmp_path / "outside" / "stolen_last.png",
        data_root / "outputs" / "missing_last.png",
    ]
    for keyframe in cases:
        messages = set()
        for module in (mock_mod, real_mod):
            with pytest.raises(ValidationError) as excinfo:
                module.validate_keyframe(data_root, keyframe)
            messages.add(str(excinfo.value))
        assert len(messages) == 1, f"拒否理由が real/mock で異なります: {messages}"

    ok = _write_png(data_root / "outputs" / "v_parent_last.png")
    assert mock_mod.validate_keyframe(data_root, ok) == real_mod.validate_keyframe(
        data_root, ok
    )


# ---------------------------------------------------------------- 開始画像（P8）


def _start_image_spec(tmp_path, image: Path, **overrides) -> JobSpec:
    params = {
        "job_id": "v_20260810_120000_si00",
        "job_type": "start_image",
        "keyframe_path": image,
    }
    params.update(overrides)
    return _spec(tmp_path, **params)


def test_start_image_job_produces_the_same_artifacts(make_engine, tmp_path):
    """P8: 開始画像つきでも成果物の形・イベント列は単発／継続とまったく同じ。"""
    engine = make_engine()
    _start_ready(engine)
    image = _write_png(tmp_path / "start_images" / "si_0123456789ab.png")
    spec = _start_image_spec(tmp_path, image)

    events = _run_job(engine, spec)
    done = events[-1]

    assert _types(events) == [
        "stage",
        "progress",
        "progress",
        "progress",
        "progress",
        "stage",
        "done",
    ]
    assert done.type is EventType.DONE
    assert done.output_path == spec.output_path and done.output_path.is_file()
    assert done.last_frame_path == spec.last_frame_path
    assert done.seed_used == 42
    assert done.warnings == ()
    # 開始画像は読むだけ（書き換えない）
    assert image.is_file() and image.stat().st_size > 0
    assert engine.state() is EngineState.READY


def test_start_image_job_rejects_a_missing_image(make_engine, tmp_path):
    """P8: 実在しない開始画像は継続生成と同じ文言で同期拒否する。"""
    engine = make_engine()
    _start_ready(engine)
    missing = tmp_path / "start_images" / "si_ffffffffffff.png"

    with pytest.raises(ValidationError, match="見つかりません"):
        engine.submit(_start_image_spec(tmp_path, missing))
    assert engine.state() is EngineState.READY


# ---------------------------------------------------------------- 状態遷移


def test_submit_before_ready_raises_busy(make_engine, tmp_path):
    engine = make_engine()
    with pytest.raises(EngineBusyError):
        engine.submit(_spec(tmp_path))


def test_second_submit_while_busy_raises_busy(make_engine, tmp_path):
    class SwitchableSleep:
        def __init__(self) -> None:
            self.block = False
            self.release = threading.Event()

        def __call__(self, _seconds: float) -> None:
            if self.block:
                self.release.wait(timeout=10.0)

    sleeper = SwitchableSleep()
    engine = make_engine(sleep_fn=sleeper)
    _start_ready(engine)

    sleeper.block = True  # 生成スレッドを待機で止め、BUSY を維持する
    engine.submit(_spec(tmp_path, job_id="v_20260807_120000_run1"))
    try:
        assert engine.state() is EngineState.BUSY
        with pytest.raises(EngineBusyError, match="受け付けられる状態ではありません"):
            engine.submit(_spec(tmp_path, job_id="v_20260807_120000_run2"))
    finally:
        sleeper.release.set()

    done = _collect(engine, TERMINAL)[-1]
    assert done.type is EventType.DONE
    assert done.job_id == "v_20260807_120000_run1"


def test_shutdown_stops_threads_and_is_idempotent(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    _run_job(engine, _spec(tmp_path))

    engine.shutdown(timeout=5.0)
    engine.shutdown(timeout=5.0)  # 二重呼び出しでも例外・デッドロックなし

    assert engine.state() is EngineState.HALTED
    alive = [t.name for t in threading.enumerate() if t.name.startswith("mock-engine")]
    assert alive == []
    with pytest.raises(EngineBusyError):
        engine.submit(_spec(tmp_path, job_id="v_20260807_120002_late0"))


def test_shutdown_during_job_leaves_no_threads(make_engine, tmp_path):
    engine = make_engine(sleep_fn=lambda s: time.sleep(0.01))
    _start_ready(engine)
    engine.submit(_spec(tmp_path))
    engine.shutdown(timeout=5.0)

    assert engine.state() is EngineState.HALTED
    alive = [t.name for t in threading.enumerate() if t.name.startswith("mock-engine")]
    assert alive == []


def test_shutdown_wakes_blocked_poller(make_engine):
    engine = make_engine()
    _start_ready(engine)
    result: queue.Queue = queue.Queue()

    def poller() -> None:
        result.put(engine.poll_event(timeout=None))

    thread = threading.Thread(target=poller, daemon=True)
    thread.start()
    time.sleep(0.05)
    engine.shutdown(timeout=5.0)
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result.get(timeout=1.0) is None


def test_poll_event_timeout_returns_none(make_engine):
    engine = make_engine()
    started = time.monotonic()
    assert engine.poll_event(timeout=0.1) is None
    assert time.monotonic() - started >= 0.05


# ---------------------------------------------------------------- restart（§13.3）


def test_restart_reinitializes_and_accepts_jobs(make_engine, tmp_path):
    engine = make_engine()
    _start_ready(engine)
    _run_job(engine, _spec(tmp_path, job_id="v_20260807_120000_one0"))

    engine.restart()
    events = _collect(engine, EventType.READY)
    assert _types(events)[-3:] == ["stage", "stage", "ready"]
    assert engine.state() is EngineState.READY

    spec = _spec(tmp_path, job_id="v_20260807_120003_two0")
    assert _run_job(engine, spec)[-1].type is EventType.DONE
    assert spec.output_path.is_file()


def test_restart_while_idle_emits_no_error(make_engine, tmp_path):
    """実行中ジョブが無い再起動では余計な ERROR を出さない（stage×2 → ready のみ）。"""
    engine = make_engine()
    _start_ready(engine)
    _run_job(engine, _spec(tmp_path, job_id="v_20260807_120100_idle"))

    engine.restart()
    events = _collect(engine, EventType.READY)

    assert _types(events) == ["stage", "stage", "ready"]
    assert [e.stage for e in events[:2]] == [
        JobStage.LOADING_MODEL,
        JobStage.LOADING_LORA,
    ]
    assert events[-1].backend_id == "minimax_h3"
    assert events[-1].capabilities == MINIMAX_H3_CAPABILITIES
    assert engine.poll_event(timeout=0.2) is None


def test_restart_during_job_reports_fatal_worker_dead(make_engine, tmp_path):
    """生成中の restart は実行中ジョブへ fatal ERROR(worker_dead) を届けてから再起動する。

    P1 の実装では実行中ジョブが DONE も ERROR も出さずに消え、ディスパッチャが
    終端イベントを待ち続けてキューが永久停止していた（P3 の必須修正点）。
    """
    parker = _ParkWhileBusy()
    engine = make_engine(sleep_fn=parker)
    parker.engine = engine
    _start_ready(engine)

    spec = _spec(tmp_path, job_id="v_20260807_120200_run0")
    engine.submit(spec)
    assert parker.parked.wait(timeout=10.0), "生成スレッドが動き出していません"
    assert engine.state() is EngineState.BUSY

    engine.restart()

    events = _collect(engine, EventType.ERROR)
    error = events[-1]
    assert error.job_id == spec.job_id
    assert error.fatal is True
    assert error.category is ErrorCategory.WORKER_DEAD
    assert "再起動" in (error.message or "") and "中断" in (error.message or "")
    # 中断されたジョブの成果物は正式名で残さない
    assert not spec.output_path.exists()
    assert not spec.last_frame_path.exists()

    # 再起動後は stage×2 → ready を再送する
    after = _collect(engine, EventType.READY)
    assert _types(after) == ["stage", "stage", "ready"]
    assert engine.state() is EngineState.READY
    # 中断済みジョブの DONE が後から届かない（終端イベントは1回だけ）
    assert engine.poll_event(timeout=0.3) is None


def test_restart_during_job_allows_next_job(make_engine, tmp_path):
    """中断後も新しいジョブを正常に処理できる（キューが止まらない）。"""
    parker = _ParkWhileBusy()
    engine = make_engine(sleep_fn=parker)
    parker.engine = engine
    _start_ready(engine)

    interrupted = _spec(tmp_path, job_id="v_20260807_120300_kill")
    engine.submit(interrupted)
    assert parker.parked.wait(timeout=10.0)
    engine.restart()
    _collect(engine, EventType.READY)

    parker.engine = None  # 以後は待たせない（通常ペースで完走させる）
    nxt = _spec(tmp_path, job_id="v_20260807_120301_next")
    done = _run_job(engine, nxt)[-1]

    assert done.type is EventType.DONE
    assert done.job_id == nxt.job_id
    assert nxt.output_path.is_file()
    assert nxt.last_frame_path.is_file()
    assert not interrupted.output_path.exists()


def test_restart_does_not_recreate_event_queue(make_engine):
    """未消費イベントを捨てない（中断 ERROR を確実に届けるための必須条件）。"""
    engine = make_engine()
    engine.start()
    _wait_state(engine, EngineState.READY)  # イベントを消費せずに初期化完了を待つ

    engine.restart()
    _wait_state(engine, EngineState.READY)

    events = _drain(engine)
    # 1回目の stage×2 → ready が残ったまま、2回目の stage×2 → ready が続く
    assert _types(events) == ["stage", "stage", "ready"] * 2


def test_restart_after_shutdown_raises_busy(make_engine):
    """shutdown 済みのエンジンは restart しない（RealEngine と同じ扱い）。"""
    engine = make_engine()
    _start_ready(engine)
    engine.shutdown(timeout=5.0)

    with pytest.raises(EngineBusyError, match="停止済み"):
        engine.restart()

    assert engine.state() is EngineState.HALTED
    assert _settled_threads() == []


def test_double_restart_is_safe(make_engine, tmp_path):
    """連続した restart でも例外・デッドロック・ERROR を起こさない。"""
    engine = make_engine()
    _start_ready(engine)

    engine.restart()
    engine.restart()
    _wait_state(engine, EngineState.READY)

    events = _drain(engine)
    assert not [e for e in events if e.type is EventType.ERROR]
    assert events[-1].type is EventType.READY
    assert engine.state() is EngineState.READY

    spec = _spec(tmp_path, job_id="v_20260807_120400_afte")
    assert _run_job(engine, spec)[-1].type is EventType.DONE


def test_repeated_restarts_do_not_leak_threads(make_engine, tmp_path):
    """再起動を繰り返してもスレッドが増殖しない。"""
    engine = make_engine()
    _start_ready(engine)
    assert len(_mock_threads()) <= 1

    for i in range(3):
        engine.restart()
        _wait_state(engine, EngineState.READY)
        _drain(engine)
        spec = _spec(tmp_path, job_id=f"v_20260807_12050{i}_loop")
        assert _run_job(engine, spec)[-1].type is EventType.DONE
        assert spec.output_path.is_file()

    # 生成も初期化も終わっていれば専用スレッドは残らない
    assert _settled_threads() == []


# ---------------------------------------------------------------- config 連携


def test_from_config_uses_backend_identity_and_data_root(tmp_path, monkeypatch):
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = dataclasses.replace(load_config(PROJECT_ROOT), data_root=tmp_path)
    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    try:
        assert engine.identity == IDENTITY
        assert engine.capabilities == MINIMAX_H3_CAPABILITIES
        _start_ready(engine)
        spec = _spec(tmp_path)
        done = _run_job(engine, spec)[-1]
        assert done.type is EventType.DONE
        assert spec.output_path.is_file()
        assert spec.last_frame_path.is_file()
    finally:
        engine.shutdown(timeout=5.0)
