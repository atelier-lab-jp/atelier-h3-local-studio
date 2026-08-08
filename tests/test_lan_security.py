"""LANモードのセキュリティ検証（P5契約 §7.3）。

実際に Gradio を `auth=` 付きでヘッドレス起動し、**実 HTTP** で確認する:

- 未認証では動画ファイル・`/config`・`/gradio_api/info`・`/run/...`・`/queue/join` が 401
- 認証後は動画が Range 206 で配信される
- 履歴JSON・ログ・データ領域外は**認証後でも 403**（`allowed_paths` の外）
- 誤PINは拒否、連続失敗でバックオフ、正PINで成功
- QR のデータに PIN が入らない／ログ全文に PIN が現れない
- 通常モードは 127.0.0.1 だけに bind し、LAN の IP では待ち受けない
- コードベースのどこにも `share=True` が無い

**実モデルは絶対に起動しない**（UI は最小構成の `gr.Blocks`）。
書き込み先は `tmp_path` のみ（プロジェクトの `data/` には触れない）。
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.core.lanauth import LAN_USERNAME, PinAuthenticator, generate_pin
from app.core.network import (
    build_lan_info,
    format_lan_banner,
    list_lan_interfaces,
)
from app.core.qrgen import cleanup_qr, render_qr

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 15


# ---------------------------------------------------------------- HTTP ヘルパ


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fetch(base: str, path: str, *, opener=None, headers=None, data=None):
    """(status, headers, body) を返す。HTTPError もステータスとして扱う。"""
    request = urllib.request.Request(base + path, data=data, headers=headers or {})
    op = opener or urllib.request.build_opener()
    try:
        with op.open(request, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _login(base: str, pin: str, username: str = LAN_USERNAME):
    """ログインして (status, opener, cookiejar) を返す。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode({"username": username, "password": pin}).encode()
    status, _headers, _payload = _fetch(
        base,
        "/login",
        opener=opener,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    return status, opener, jar


def _build_demo():
    """最小構成の UI（実モデルもアプリ本体の UI も使わない）。"""
    import gradio as gr

    with gr.Blocks() as demo:
        text_in = gr.Textbox(label="入力")
        text_out = gr.Textbox(label="出力")
        gr.Button("実行").click(
            lambda x: f"echo:{x}", inputs=text_in, outputs=text_out, api_name="ping"
        )
    return demo


# ---------------------------------------------------------------- 認証つきサーバ


@pytest.fixture(scope="module")
def lan_server(tmp_path_factory):
    """PIN 認証つきで起動した Gradio サーバ（LANモード相当の launch 引数）。"""
    data_root = tmp_path_factory.mktemp("lan_data")
    outputs = data_root / "outputs"
    concat = data_root / "concat"
    logs = data_root / "logs"
    for d in (outputs, concat, logs):
        d.mkdir(parents=True, exist_ok=True)

    video = outputs / "v_20260808_120000_abcd.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(8192))
    history = data_root / "history.json"
    history.write_text('{"version": "1.2", "records": []}', encoding="utf-8")
    app_log = logs / "app.log"
    app_log.write_text("起動しました\n", encoding="utf-8")
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("秘密", encoding="utf-8")

    pin = generate_pin(6)
    # 端末側の再試行で誤ロックしないよう、この検証用サーバは失敗上限を高くしておく
    # （バックオフ自体は lockout_server で別途検証する）
    auth = PinAuthenticator(pin, max_failures=1000, lockout_sec=30.0)

    demo = _build_demo()
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",          # 検証中に LAN へ晒さない（本番はここが private IPv4）
        server_port=port,
        share=False,
        inbrowser=False,
        auth=auth.as_gradio_auth(),
        allowed_paths=[str(outputs), str(concat)],
        prevent_thread_lock=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        yield {
            "base": base,
            "pin": pin,
            "auth": auth,
            "data_root": data_root,
            "video": video,
            "history": history,
            "app_log": app_log,
            "outside": outside,
        }
    finally:
        demo.close()


@pytest.fixture(scope="module")
def authed(lan_server):
    status, opener, jar = _login(lan_server["base"], lan_server["pin"])
    assert status == 200, "正しい PIN でログインできること"
    assert any(c.name.startswith("access-token") for c in jar)
    return opener


# ---------------------------------------------------------------- 未認証は 401


def test_unauthenticated_video_file_is_401(lan_server):
    status, _h, _b = _fetch(
        lan_server["base"], f"/gradio_api/file={lan_server['video']}"
    )
    assert status == 401


def test_unauthenticated_config_and_info_are_401(lan_server):
    for path in ("/config", "/gradio_api/info", "/gradio_api/openapi.json"):
        status, _h, _b = _fetch(lan_server["base"], path)
        assert status == 401, f"{path} が未認証で通っています"


def test_unauthenticated_run_and_queue_are_401(lan_server):
    payload = json.dumps({"data": ["x"], "fn_index": 0}).encode()
    status, _h, _b = _fetch(
        lan_server["base"],
        "/gradio_api/run/ping",
        headers={"Content-Type": "application/json"},
        data=payload,
    )
    assert status == 401

    join = json.dumps({"data": ["x"], "fn_index": 0, "session_hash": "abc"}).encode()
    status, _h, _b = _fetch(
        lan_server["base"],
        "/gradio_api/queue/join",
        headers={"Content-Type": "application/json"},
        data=join,
    )
    assert status == 401


def test_unauthenticated_body_never_contains_the_pin(lan_server):
    for path in ("/", "/config", f"/gradio_api/file={lan_server['video']}"):
        _status, _h, body = _fetch(lan_server["base"], path)
        assert lan_server["pin"].encode() not in body


# ---------------------------------------------------------------- 認証後


def test_authenticated_video_supports_range_206(lan_server, authed):
    url = f"/gradio_api/file={lan_server['video']}"
    status, headers, body = _fetch(
        lan_server["base"], url, opener=authed, headers={"Range": "bytes=0-99"}
    )
    assert status == 206
    assert headers.get("Content-Range", "").startswith("bytes 0-99/")
    assert len(body) == 100
    assert body == lan_server["video"].read_bytes()[:100]


def test_authenticated_video_full_request_is_200(lan_server, authed):
    status, headers, body = _fetch(
        lan_server["base"], f"/gradio_api/file={lan_server['video']}", opener=authed
    )
    assert status == 200
    assert body == lan_server["video"].read_bytes()
    assert "video/mp4" in headers.get("Content-Type", "")


def test_authenticated_api_endpoints_are_reachable(lan_server, authed):
    """401 が「壊れているから」ではなく「認証が効いているから」であることの対照。"""
    for path in ("/config", "/gradio_api/info"):
        status, _h, _b = _fetch(lan_server["base"], path, opener=authed)
        assert status == 200, f"{path} が認証後も通りません"

    payload = json.dumps({"data": ["こんにちは"], "fn_index": 0}).encode()
    status, _h, body = _fetch(
        lan_server["base"],
        "/gradio_api/run/ping",
        opener=authed,
        headers={"Content-Type": "application/json"},
        data=payload,
    )
    assert status == 200
    assert "echo:こんにちは" in body.decode("utf-8")


# ---------------------------------------------------------------- 配信対象外は 403


def test_history_json_and_logs_are_forbidden_even_after_login(lan_server, authed):
    for target in (
        lan_server["history"],
        lan_server["app_log"],
        lan_server["data_root"],
        lan_server["outside"],
        Path("/etc/hosts"),
    ):
        status, _h, _b = _fetch(
            lan_server["base"], f"/gradio_api/file={target}", opener=authed
        )
        assert status != 200, f"{target} が配信されています"
        assert status in (403, 404), f"{target} の応答が想定外です: {status}"


def test_path_traversal_is_rejected(lan_server, authed):
    outputs = lan_server["data_root"] / "outputs"
    for suffix in ("../history.json", "../logs/app.log", "../../etc/hosts"):
        status, _h, _b = _fetch(
            lan_server["base"], f"/gradio_api/file={outputs / suffix}", opener=authed
        )
        assert status != 200, f"{suffix} が配信されています"


# ---------------------------------------------------------------- PIN の受付


def test_wrong_pin_is_rejected_over_http(lan_server):
    status, _opener, jar = _login(lan_server["base"], "000000")
    assert status == 400
    assert not any(c.name.startswith("access-token") for c in jar)


def test_empty_and_odd_credentials_are_rejected(lan_server):
    for candidate in ("", "   ", "h3", "0", lan_server["pin"] + "0"):
        status, _opener, jar = _login(lan_server["base"], candidate)
        # 400（PIN 不一致）または 422（そもそも受け付けない形式）
        assert status in (400, 401, 422), f"{candidate!r} が受理されました"
        assert not any(c.name.startswith("access-token") for c in jar)


def test_username_is_not_the_secret(lan_server):
    """ユーザー名は何でもよい（秘密は PIN だけ）ことを実 HTTP で確認する。"""
    status, _opener, jar = _login(lan_server["base"], lan_server["pin"], username="dare")
    assert status == 200
    assert any(c.name.startswith("access-token") for c in jar)


def test_backoff_locks_out_repeated_failures_over_http(tmp_path):
    """連続失敗でバックオフし、時間経過で復帰する（専用サーバで検証）。"""
    pin = generate_pin(6)
    auth = PinAuthenticator(pin, max_failures=3, lockout_sec=1.0)
    demo = _build_demo()
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        auth=auth.as_gradio_auth(),
        allowed_paths=[str(tmp_path)],
        prevent_thread_lock=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(3):
            assert _login(base, "000000")[0] == 400
        assert auth.is_locked() is True

        # ロック中は正しい PIN でも拒否される
        assert _login(base, pin)[0] == 400

        time.sleep(1.2)
        assert auth.locked_for() == 0.0
        status, _opener, jar = _login(base, pin)
        assert status == 200
        assert any(c.name.startswith("access-token") for c in jar)
    finally:
        demo.close()


# ---------------------------------------------------------------- PIN を漏らさない


def test_qr_payload_contains_only_the_url(tmp_path):
    info = build_lan_info("192.168.1.23", 7860)
    pin = generate_pin(6)
    artifact = render_qr(info.url, tmp_path)
    try:
        assert artifact.url == info.url
        assert pin not in artifact.url
        assert pin not in artifact.ascii_art
        assert pin.encode() not in artifact.png_path.read_bytes()
        assert (artifact.png_path.stat().st_mode & 0o777) == 0o600
    finally:
        cleanup_qr(artifact)
    assert not artifact.png_path.exists()


def test_lan_info_never_carries_the_pin():
    info = build_lan_info("192.168.1.23", 7860)
    pin = "246813"
    assert pin not in repr(info)
    assert pin not in str(info)
    assert "pin" not in repr(info).lower()


def test_pin_never_appears_in_the_log_file(tmp_path):
    """LANモード起動と同じ手順を踏んでも、ログ全文に PIN が出ない。"""
    log_path = tmp_path / "app.log"
    logger = logging.getLogger("atelier.test_lan_security")
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    try:
        interfaces = list_lan_interfaces(
            ifconfig_reader=lambda: (
                "en0: flags=8863<UP> mtu 1500\n\tinet 192.168.1.23 netmask 0xffffff00\n"
            )
        )
        info = build_lan_info(interfaces[0].ip, 7860)
        pin = generate_pin(6)
        auth = PinAuthenticator(pin)
        artifact = render_qr(info.url, tmp_path)
        banner = format_lan_banner(
            info, pin, qr_ascii=artifact.ascii_art, qr_png=artifact.png_path,
            interfaces=interfaces,
        )

        # main.py 相当のログ出力（PIN は決して渡さない）
        logger.info("iPhone接続モードで起動します: %s", info.url)
        logger.info("認証器: %s", auth)
        logger.info("認証器(repr): %r", auth)
        logger.info("QRコード: %s", artifact.png_path)
        logger.info("候補: %s", interfaces)
        logger.info("LanInfo: %r", info)
        auth.check(LAN_USERNAME, "000000")
        auth.check(LAN_USERNAME, pin)
        logger.info("失敗回数: %d / ロック: %s", auth.failure_count, auth.is_locked())
        handler.flush()

        text = log_path.read_text(encoding="utf-8")
        assert pin not in text
        assert info.url in text          # 何も記録していないだけ、ではないことの確認
        assert "****" in text            # 認証器は伏字で記録される
        assert pin in banner             # PIN はターミナル表示にだけ出す
        cleanup_qr(artifact)
    finally:
        logger.removeHandler(handler)
        handler.close()


# ---------------------------------------------------------------- bind 範囲


def test_localhost_server_is_not_reachable_from_lan_ip(tmp_path):
    """通常モードは 127.0.0.1 のみ。LAN の IP では待ち受けない。"""
    interfaces = list_lan_interfaces()
    if not interfaces:
        pytest.skip("この環境にはプライベート IPv4 がありません")
    lan_ip = interfaces[0].ip

    demo = _build_demo()
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        allowed_paths=[str(tmp_path)],
        prevent_thread_lock=True,
    )
    try:
        status, _h, _b = _fetch(f"http://127.0.0.1:{port}", "/")
        assert status == 200  # ループバックからは見える

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3.0)
            with pytest.raises((ConnectionRefusedError, OSError)):
                sock.connect((lan_ip, port))
    finally:
        demo.close()


def test_lan_bind_target_is_never_a_wildcard_address():
    """LANモードでも 0.0.0.0 へは bind しない（検出結果は常に private IPv4）。"""
    from app.core.network import detect_lan_ipv4, is_private_ipv4

    for iface in list_lan_interfaces():
        assert iface.ip != "0.0.0.0"
        assert is_private_ipv4(iface.ip)
    if list_lan_interfaces():
        assert detect_lan_ipv4() != "0.0.0.0"


# ---------------------------------------------------------------- 静的チェック


def _source_files() -> list[Path]:
    files: list[Path] = []
    for directory in ("app", "scripts", "config"):
        root = PROJECT_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in (".png", ".mp4", ".pyc"):
                continue
            files.append(path)
    files.extend(p for p in PROJECT_ROOT.glob("*.py"))
    return files


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _code_text(path: Path) -> str:
    """コメント・文字列リテラル・説明文を除いた「実際に動くコード」だけを返す。

    「shell=True は使わない」のような**注意書き**を検出してしまわないようにする。
    """
    if path.suffix == ".py":
        import io
        import tokenize

        try:
            pieces: list[str] = []
            for token in tokenize.tokenize(io.BytesIO(_read(path).encode()).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                pieces.append(token.string)
            return " ".join(pieces)
        except (tokenize.TokenError, SyntaxError, IndentationError):
            return _read(path)
    # シェルスクリプト等: 行頭・行中の # 以降を落とす
    lines = []
    for line in _read(path).splitlines():
        head = line.split("#", 1)[0]
        if head.strip():
            lines.append(head)
    return "\n".join(lines)


def test_no_share_true_anywhere_in_the_codebase():
    """外部トンネル（`share=True`）がコード上に存在しないこと。"""
    # grep 自身が引っかからないよう、探す文字列は実行時に組み立てる
    pattern = re.compile(r"share\s*=\s*" + "True")
    offenders = [str(p) for p in _source_files() if pattern.search(_code_text(p))]
    assert offenders == [], f"share=True が見つかりました: {offenders}"


def test_launch_uses_share_false():
    text = _read(PROJECT_ROOT / "app" / "main.py")
    assert re.search(r"[\"']?share[\"']?\s*[:=]\s*" + "False", text)


def test_no_wildcard_bind_in_application_code():
    """`0.0.0.0`（全インタフェース待受）を値として使っていないこと。"""
    pattern = re.compile(r"""["']0\.0\.0\.0["']""")
    offenders = [str(p) for p in _source_files() if pattern.search(_read(p))]
    assert offenders == [], f"0.0.0.0 への bind が見つかりました: {offenders}"


def test_no_shell_true_in_application_code():
    pattern = re.compile(r"shell\s*=\s*" + "True")
    offenders = [str(p) for p in _source_files() if pattern.search(_code_text(p))]
    assert offenders == [], f"shell=True が見つかりました: {offenders}"


def test_no_sudo_or_firewall_mutation_in_scripts():
    banned = ("sudo ", "pfctl", "socketfilterfw", "upnp", "ngrok")
    offenders = []
    for path in (PROJECT_ROOT / "scripts").iterdir():
        if not path.is_file():
            continue
        text = _read(path).lower()
        for word in banned:
            if word in text:
                offenders.append(f"{path.name}: {word}")
    assert offenders == [], offenders


def test_launch_scripts_are_executable():
    for name in (
        "start.sh",
        "setup.sh",
        "ATELIER H3 Studio.command",
        "ATELIER H3 Studio LAN.command",
    ):
        path = PROJECT_ROOT / "scripts" / name
        assert path.is_file(), f"{name} がありません"
        assert os.access(path, os.X_OK), f"{name} に実行権限がありません"


def _fake_project(tmp_path: Path, command_name: str, *, check_ok: bool = True) -> Path:
    """`.command` だけを本物にした模擬プロジェクト（空白・日本語を含むパス）。

    `start.sh` は呼び出しを記録するスタブに差し替えるので、実モデルは絶対に動かない。
    """
    import shutil

    root = tmp_path / "アプリ フォルダ"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    python = root / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)

    shutil.copy(PROJECT_ROOT / "scripts" / command_name, root / "scripts" / command_name)
    (root / "scripts" / command_name).chmod(0o755)

    stub = root / "scripts" / "start.sh"
    stub.write_text(
        "#!/bin/bash\n"
        'printf "ARGS:%s\\n" "$*" >> "$(dirname "$0")/../calls.txt"\n'
        'if [ "${1:-}" = "--check" ]; then\n'
        f"    exit {0 if check_ok else 1}\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return root


def _run_command_script(root: Path, command_name: str):
    import subprocess

    return subprocess.run(
        [str(root / "scripts" / command_name)],
        cwd=str(Path.home()),          # Finder からの起動と同じく現在地に依存しない
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS 専用の起動スクリプト")
@pytest.mark.parametrize(
    ("command_name", "expected_args", "expected_check_args"),
    [
        ("ATELIER H3 Studio.command", "", "--check"),
        # LAN 用は起動前チェックも --lan 付きで走らせる。そうしないと
        # 「アドレスが見つからない」等のLAN固有の失敗を事前に検出できず、
        # ユーザーは合格表示のあとで別の失敗を見せられる（相互レビュー M-1）。
        ("ATELIER H3 Studio LAN.command", "--lan", "--check --lan"),
    ],
)
def test_command_script_runs_preflight_then_launches(
    tmp_path: Path, command_name: str, expected_args: str, expected_check_args: str
):
    root = _fake_project(tmp_path, command_name)
    proc = _run_command_script(root, command_name)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = (root / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"ARGS:{expected_check_args}"  # 先に起動前チェック
    assert calls[-1] == f"ARGS:{expected_args}"       # そのあと本起動
    assert "起動前チェック: 合格" in proc.stdout


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS 専用の起動スクリプト")
@pytest.mark.parametrize(
    "command_name", ["ATELIER H3 Studio.command", "ATELIER H3 Studio LAN.command"]
)
def test_command_script_reports_missing_venv_in_japanese(
    tmp_path: Path, command_name: str
):
    root = _fake_project(tmp_path, command_name)
    (root / ".venv" / "bin" / "python").unlink()
    proc = _run_command_script(root, command_name)

    assert proc.returncode == 1
    assert "起動できませんでした" in proc.stdout
    assert "setup.sh" in proc.stdout
    assert not (root / "calls.txt").exists()   # 起動処理には進まない


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS 専用の起動スクリプト")
@pytest.mark.parametrize(
    ("command_name", "expected_check_args"),
    [
        ("ATELIER H3 Studio.command", "--check"),
        ("ATELIER H3 Studio LAN.command", "--check --lan"),
    ],
)
def test_command_script_stops_when_preflight_fails(
    tmp_path: Path, command_name: str, expected_check_args: str
):
    root = _fake_project(tmp_path, command_name, check_ok=False)
    proc = _run_command_script(root, command_name)

    assert proc.returncode == 1
    assert "起動前チェックに合格しませんでした" in proc.stdout
    calls = (root / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert calls == [f"ARGS:{expected_check_args}"]   # 本起動はしない


def test_lan_command_passes_lan_flag_and_normal_command_does_not():
    lan = _read(PROJECT_ROOT / "scripts" / "ATELIER H3 Studio LAN.command")
    normal = _read(PROJECT_ROOT / "scripts" / "ATELIER H3 Studio.command")
    assert "--lan" in lan
    assert "--lan" not in normal
    for text in (lan, normal):
        assert "caffeinate -i" in text          # 生成中のスリープ防止
        assert "eval" not in text               # shell injection の温床を作らない
        assert "${(%):-%x}" in text             # 自身の位置からルート解決
        assert "Control + C" in text            # 終了方法の案内
