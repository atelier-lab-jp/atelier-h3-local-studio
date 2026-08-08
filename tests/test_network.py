"""LAN IP 検出のユニットテスト（P5契約 §2・§7.2）。

実ネットワークには依存させない（`ifconfig` の出力はすべて注入する）。
プロジェクトの `data/` には一切書き込まない。
"""

from __future__ import annotations

import ipaddress
import subprocess

import pytest

from app.core.network import (
    NO_LAN_MESSAGE,
    RFC1918_NETWORKS,
    LanInfo,
    LanInterface,
    NetworkError,
    build_lan_info,
    describe_interfaces,
    detect_lan_ipv4,
    format_lan_banner,
    is_private_ipv4,
    list_lan_interfaces,
    parse_ifconfig,
    probe_route_ipv4,
    read_ifconfig,
    sort_interfaces,
    validate_lan_host,
)

# 実機（macOS）の `ifconfig -a` を模した出力。
# lo0 / link-local / CGNAT / IPv6 / VPN(utun) を混ぜてある。
IFCONFIG_SAMPLE = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
gif0: flags=8010<POINTOPOINT,MULTICAST> mtu 1280
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 1c:f6:4c:6c:cc:87
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 fe80::1c:f6ff:fe4c:cc87%en0 prefixlen 64 secured scopeid 0xc
\tstatus: active
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.0.0.5 netmask 0xffffff00 broadcast 10.0.0.255
\tstatus: active
en8: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 172.16.9.9 netmask 0xffff0000
awdl0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::9c:2fff:fe1a:3c%awdl0 prefixlen 64 scopeid 0x10
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 169.254.13.7 netmask 0xffff0000 broadcast 169.254.255.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 100.72.4.9 --> 100.72.4.9 netmask 0xffffffff
utun9: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1500
\tinet 198.51.100.42 netmask 0xffffff00
"""


# ---------------------------------------------------------------- RFC1918 判定


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "10.255.255.254",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.0.1",
        "192.168.1.23",
        "192.168.255.254",
    ],
)
def test_private_ipv4_accepted(ip: str):
    assert is_private_ipv4(ip) is True
    assert validate_lan_host(ip) == ip


@pytest.mark.parametrize(
    "ip",
    [
        "0.0.0.0",              # 全インタフェース bind は禁止
        "127.0.0.1",            # loopback は LAN ではない
        "127.1.2.3",
        "169.254.10.1",         # link-local
        "100.64.0.1",           # CGNAT / VPN オーバーレイ
        "100.127.255.255",
        "172.15.0.1",           # 172.16/12 の下限外
        "172.32.0.1",           # 172.16/12 の上限外
        "8.8.8.8",              # public
        "198.51.100.42",        # TEST-NET-3（is_private は True を返してしまう）
        "203.0.113.9",
        "224.0.0.1",            # multicast
        "255.255.255.255",
        "192.168.1.256",        # 範囲外
        "192.168.1",            # 桁不足
        "192.168.01.1",         # 先頭ゼロ（曖昧表記）
        "192.168.1.23:7860",    # ポート付き
        "http://192.168.1.23",  # URL
        "data:text/plain,x",
        "localhost",
        "mac.local",
        "::1",                  # IPv6
        "fe80::1",
        "fd00::1",              # IPv6 ULA も対象外
        "",
        "   ",
        "192.168.1.23 192.168.1.24",
    ],
)
def test_non_private_ipv4_rejected(ip: str):
    assert is_private_ipv4(ip) is False
    with pytest.raises(NetworkError):
        validate_lan_host(ip)


def test_reject_non_string_host():
    for value in (None, 1234, 3232235777, b"192.168.1.23", ["192.168.1.23"]):
        assert is_private_ipv4(value) is False
        with pytest.raises(NetworkError, match="文字列"):
            validate_lan_host(value)  # type: ignore[arg-type]


def test_validate_strips_surrounding_whitespace():
    assert validate_lan_host("  192.168.1.23\n") == "192.168.1.23"


def test_rejection_message_is_japanese_and_explains_allowed_range():
    with pytest.raises(NetworkError) as excinfo:
        validate_lan_host("8.8.8.8")
    msg = str(excinfo.value)
    assert "家庭内LAN" in msg
    assert "192.168" in msg


def test_rfc1918_networks_are_exactly_the_three_private_blocks():
    assert [str(n) for n in RFC1918_NETWORKS] == [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]


def test_stdlib_is_private_is_not_used_as_the_criterion():
    """`ipaddress.is_private` は LAN 判定には使えない（根拠を固定する）。"""
    for ip in ("0.0.0.0", "169.254.1.1", "198.51.100.1", "127.0.0.1"):
        assert ipaddress.ip_address(ip).is_private is True   # 標準ライブラリの挙動
        assert is_private_ipv4(ip) is False                  # 本アプリの判定


# ---------------------------------------------------------------- ifconfig パース


def test_parse_ifconfig_picks_only_private_ipv4():
    found = parse_ifconfig(IFCONFIG_SAMPLE)
    assert [(f.name, f.ip) for f in found] == [
        ("en0", "192.168.1.23"),
        ("en1", "10.0.0.5"),
        ("en8", "172.16.9.9"),
    ]


def test_parse_ifconfig_excludes_loopback_linklocal_cgnat_ipv6_public():
    ips = {f.ip for f in parse_ifconfig(IFCONFIG_SAMPLE)}
    for excluded in ("127.0.0.1", "169.254.13.7", "100.72.4.9", "198.51.100.42"):
        assert excluded not in ips
    assert not any(":" in f.ip for f in parse_ifconfig(IFCONFIG_SAMPLE))


def test_parse_ifconfig_handles_empty_and_garbage():
    assert parse_ifconfig("") == []
    assert parse_ifconfig("\n\n") == []
    assert parse_ifconfig("これは ifconfig の出力ではありません") == []
    # 先頭が空白の孤児行（インタフェース名が未確定）でも壊れない
    assert parse_ifconfig("\tinet 192.168.1.1 netmask 0xffffff00") == []


def test_parse_ifconfig_deduplicates_identical_entries():
    text = (
        "en0: flags=8863<UP> mtu 1500\n"
        "\tinet 192.168.1.23 netmask 0xffffff00\n"
        "\tinet 192.168.1.23 netmask 0xffffff00\n"
    )
    assert parse_ifconfig(text) == [LanInterface(name="en0", ip="192.168.1.23")]


def test_parse_ifconfig_keeps_multiple_ips_on_one_interface():
    text = (
        "en0: flags=8863<UP> mtu 1500\n"
        "\tinet 192.168.1.99 netmask 0xffffff00\n"
        "\tinet 192.168.1.23 netmask 0xffffff00\n"
    )
    assert [f.ip for f in parse_ifconfig(text)] == ["192.168.1.99", "192.168.1.23"]


# ---------------------------------------------------------------- 並び順


def test_sort_order_is_en0_then_en1_then_name_ascending():
    unsorted_ = [
        LanInterface("en8", "172.16.9.9"),
        LanInterface("bridge0", "192.168.5.1"),
        LanInterface("en1", "10.0.0.5"),
        LanInterface("en0", "192.168.1.23"),
    ]
    assert [f.name for f in sort_interfaces(unsorted_)] == [
        "en0",
        "en1",
        "bridge0",
        "en8",
    ]


def test_sort_order_breaks_ties_by_ip_ascending():
    same_name = [
        LanInterface("en0", "192.168.1.100"),
        LanInterface("en0", "192.168.1.23"),
        LanInterface("en0", "10.0.0.5"),
    ]
    assert [f.ip for f in sort_interfaces(same_name)] == [
        "10.0.0.5",
        "192.168.1.23",
        "192.168.1.100",  # 文字列順なら 100 < 23 になってしまうので数値順を確認
    ]


def test_sort_is_stable_and_deterministic():
    found = parse_ifconfig(IFCONFIG_SAMPLE)
    assert sort_interfaces(found) == sort_interfaces(sort_interfaces(found))


# ---------------------------------------------------------------- 一覧・検出


def test_list_lan_interfaces_uses_injected_reader():
    found = list_lan_interfaces(ifconfig_reader=lambda: IFCONFIG_SAMPLE)
    assert [f.ip for f in found] == ["192.168.1.23", "10.0.0.5", "172.16.9.9"]


def test_list_lan_interfaces_falls_back_to_route_probe():
    probed = [LanInterface("", "192.168.77.4")]
    found = list_lan_interfaces(
        ifconfig_reader=lambda: "", route_prober=lambda: list(probed)
    )
    assert found == probed


def test_list_lan_interfaces_survives_reader_exception():
    def boom() -> str:
        raise OSError("ifconfig が壊れている想定")

    assert list_lan_interfaces(ifconfig_reader=boom, route_prober=lambda: []) == []


def test_list_lan_interfaces_survives_prober_exception():
    def boom():
        raise OSError("経路表を引けない想定")

    assert list_lan_interfaces(ifconfig_reader=lambda: "", route_prober=boom) == []


def test_detect_prefers_explicit_host_over_autodetection():
    ifaces = [LanInterface("en0", "192.168.1.23")]
    assert detect_lan_ipv4("10.1.2.3", interfaces=ifaces) == "10.1.2.3"


def test_detect_rejects_invalid_explicit_host():
    with pytest.raises(NetworkError):
        detect_lan_ipv4("8.8.8.8", interfaces=[LanInterface("en0", "192.168.1.23")])


def test_detect_treats_blank_override_as_absent():
    ifaces = [LanInterface("en0", "192.168.1.23")]
    for blank in (None, "", "   "):
        assert detect_lan_ipv4(blank, interfaces=ifaces) == "192.168.1.23"


def test_detect_picks_first_of_sorted_candidates():
    ifaces = list_lan_interfaces(ifconfig_reader=lambda: IFCONFIG_SAMPLE)
    assert detect_lan_ipv4(interfaces=ifaces) == "192.168.1.23"


def test_detect_without_any_interface_raises_japanese_guidance():
    with pytest.raises(NetworkError) as excinfo:
        detect_lan_ipv4(interfaces=[])
    msg = str(excinfo.value)
    assert "Wi-Fi" in msg
    assert "ゲスト" in msg
    assert "--lan-host" in msg
    assert msg == NO_LAN_MESSAGE


# ---------------------------------------------------------------- 外部コマンド呼び出し


def test_read_ifconfig_uses_argument_array_without_shell(monkeypatch):
    calls: list[tuple] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=IFCONFIG_SAMPLE, stderr="")

    monkeypatch.setattr("app.core.network.subprocess.run", fake_run)
    text = read_ifconfig()

    assert text == IFCONFIG_SAMPLE
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert argv[0].endswith("ifconfig")
    assert argv[1] == "-a"
    assert "shell" not in kwargs or kwargs["shell"] is False
    assert kwargs.get("timeout")  # 応答なしでハングしない


def test_read_ifconfig_returns_empty_when_command_missing(monkeypatch):
    def boom(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr("app.core.network.subprocess.run", boom)
    assert read_ifconfig() == ""


def test_read_ifconfig_returns_empty_on_timeout(monkeypatch):
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr("app.core.network.subprocess.run", boom)
    assert read_ifconfig() == ""


def test_probe_route_returns_only_private_addresses():
    """実機の経路表を使うが、返るのは必ずプライベート IPv4 だけ。"""
    for iface in probe_route_ipv4():
        assert is_private_ipv4(iface.ip)


# ---------------------------------------------------------------- LanInfo


def test_build_lan_info_composes_url():
    info = build_lan_info("192.168.1.23", 7860)
    assert info == LanInfo(url="http://192.168.1.23:7860", host="192.168.1.23", port=7860)


def test_lan_info_is_frozen_and_has_no_pin_field():
    info = build_lan_info("192.168.1.23", 7860)
    with pytest.raises(Exception):
        info.host = "10.0.0.1"  # type: ignore[misc]
    assert not any("pin" in name.lower() for name in info.__dataclass_fields__)


def test_build_lan_info_rejects_non_private_host():
    with pytest.raises(NetworkError):
        build_lan_info("0.0.0.0", 7860)


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, 99999, -1])
def test_build_lan_info_rejects_out_of_range_port(port: int):
    with pytest.raises(NetworkError, match="ポート"):
        build_lan_info("192.168.1.23", port)


def test_build_lan_info_rejects_non_int_port():
    for bad in ("7860", None, True, 7860.0):
        with pytest.raises(NetworkError, match="ポート"):
            build_lan_info("192.168.1.23", bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 画面表示


def test_describe_interfaces_is_empty_for_single_candidate():
    assert describe_interfaces([LanInterface("en0", "192.168.1.23")]) == ""
    assert describe_interfaces([]) == ""


def test_describe_interfaces_lists_all_candidates_and_override_hint():
    found = list_lan_interfaces(ifconfig_reader=lambda: IFCONFIG_SAMPLE)
    text = describe_interfaces(found)
    for ip in ("192.168.1.23", "10.0.0.5", "172.16.9.9"):
        assert ip in text
    assert "--lan-host" in text
    assert "今回使用" in text


def test_banner_contains_url_pin_username_and_japanese_notes():
    info = build_lan_info("192.168.1.23", 7860)
    banner = format_lan_banner(info, "123456")
    assert "http://192.168.1.23:7860" in banner
    assert "123456" in banner            # PIN はターミナル表示にだけ出す
    assert "h3" in banner                # ユーザー名
    assert "同じWi-Fi" in banner
    assert "インターネットには公開していません" in banner
    assert "Control + C" in banner
    assert "許可" in banner              # ファイアウォールのダイアログ案内
    assert "ゲスト用Wi-Fi" in banner


def test_banner_includes_qr_when_given(tmp_path):
    info = build_lan_info("192.168.1.23", 7860)
    png = tmp_path / "lan_qr.png"
    banner = format_lan_banner(info, "123456", qr_ascii="█▀█", qr_png=png)
    assert "█▀█" in banner
    assert str(png) in banner


def test_banner_lists_candidates_when_multiple(tmp_path):
    info = build_lan_info("192.168.1.23", 7860)
    found = list_lan_interfaces(ifconfig_reader=lambda: IFCONFIG_SAMPLE)
    banner = format_lan_banner(info, "123456", interfaces=found)
    assert "10.0.0.5" in banner
    assert "--lan-host" in banner
