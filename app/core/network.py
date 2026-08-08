"""LANモード用のネットワーク検出（P5契約 §2・§7.2）。

同一 LAN（同じルーター配下）の iPhone からだけ触れるように、
**RFC1918 のプライベート IPv4 だけ**を許可して、そこへ明示 bind する。
`0.0.0.0` へは決して bind しない（全インタフェースへの公開を避けるため）。

許可: 10.0.0.0/8 ・ 172.16.0.0/12 ・ 192.168.0.0/16
拒否: グローバル IP ・ 0.0.0.0 ・ 127.0.0.0/8 ・ 169.254.0.0/16（link-local）・
      100.64.0.0/10（CGNAT / VPN オーバーレイ。家庭内 LAN ではない）・IPv6 ・
      ホスト名 ・ URL ・ その他の文字列

注意: `ipaddress.ip_address(s).is_private` は使わない。
標準ライブラリの `is_private` は 0.0.0.0 ・ 169.254/16 ・ 198.51.100/24 なども True を返し、
「家庭内 LAN かどうか」の判定にはならない（実測で確認済み）。
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.lanauth import LAN_USERNAME

# 家庭内 LAN として許可するアドレス範囲（RFC1918 のみ）
RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

# 選択順序を予測可能に固定する（Wi-Fi → 有線 → その他を名前昇順）
PREFERRED_INTERFACES = ("en0", "en1")

_IFCONFIG_CANDIDATES = ("/sbin/ifconfig", "/usr/sbin/ifconfig", "ifconfig")

# 経路表の参照だけを行うプローブ先（UDP connect はパケットを送信しない）
_PROBE_TARGETS = ("192.168.255.255", "10.255.255.255", "172.31.255.255")


class NetworkError(Exception):
    """LAN の検出・検証に失敗した（日本語メッセージ）。"""


@dataclass(frozen=True)
class LanInterface:
    """プライベート IPv4 を持つネットワークインタフェース1件。"""

    name: str  # "en0"
    ip: str    # "192.168.1.23"


@dataclass(frozen=True)
class LanInfo:
    """UI 層へ渡す接続情報。**PIN は絶対に含めない**（P5契約 §3）。"""

    url: str   # "http://192.168.1.23:7860"
    host: str
    port: int


# ---------------------------------------------------------------- 判定


def is_private_ipv4(value: object) -> bool:
    """RFC1918 のプライベート IPv4 文字列なら True（例外を投げない）。"""
    try:
        _parse_private_ipv4(value)
    except NetworkError:
        return False
    return True


def _parse_private_ipv4(value: object) -> ipaddress.IPv4Address:
    if not isinstance(value, str):
        raise NetworkError("IPアドレスは文字列で指定してください")
    text = value.strip()
    if not text:
        raise NetworkError("IPアドレスが指定されていません")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as e:
        raise NetworkError(
            f"IPアドレスとして解釈できません: {value!r}"
            "（例: 192.168.1.23 のような数字だけの形式で指定してください。"
            "ホスト名・URL・ポート番号付きは使えません）"
        ) from e
    if not isinstance(addr, ipaddress.IPv4Address):
        raise NetworkError(
            f"IPv6 アドレスは使えません: {text}（IPv4 のプライベートアドレスを指定してください）"
        )
    if not any(addr in net for net in RFC1918_NETWORKS):
        raise NetworkError(
            f"家庭内LANのアドレスではありません: {text}\n"
            "    使えるのは 10.x.x.x / 172.16〜31.x.x / 192.168.x.x だけです"
            "（同じWi-Fi・同じルーターの中だけで使う設計のため）"
        )
    return addr


def validate_lan_host(host: str) -> str:
    """プライベート IPv4 のみ許可し、正規化した文字列を返す。

    それ以外（グローバルIP・0.0.0.0・127.x・169.254.x・100.64.x・IPv6・
    ホスト名・URL など）は NetworkError。
    """
    return str(_parse_private_ipv4(host))


# ---------------------------------------------------------------- 検出


def parse_ifconfig(text: str) -> list[LanInterface]:
    """`ifconfig -a` の出力からプライベート IPv4 を持つインタフェースを取り出す。

    実ネットワークに依存させずテストできるよう、パースだけを独立させている。
    """
    found: list[LanInterface] = []
    seen: set[tuple[str, str]] = set()
    current: str | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[0].isspace():
            # 例: "en0: flags=8863<UP,BROADCAST,...> mtu 1500"
            name = raw.split(":", 1)[0].strip()
            current = name or None
            continue
        stripped = raw.strip()
        if current is None or not stripped.startswith("inet "):
            continue  # inet6 はここで除外される
        parts = stripped.split()
        if len(parts) < 2:
            continue
        ip = parts[1]
        if not is_private_ipv4(ip):
            continue
        key = (current, ip)
        if key in seen:
            continue
        seen.add(key)
        found.append(LanInterface(name=current, ip=ip))
    return found


def _sort_key(iface: LanInterface) -> tuple[int, str, int]:
    try:
        rank = PREFERRED_INTERFACES.index(iface.name)
    except ValueError:
        rank = len(PREFERRED_INTERFACES)
    return (rank, iface.name, int(ipaddress.IPv4Address(iface.ip)))


def sort_interfaces(interfaces: Sequence[LanInterface]) -> list[LanInterface]:
    """en0 → en1 → その他を名前昇順、同点なら IP 昇順に整列する。"""
    return sorted(interfaces, key=_sort_key)


def read_ifconfig(timeout: float = 10.0) -> str:
    """`ifconfig -a` を引数配列で実行して出力を返す（shell=True は使わない）。"""
    for path in _IFCONFIG_CANDIDATES:
        try:
            proc = subprocess.run(
                [path, "-a"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    return ""


def probe_route_ipv4() -> list[LanInterface]:
    """経路表からローカルアドレスを推定する予備手段。

    UDP ソケットの connect() は**パケットを送信しない**（経路表の参照だけ）。
    インタフェース名は分からないので空文字にする。
    """
    found: list[LanInterface] = []
    for target in _PROBE_TARGETS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((target, 1))
            ip = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        if is_private_ipv4(ip) and all(f.ip != ip for f in found):
            found.append(LanInterface(name="", ip=ip))
    return found


def list_lan_interfaces(
    *,
    ifconfig_reader: Callable[[], str] | None = None,
    route_prober: Callable[[], list[LanInterface]] | None = None,
) -> list[LanInterface]:
    """RFC1918 のプライベート IPv4 を持つインタフェースだけを優先順に返す。

    引数はテスト用の注入口（seam）。既定では実機の `ifconfig -a` を読む。
    """
    reader = ifconfig_reader or read_ifconfig
    try:
        text = reader()
    except Exception:  # 検出経路でアプリを落とさない
        text = ""
    interfaces = parse_ifconfig(text or "")
    if not interfaces:
        prober = route_prober or probe_route_ipv4
        try:
            interfaces = list(prober())
        except Exception:
            interfaces = []
    return sort_interfaces(interfaces)


NO_LAN_MESSAGE = (
    "同じWi-Fi内で使えるIPアドレス（192.168.x.x など）が見つかりませんでした。\n"
    "    次を確認してください:\n"
    "      1. この Mac が Wi-Fi または有線LANに接続されているか\n"
    "      2. 「ゲスト用Wi-Fi」や「端末間の通信が禁止されたWi-Fi」に繋がっていないか\n"
    "      3. iPhone と Mac が同じルーター（同じWi-Fi名）に繋がっているか\n"
    "    ルーターのIPが分かる場合は --lan-host 192.168.1.23 のように直接指定できます"
)


def detect_lan_ipv4(
    preferred: str | None = None,
    *,
    interfaces: Sequence[LanInterface] | None = None,
) -> str:
    """LANモードで bind するプライベート IPv4 を決める。

    `preferred` があれば検証して返す（自動検出より優先）。
    無ければ自動検出し、見つからなければ NetworkError。
    """
    if preferred is not None and str(preferred).strip():
        return validate_lan_host(preferred)
    found = list(interfaces) if interfaces is not None else list_lan_interfaces()
    if not found:
        raise NetworkError(NO_LAN_MESSAGE)
    return found[0].ip


def build_lan_info(host: str, port: int) -> LanInfo:
    """検証済みのプライベート IPv4 と port から LanInfo を作る（PIN は含めない）。"""
    validated = validate_lan_host(host)
    if not isinstance(port, int) or isinstance(port, bool):
        raise NetworkError("ポート番号が不正です")
    if not (1024 <= port <= 65535):
        raise NetworkError("ポート番号は 1024〜65535 の範囲で指定してください")
    return LanInfo(url=f"http://{validated}:{port}", host=validated, port=port)


# ---------------------------------------------------------------- 画面表示


def describe_interfaces(interfaces: Sequence[LanInterface]) -> str:
    """候補が複数あるときの日本語案内（P5契約 §2）。"""
    if len(interfaces) <= 1:
        return ""
    lines = ["接続できそうなアドレスが複数見つかりました:"]
    for i, iface in enumerate(interfaces):
        label = f"{iface.ip}" + (f"（{iface.name}）" if iface.name else "")
        mark = " ← 今回使用" if i == 0 else ""
        lines.append(f"    ・{label}{mark}")
    lines.append(
        "  うまく繋がらない場合は、別のアドレスを指定して起動し直してください:"
    )
    lines.append(f"    ./scripts/start.sh --lan --lan-host {interfaces[1].ip}")
    return "\n".join(lines)


def format_lan_banner(
    lan_info: LanInfo,
    pin: str,
    *,
    username: str = LAN_USERNAME,
    qr_ascii: str | None = None,
    qr_png: Path | None = None,
    interfaces: Sequence[LanInterface] = (),
) -> str:
    """LANモード起動時に Mac のターミナルへ大きく表示する案内（P5契約 §7.2）。

    **この文字列だけが PIN を含む。** ログ・UI・QR・履歴へは渡さないこと。
    """
    bar = "=" * 64
    lines = [
        "",
        bar,
        "  iPhone接続モード（同じWi-Fi内だけ）で起動しました",
        bar,
        "",
        "  ▼ iPhone の Safari で次のURLを開いてください",
        f"      {lan_info.url}",
        "",
    ]
    if qr_ascii:
        lines.append("  ▼ QRコード（iPhoneのカメラで読み取れます）")
        lines.append("")
        lines.append(qr_ascii)
        lines.append("")
    if qr_png is not None:
        lines.append(f"  QRコード画像: {qr_png}")
        lines.append("  （小さくて読み取れない場合はこの画像をプレビューで開いてください）")
        lines.append("")
    lines += [
        "  ▼ ログイン情報（iPhone の画面で入力します）",
        f"      ユーザー名: {username}",
        f"      パスワード（PIN）: {pin}",
        "      ※ この PIN はアプリを起動するたびに新しくなります"
        "（このターミナルの表示だけに出ます）",
        "",
        "  ▼ 大切なこと",
        "      ・同じWi-Fi・同じルーターの中だけで使えます。インターネットには公開していません",
        "      ・PIN は他の人に教えないでください",
        "      ・信頼できないWi-Fi（カフェ・公共Wi-Fiなど）では使わないでください",
        "      ・初回だけ macOS が「ネットワーク接続を許可しますか？」と聞くことがあります。"
        "その場合は［許可］を選んでください",
        "      ・ゲスト用Wi-Fiや、端末どうしの通信が禁止されたWi-Fiでは繋がりません",
        "",
        "  ▼ 終了するには",
        "      このターミナルで Control + C を押してください",
        "",
        bar,
        "",
    ]
    note = describe_interfaces(interfaces)
    if note:
        lines.insert(len(lines) - 2, "  " + note)
    return "\n".join(lines)
