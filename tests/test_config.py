from pathlib import Path

import pytest

from app.core.config import ConfigError, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = PROJECT_ROOT / "config" / "config.toml"


def _load_modified(tmp_path, old: str, new: str):
    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    assert old in text, f"置換対象が見つかりません: {old}"
    modified = tmp_path / "config.toml"
    modified.write_text(text.replace(old, new), encoding="utf-8")
    return load_config(PROJECT_ROOT, config_path=modified)


def test_shipped_config_is_valid(monkeypatch):
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = load_config(PROJECT_ROOT)
    assert cfg.name == "ATELIER H3 Local Studio"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 7860
    assert cfg.allowed_num_frames == (56, 124)
    assert cfg.allowed_steps == (4, 8)
    # P5: 連結方式は「PTS正規化つき再エンコード」の1つだけ。設定から選べない
    assert not hasattr(cfg, "concat_reencode")
    assert cfg.warn_free_disk_gb == 20
    assert cfg.stop_free_disk_gb == 5
    assert cfg.data_root == (PROJECT_ROOT / "data").resolve()
    # v1.2: Execution Engine と Generation Backend の分離（§22）
    assert cfg.engine_mode == "real"  # P2 実機試験合格後、既定を real にした
    assert cfg.backend_id == "minimax_h3"
    assert cfg.backend.display_name == "MiniMax H3 NF4 Turbo"
    assert cfg.backend.model_id == "DiffSynth-Studio/MiniMax-H3-NF4"
    assert cfg.backend.model_revision == "nf4-turbo4step-ckpt500"
    # working_directory は各自の環境で異なる（config.toml は Git 管理外）ので、
    # 特定の絶対パスを固定しない。絶対パスであることと、末尾が DiffSynth-Studio
    # であることだけを確認する。
    assert cfg.backend.working_directory.is_absolute()
    assert cfg.backend.working_directory.name == "DiffSynth-Studio"


def test_env_override_mock(monkeypatch):
    monkeypatch.setenv("ATELIER_MOCK", "0")
    assert load_config(PROJECT_ROOT).engine_mode == "real"
    monkeypatch.setenv("ATELIER_MOCK", "1")
    assert load_config(PROJECT_ROOT).engine_mode == "mock"


def test_invalid_engine_mode(tmp_path):
    with pytest.raises(ConfigError, match='"real" か "mock"'):
        _load_modified(tmp_path, 'mode = "real"', 'mode = "banana"')


def test_backend_without_table_fails(tmp_path):
    with pytest.raises(ConfigError, match=r"\[backends.foo\] セクションがありません"):
        _load_modified(tmp_path, 'backend = "minimax_h3"', 'backend = "foo"')


def test_missing_file():
    with pytest.raises(ConfigError, match="見つかりません"):
        load_config(PROJECT_ROOT, config_path=Path("/nonexistent/config.toml"))


def test_host_must_be_localhost(tmp_path):
    with pytest.raises(ConfigError, match="127.0.0.1 固定"):
        _load_modified(tmp_path, 'host = "127.0.0.1"', 'host = "0.0.0.0"')


def test_frames_fixed_to_verified_values(tmp_path):
    with pytest.raises(ConfigError, match=r"\[56, 124\] 固定"):
        _load_modified(
            tmp_path,
            "allowed_num_frames = [56, 124]",
            "allowed_num_frames = [56, 124, 243]",
        )


def test_resolution_fixed(tmp_path):
    with pytest.raises(ConfigError, match="576 固定"):
        _load_modified(tmp_path, "width  = 576", "width  = 1024")


def test_disk_thresholds_order(tmp_path):
    with pytest.raises(ConfigError, match="閾値が不正"):
        _load_modified(
            tmp_path, "warn_free_disk_gb = 20", "warn_free_disk_gb = 1"
        )


# ==================================================== P5: concat.reencode の排除


def _with_extra(tmp_path, section: str, lines: str):
    """出荷 config.toml の指定セクション直後に行を差し込んで読み込む。"""
    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    assert section in text, section
    modified = tmp_path / "config.toml"
    modified.write_text(text.replace(section, section + "\n" + lines, 1), encoding="utf-8")
    return load_config(PROJECT_ROOT, config_path=modified)


def test_shipped_config_has_no_reencode_key():
    """未配線の -c copy が設定から選べるように見える状態を残さない。"""
    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    assert "\nreencode" not in text
    assert "reencode = " not in text


@pytest.mark.parametrize("value", ["true", "false"])
def test_concat_reencode_key_is_rejected(tmp_path, value):
    """書かれていたら日本語で「V1では未対応」と拒否する（true でも false でも）。"""
    with pytest.raises(ConfigError, match="V1 では未対応"):
        _with_extra(tmp_path, "[concat]", f"reencode = {value}")


def test_concat_reencode_error_tells_user_to_delete_the_line(tmp_path):
    with pytest.raises(ConfigError, match="この行を削除してください"):
        _with_extra(tmp_path, "[concat]", "reencode = true")


def test_app_config_has_no_concat_reencode_attribute(monkeypatch):
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = load_config(PROJECT_ROOT)
    assert "concat_reencode" not in cfg.__dataclass_fields__


# ==================================================== P5: [lan] セクション


def test_shipped_config_lan_defaults(monkeypatch):
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = load_config(PROJECT_ROOT)
    assert cfg.lan_host_override == ""
    assert cfg.lan_pin_digits == 6
    assert cfg.lan_max_auth_failures == 10
    assert cfg.lan_auth_lockout_sec == 30.0


def test_lan_section_is_optional(tmp_path):
    """[lan] が丸ごと無くても通常モードは起動できる（既定値になる）。"""
    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    start = text.index("[lan]")
    end = text.index("[logging]")
    modified = tmp_path / "config.toml"
    modified.write_text(text[:start] + text[end:], encoding="utf-8")
    cfg = load_config(PROJECT_ROOT, config_path=modified)
    assert cfg.lan_pin_digits == 6
    assert cfg.lan_host_override == ""
    assert cfg.lan_max_auth_failures == 10
    assert cfg.lan_auth_lockout_sec == 30.0


@pytest.mark.parametrize(
    "key", ["enabled", "enable", "auto_enable", "auto_start", "share", "pin"]
)
def test_lan_enable_style_keys_are_rejected(tmp_path, key):
    """設定ファイルだけで LAN 公開が始まる状態を作らせない（契約 §5.4）。"""
    with pytest.raises(ConfigError, match="--lan"):
        _with_extra(tmp_path, "[lan]", f"{key} = true")


def test_lan_enabled_error_explains_it_is_not_a_switch(tmp_path):
    with pytest.raises(ConfigError, match="設定ファイルだけでは有効になりません"):
        _with_extra(tmp_path, "[lan]", "enabled = true")


@pytest.mark.parametrize("digits", [3, 13, 0, -1])
def test_lan_pin_digits_out_of_range(tmp_path, digits):
    with pytest.raises(ConfigError, match="pin_digits は 4〜12"):
        _load_modified(tmp_path, "pin_digits        = 6", f"pin_digits        = {digits}")


@pytest.mark.parametrize("digits", [4, 6, 12])
def test_lan_pin_digits_in_range(tmp_path, digits):
    cfg = _load_modified(
        tmp_path, "pin_digits        = 6", f"pin_digits        = {digits}"
    )
    assert cfg.lan_pin_digits == digits


@pytest.mark.parametrize("value", ['"6"', "true", "6.5"])
def test_lan_pin_digits_wrong_type(tmp_path, value):
    with pytest.raises(ConfigError, match="pin_digits"):
        _load_modified(tmp_path, "pin_digits        = 6", f"pin_digits        = {value}")


def test_lan_max_auth_failures_must_be_positive(tmp_path):
    with pytest.raises(ConfigError, match="max_auth_failures は 1 以上"):
        _load_modified(
            tmp_path, "max_auth_failures = 10", "max_auth_failures = 0"
        )


@pytest.mark.parametrize("bad", ["-1", "0", "0.5"])
def test_lan_auth_lockout_sec_must_be_at_least_one_second(tmp_path, bad):
    """0 を許すとレート制限が無効になり、PIN の総当たりを止められなくなる。

    相互レビューで「`auth_lockout_sec = 0` にすると誤PINを何回入れてもロックされない」
    ことが実測で確認されたため、下限を 1 秒にした。
    """
    with pytest.raises(ConfigError, match="auth_lockout_sec は 1 以上"):
        _load_modified(tmp_path, "auth_lockout_sec  = 30", f"auth_lockout_sec  = {bad}")


def test_lan_auth_lockout_sec_accepts_float(tmp_path):
    cfg = _load_modified(tmp_path, "auth_lockout_sec  = 30", "auth_lockout_sec  = 2.5")
    assert cfg.lan_auth_lockout_sec == pytest.approx(2.5)


@pytest.mark.parametrize(
    "host", ["192.168.1.23", "10.0.0.5", "172.16.0.1", "172.31.255.254"]
)
def test_lan_host_override_accepts_private_ipv4(tmp_path, host):
    cfg = _load_modified(
        tmp_path, 'host_override     = ""', f'host_override     = "{host}"'
    )
    assert cfg.lan_host_override == host


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",            # グローバル
        "0.0.0.0",            # 全インタフェース
        "127.0.0.1",          # ループバック
        "169.254.1.1",        # link-local
        "100.64.0.1",         # CGNAT / VPN オーバーレイ
        "172.32.0.1",         # RFC1918 の外
        "::1",                # IPv6
        "example.local",      # ホスト名
        "http://192.168.1.5", # URL
        "192.168.1.23 ",      # 末尾空白つき（strip 後に検証される想定）
    ],
)
def test_lan_host_override_rejects_non_private_ipv4(tmp_path, host):
    if host.strip() == "192.168.1.23":
        # strip して正規化されるので、これだけは通るのが正しい
        cfg = _load_modified(
            tmp_path, 'host_override     = ""', f'host_override     = "{host}"'
        )
        assert cfg.lan_host_override == "192.168.1.23"
        return
    with pytest.raises(ConfigError, match="host_override"):
        _load_modified(
            tmp_path, 'host_override     = ""', f'host_override     = "{host}"'
        )


def test_lan_host_override_wrong_type(tmp_path):
    with pytest.raises(ConfigError, match="host_override"):
        _load_modified(tmp_path, 'host_override     = ""', "host_override     = 42")


def test_lan_must_be_a_table(tmp_path):
    text = SHIPPED_CONFIG.read_text(encoding="utf-8")
    start = text.index("[lan]")
    end = text.index("[logging]")
    modified = tmp_path / "config.toml"
    # [lan] を削ったうえで、トップレベルへ「テーブルではない lan」を置く
    stripped = text[:start] + text[end:]
    stripped = stripped.replace(
        "schema_version = 1", 'schema_version = 1\nlan = "yes"', 1
    )
    modified.write_text(stripped, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\[lan\] はセクション"):
        load_config(PROJECT_ROOT, config_path=modified)


# ==================================================== P5: config.example.toml

EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "config.example.toml"


def test_example_config_exists_and_loads(monkeypatch):
    """雛形も設定として妥当であること（コピーすればそのまま検証を通る）。"""
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    assert EXAMPLE_CONFIG.is_file()
    cfg = load_config(PROJECT_ROOT, config_path=EXAMPLE_CONFIG)
    assert cfg.backend_id == "minimax_h3"
    assert cfg.host == "127.0.0.1"
    assert cfg.lan_pin_digits == 6


def test_example_config_has_no_personal_information():
    """個人名・ユーザー名・実在の絶対パス・秘密情報を含めない（契約 §5.4）。"""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in ("daisuke", "/users/", "/home/", "secret", "password", "token"):
        assert forbidden not in lowered, forbidden
    # worker_python / working_directory はプレースホルダであること
    assert '"/path/to/DiffSynth-Studio/.venv/bin/python"' in text
    assert '"/path/to/DiffSynth-Studio"' in text


def test_example_config_has_no_pin_value():
    """PIN を設定ファイルへ書かせない（P5契約 §2）。"""
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "\npin =" not in text
    assert "\npin=" not in text


def test_example_config_has_no_reencode_and_no_lan_switch():
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert "reencode = " not in text
    assert "\nenabled" not in text


def test_example_config_covers_the_same_sections_as_shipped():
    """雛形が古くならないよう、セクション構成の一致を機械的に確認する。"""
    import tomllib

    shipped = tomllib.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    example = tomllib.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert sorted(shipped) == sorted(example)
    for name, section in shipped.items():
        if isinstance(section, dict):
            assert sorted(section) == sorted(example[name]), name
