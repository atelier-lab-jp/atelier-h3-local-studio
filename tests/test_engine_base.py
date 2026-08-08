"""エンジン共通契約（app/engine/base.py）のテスト。設計書 §16.2・§22.2。"""

from __future__ import annotations

import inspect
from pathlib import Path

from app.core.config import BackendConfig, load_config
from app.core.contracts import BackendIdentity, EngineState
from app.engine.base import Engine, backend_identity
from app.engine.mock_engine import MockEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_ASSETS_DIR = PROJECT_ROOT / "app" / "assets" / "mock"

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax H3 NF4 Turbo",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)


def _backend_config(**overrides) -> BackendConfig:
    values = {
        "backend_id": "minimax_h3",
        "display_name": "MiniMax H3 NF4 Turbo",
        "worker_python": Path("/tmp/python"),
        "working_directory": Path("/tmp/work"),
        "worker_script": "app/engine/backends/minimax_h3/h3_worker.py",
        "model_id": "DiffSynth-Studio/MiniMax-H3-NF4",
        "model_revision": "nf4-turbo4step-ckpt500",
        "processor_id": "MiniMax/MiniMax-H3",
        "lora_relpath": "models/loras/x.safetensors",
        "lora_alpha": 1.0,
    }
    values.update(overrides)
    return BackendConfig(**values)


# ---------------------------------------------------------------- 契約適合


def test_mock_engine_satisfies_engine_protocol(tmp_path):
    engine = MockEngine(
        identity=IDENTITY,
        assets_dir=MOCK_ASSETS_DIR,
        data_root=tmp_path,
        sleep_fn=lambda s: None,
    )
    try:
        assert isinstance(engine, Engine)
        # 契約の各メンバが実体を持つ（Protocol の isinstance は存在確認のみのため補強）
        assert isinstance(engine.identity, BackendIdentity)
        assert engine.capabilities.width == 576
        assert isinstance(engine.state(), EngineState)
        for name in ("start", "submit", "poll_event", "shutdown", "restart"):
            assert callable(getattr(engine, name)), name
    finally:
        engine.shutdown()


def test_incomplete_implementation_is_not_engine():
    class NotAnEngine:
        def start(self) -> None: ...

    assert not isinstance(NotAnEngine(), Engine)


def test_protocol_signatures_are_the_agreed_contract():
    poll = inspect.signature(Engine.poll_event)
    assert list(poll.parameters) == ["self", "timeout"]
    assert poll.parameters["timeout"].default is None

    shutdown = inspect.signature(Engine.shutdown)
    assert shutdown.parameters["timeout"].default == 5.0

    assert list(inspect.signature(Engine.submit).parameters) == ["self", "spec"]
    assert isinstance(inspect.getattr_static(Engine, "identity"), property)
    assert isinstance(inspect.getattr_static(Engine, "capabilities"), property)


# ---------------------------------------------------------------- identity 変換


def test_backend_identity_maps_config_fields():
    identity = backend_identity(_backend_config())
    assert identity == IDENTITY


def test_backend_identity_follows_config_values():
    identity = backend_identity(
        _backend_config(
            backend_id="other_model",
            display_name="Other",
            model_id="org/Other",
            model_revision="rev9",
        )
    )
    assert identity.backend_id == "other_model"
    assert identity.display_name == "Other"
    assert identity.model_id == "org/Other"
    assert identity.model_revision == "rev9"
    # 実行環境（worker_python 等）は identity に混ぜない（履歴へ記録するのは4項目のみ）
    assert set(vars(identity)) == {
        "backend_id",
        "display_name",
        "model_id",
        "model_revision",
    }


def test_backend_identity_from_shipped_config(monkeypatch):
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = load_config(PROJECT_ROOT)
    assert backend_identity(cfg.backend) == IDENTITY
