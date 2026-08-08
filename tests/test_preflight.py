import socket
from pathlib import Path

from app.core.config import load_config
from app.core.preflight import check_port_free, format_report, run_preflight

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cfg(free_port: bool = True):
    """テスト用の設定。

    既定では**空きポート**を割り当てる。ポート 7860 固定のままだと、
    開発者がアプリを起動しているだけで preflight の総合判定テストが落ちるため
    （テストの目的はポートの空き状況ではない）。
    """
    cfg = load_config(PROJECT_ROOT)
    if free_port:
        import dataclasses
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = dataclasses.replace(cfg, port=port)
    return cfg


def test_port_check_detects_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        msg = check_port_free("127.0.0.1", busy_port)
        assert msg is not None and "使用中" in msg


def test_port_check_free_port_ok():
    assert check_port_free("127.0.0.1", 0) is None


def test_disk_warn_message(tmp_path, monkeypatch):
    cfg = _cfg()
    result = run_preflight(cfg, "mock", free_gb_override=10.0)
    assert any("20GB未満" in w for w in result.warnings)


def test_disk_stop_message():
    cfg = _cfg()
    result = run_preflight(cfg, "mock", free_gb_override=3.0)
    assert any("受付を停止" in w for w in result.warnings)


def test_mock_mode_reports_missing_assets_in_japanese(tmp_path):
    """モック素材が無い場合、日本語エラーで setup.sh を案内する（異常系）。"""
    cfg = _cfg()
    if all(
        (cfg.assets_mock_dir / n).is_file()
        for n in ("mock_56.mp4", "mock_124.mp4", "mock_56_last.png", "mock_124_last.png")
    ):
        # 素材生成済み環境では正常系を確認
        result = run_preflight(cfg, "mock", free_gb_override=100.0)
        assert result.ok
    else:
        result = run_preflight(cfg, "mock", free_gb_override=100.0)
        assert any("setup.sh" in e for e in result.errors)


def test_unregistered_backend_rejected(tmp_path):
    """未登録の backend_id は preflight が日本語エラーで拒否する（§22.1）。"""
    text = (PROJECT_ROOT / "config" / "config.toml").read_text(encoding="utf-8")
    text = text.replace('backend = "minimax_h3"', 'backend = "foo"')
    text += (
        "\n[backends.foo]\n"
        'display_name = "Unknown Backend"\n'
        'worker_python = "/usr/bin/true"\n'
        'working_directory = "/tmp"\n'
        'worker_script = "none.py"\n'
        'model_id = "x"\n'
        'model_revision = "x"\n'
        'processor_id = "x"\n'
        'lora_relpath = "x"\n'
        "lora_alpha = 1.0\n"
    )
    modified = tmp_path / "config.toml"
    modified.write_text(text, encoding="utf-8")
    cfg = load_config(PROJECT_ROOT, config_path=modified)
    result = run_preflight(cfg, "mock", free_gb_override=100.0)
    assert any("未登録の生成バックエンド" in e for e in result.errors)
    assert not result.ok


def test_registered_backend_reported(tmp_path):
    cfg = _cfg()
    result = run_preflight(cfg, "mock", free_gb_override=100.0)
    assert any("minimax_h3" in i for i in result.infos)


def test_format_report_japanese():
    cfg = _cfg()
    result = run_preflight(cfg, "mock", free_gb_override=100.0)
    report = format_report(result, "mock")
    assert "起動前チェック" in report
    assert ("合格" in report) or ("不合格" in report)


def test_worker_script_missing_is_rejected(tmp_path):
    """実機モードではワーカースクリプトの存在も検査する（設計書 §22.6）。"""
    import dataclasses

    cfg = _cfg()
    broken_backend = dataclasses.replace(
        cfg.backend, worker_script="app/engine/backends/minimax_h3/does_not_exist.py"
    )
    broken = dataclasses.replace(cfg, backend=broken_backend)
    result = run_preflight(broken, "real", free_gb_override=100.0)
    assert any("ワーカースクリプトが見つかりません" in e for e in result.errors)
    assert not result.ok


def test_worker_script_present_in_real_mode(tmp_path):
    cfg = _cfg()
    result = run_preflight(cfg, "real", free_gb_override=100.0)
    assert not any("ワーカースクリプト" in e for e in result.errors)


# ===================================================== P5: LANモードのチェック（§5.5）


import dataclasses  # noqa: E402

import pytest  # noqa: E402

from app.core import network  # noqa: E402
from app.core import preflight as pf  # noqa: E402


def _lan_cfg(**overrides):
    cfg = _cfg()
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


def _fake_interfaces(monkeypatch, interfaces):
    monkeypatch.setattr(network, "list_lan_interfaces", lambda **kw: list(interfaces))


def _port_always_free(monkeypatch):
    monkeypatch.setattr(pf, "check_lan_port", lambda host, port: None)


def test_lan_checks_are_skipped_by_default(monkeypatch):
    """既定（通常モード）では LAN の検出を一切行わない（挙動を変えない）。"""
    called: list[str] = []
    monkeypatch.setattr(
        network, "list_lan_interfaces", lambda **kw: called.append("x") or []
    )
    result = run_preflight(_cfg(), "mock", free_gb_override=100.0)
    assert called == []
    assert not any("iPhone接続モード" in i for i in result.infos)
    assert not any("iPhone接続モード" in e for e in result.errors)


def test_lan_check_reports_detected_address(monkeypatch):
    _fake_interfaces(monkeypatch, [network.LanInterface("en0", "192.168.1.23")])
    _port_always_free(monkeypatch)
    cfg = _lan_cfg()
    result = pf.check_lan(cfg)
    assert result.ok
    assert any("192.168.1.23" in i for i in result.infos)
    assert any(f"http://192.168.1.23:{cfg.port}" in i for i in result.infos)


def test_lan_check_guides_the_user_when_no_private_ipv4(monkeypatch):
    """見つからないときは Wi-Fi / ゲストWi-Fi を日本語で案内する（契約 §5.5）。"""
    _fake_interfaces(monkeypatch, [])
    result = pf.check_lan(_lan_cfg())
    assert not result.ok
    joined = "\n".join(result.errors)
    assert "Wi-Fi" in joined
    assert "ゲスト" in joined
    assert "iPhone接続モードを開始できません" in joined


def test_lan_check_lists_multiple_candidates(monkeypatch):
    _fake_interfaces(
        monkeypatch,
        [
            network.LanInterface("en0", "192.168.1.23"),
            network.LanInterface("en1", "10.0.0.5"),
        ],
    )
    _port_always_free(monkeypatch)
    result = pf.check_lan(_lan_cfg())
    assert result.ok
    joined = "\n".join(result.infos)
    assert "192.168.1.23" in joined and "10.0.0.5" in joined
    assert "--lan-host" in joined


def test_lan_check_uses_config_host_override(monkeypatch):
    """config の host_override があれば自動検出しない。"""
    called: list[str] = []
    monkeypatch.setattr(
        network, "list_lan_interfaces", lambda **kw: called.append("x") or []
    )
    _port_always_free(monkeypatch)
    cfg = _lan_cfg(lan_host_override="192.168.5.10")
    result = pf.check_lan(cfg)
    assert result.ok
    assert called == []
    assert any("192.168.5.10" in i for i in result.infos)
    assert any("手動指定" in i for i in result.infos)


def test_lan_check_cli_host_beats_config(monkeypatch):
    _port_always_free(monkeypatch)
    cfg = _lan_cfg(lan_host_override="192.168.5.10")
    result = pf.check_lan(cfg, "10.1.2.3")
    assert result.ok
    assert any("10.1.2.3" in i for i in result.infos)
    assert not any("192.168.5.10" in i for i in result.infos)


@pytest.mark.parametrize(
    "bad", ["8.8.8.8", "0.0.0.0", "127.0.0.1", "169.254.1.1", "100.64.0.1", "not-an-ip"]
)
def test_lan_check_rejects_non_private_host(monkeypatch, bad):
    _port_always_free(monkeypatch)
    result = pf.check_lan(_lan_cfg(), bad)
    assert not result.ok
    assert any("iPhone接続モードを開始できません" in e for e in result.errors)


def test_lan_check_reports_busy_port(monkeypatch):
    _fake_interfaces(monkeypatch, [network.LanInterface("en0", "192.168.1.23")])
    monkeypatch.setattr(
        pf,
        "check_lan_port",
        lambda host, port: f"iPhone接続モードで使うポートが空いていません（{host}:{port}）",
    )
    result = pf.check_lan(_lan_cfg())
    assert not result.ok
    joined = "\n".join(result.errors)
    assert "ポートが空いていません" in joined
    assert "192.168.1.23" in joined


def test_run_preflight_merges_lan_results(monkeypatch):
    """run_preflight(..., lan=True) で LAN の結果が本体のレポートへ合流する。"""
    _fake_interfaces(monkeypatch, [network.LanInterface("en0", "192.168.1.23")])
    monkeypatch.setattr(pf, "check_lan_port", lambda host, port: None)
    result = run_preflight(_cfg(), "mock", free_gb_override=100.0, lan=True)
    assert result.ok
    assert any("iPhone接続モード" in i for i in result.infos)
    report = format_report(result, "mock")
    assert "iPhone接続モード" in report
    assert "合格" in report


def test_run_preflight_lan_failure_blocks_startup(monkeypatch):
    _fake_interfaces(monkeypatch, [])
    result = run_preflight(_cfg(), "mock", free_gb_override=100.0, lan=True)
    assert not result.ok
    assert "不合格" in format_report(result, "mock")


def test_run_preflight_lan_is_keyword_only(monkeypatch):
    """既存の位置引数呼び出しを壊していないこと（signature 互換）。"""
    import inspect

    sig = inspect.signature(run_preflight)
    assert sig.parameters["lan"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["lan"].default is False
    assert sig.parameters["lan_host"].kind is inspect.Parameter.KEYWORD_ONLY
    # 既存の呼び出し形（位置引数4つまで）がそのまま通る
    run_preflight(_cfg(), "mock", False, 100.0)


def test_lan_port_check_distinguishes_missing_address_from_busy_port():
    """--lan-host の打ち間違いと「ポート使用中」を別の文言で案内する。"""
    missing = pf.check_lan_port("192.168.99.99", 7860)
    assert missing is not None
    assert "割り当てられていません" in missing
    assert "--lan-host" in missing

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy_port = sock.getsockname()[1]
        busy = pf.check_lan_port("127.0.0.1", busy_port)
    assert busy is not None
    assert "ポートが空いていません" in busy


def test_lan_port_check_ok_on_loopback():
    assert pf.check_lan_port("127.0.0.1", 0) is None
