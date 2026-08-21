"""ATELIER H3 Local Studio エントリポイント（P1）。

起動手順: 設定読込 → ログ初期化 → 起動前チェック（preflight）→
統合サービス構築（履歴読込・中断ジョブ確定・ディスパッチャ開始）→ Gradio UI 起動。
Gradio Analytics は import 前に環境変数で無効化する（設計書 §15）。
"""

from __future__ import annotations

import os

# setdefault だと外から True を注入できてしまう。Analytics 無効は両モードで固定のため
# 強制代入にする（設計書 §15）。
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATELIER H3 Local Studio")
    parser.add_argument(
        "--check", action="store_true", help="起動前チェックだけを実行して終了する"
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "real"),
        default=None,
        help="動作モードを上書きする（省略時は config.toml / ATELIER_MOCK に従う）",
    )
    parser.add_argument(
        "--deep-check",
        action="store_true",
        help="実機モードで既存Python環境のパッケージ検査まで行う（数十秒かかる）",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="UIをヘッドレス起動してHTTP応答を確認し、すぐ終了する（開発用）",
    )
    parser.add_argument(
        "--lan",
        action="store_true",
        help="同じWi-Fi内のiPhoneから使えるようにする（PIN認証つき）",
    )
    parser.add_argument(
        "--lan-host",
        default=None,
        metavar="IPアドレス",
        help="iPhone接続モードで使うプライベートIPv4を手動指定する（例: 192.168.1.23）",
    )
    args = parser.parse_args(argv)

    # iPhone接続モードは「明示的に起動したときだけ」有効。設定ファイルや環境変数では
    # 有効にできない（設計書 §15.1・決定D13）。--smoke は認証つき起動の疎通を
    # 正しく判定できない（未認証でもログイン画面が 200 を返す）ため組み合わせを拒否する。
    if args.lan and args.smoke:
        print("エラー: --lan と --smoke は同時に指定できません")
        return 1
    if args.lan_host is not None and not args.lan:
        print("エラー: --lan-host は --lan と一緒に指定してください")
        return 1

    from app.core.config import ConfigError, load_config

    try:
        cfg = load_config(PROJECT_ROOT)
    except ConfigError as e:
        print(f"設定エラー: {e}")
        return 1

    mode = args.mode or cfg.engine_mode
    if args.smoke and mode == "real":
        # スモークテストは UI の疎通確認が目的。実機モードだと数分のモデル初期化が走り
        # HTTP チェックがタイムアウトするため、モックへ切り替える。
        print("スモークテストはモックモードで実行します（実機モデルは読み込みません）")
        mode = "mock"

    from app.core.applog import setup_logging

    try:
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"エラー: ログディレクトリを作成できません: {e}")
        return 1
    log = setup_logging(
        cfg.logs_dir, cfg.log_level, cfg.log_max_bytes, cfg.log_backup_count
    )
    log.info("%s v%s 起動処理を開始（%sモード）", cfg.name, cfg.version, mode)

    # iPhone接続モードの接続先を、起動前チェックより先に決める（チェックが解決済み
    # ホストを必要とするため）。private IPv4 が見つからなければ 0.0.0.0 へフォールバック
    # せず、日本語で案内して中止する（設計書 §15.1.1）。
    from app.core.network import (
        LanInfo,
        NetworkError,
        build_lan_info,
        detect_lan_ipv4,
        format_lan_banner,
        list_lan_interfaces,
    )

    host = cfg.host  # 通常モードは 127.0.0.1 のまま（config で強制検証済み）
    lan_info: LanInfo | None = None
    lan_interfaces: list = []
    if args.lan:
        try:
            lan_interfaces = list_lan_interfaces()
            host = detect_lan_ipv4(
                args.lan_host or cfg.lan_host_override or None,
                interfaces=lan_interfaces,
            )
            lan_info = build_lan_info(host, cfg.port)
        except NetworkError as e:
            print(f"iPhone接続モードを開始できません: {e}")
            return 1

    from app.core import preflight

    result = preflight.run_preflight(
        cfg,
        mode,
        deep_worker_check=(mode == "real" and args.deep_check),
        lan=args.lan,
        # 起動前チェックが受け取るのは「ユーザーが手で指定した値」であって解決後の
        # アドレスではない（解決済みを渡すと常に「手動指定」と表示されてしまう）
        lan_host=(args.lan_host or "") if args.lan else "",
    )
    print(preflight.format_report(result, mode))

    if args.check:
        return 0 if result.ok else 1
    if not result.ok:
        log.error("起動前チェック不合格のため起動を中止")
        return 1
    if mode == "real":
        print(
            "実機モードで起動します。モデルの初期化には数分かかります"
            "（進捗はブラウザ画面と下のログに表示されます）。"
        )
    else:
        print(
            "モックモードで起動します（実際の AI モデルは使わず、"
            "数秒で完成するダミー動画が出ます）。"
        )

    from app.core.app_service import AppService

    service = AppService.build(cfg, mode)
    qr = None
    try:
        # start() 以降で失敗しても finally で確実に停止させる
        for w in service.start():
            print(f"  [警告]   {w}")

        from app.ui.minimal import build_ui

        demo = build_ui(cfg, mode, service, lan_info=lan_info)
        url = f"http://{host}:{cfg.port}"

        launch_kwargs: dict = {
            "server_name": host,              # 通常は 127.0.0.1。LANモードだけ private IPv4
            "server_port": cfg.port,
            "share": False,                   # 外部公開しない（両モードで固定。設計書 §15）
            # 配信は成果物ディレクトリのみに限定する（ログや履歴JSONは配信しない）。
            # 整理済みの動画を置くディレクトリは**絶対に加えない**（§26.11）。
            # data/tmp も加えない（QR画像がHTTPで見えてしまう）。継続サムネイルは
            # UI がサーバ側で値として渡す個別ファイル（_servable(allow_tmp=True)）で、
            # ここを広げなくても表示できる。
            "allowed_paths": [
                str(cfg.outputs_dir),
                str(cfg.concat_dir),
                str(cfg.upscaled_dir),  # P6: 1080p高品質版
            ],
            "inbrowser": cfg.auto_open_browser and not args.smoke and not args.lan,
            # 開始画像（P8）で受け取るファイルの上限。下位層は 32MB までしか
            # 受け付けないので、それより少しだけ大きい値でサーバ側でも先に断る。
            "max_file_size": "40mb",
        }
        if args.smoke:
            launch_kwargs["prevent_thread_lock"] = True

        if lan_info is not None:
            # PIN はここでだけ生成し、ターミナルへ表示する以外どこにも出さない。
            # ログ・履歴・設定・URL・QR・プロセス引数のいずれにも書かない（設計書 §15.1.2）。
            from app.core.lanauth import LAN_USERNAME, PinAuthenticator, generate_pin
            from app.core.qrgen import QrError, render_qr

            pin = generate_pin(cfg.lan_pin_digits)
            authenticator = PinAuthenticator(
                pin,
                max_failures=cfg.lan_max_auth_failures,
                lockout_sec=cfg.lan_auth_lockout_sec,
            )
            launch_kwargs["auth"] = authenticator.as_gradio_auth()
            launch_kwargs["auth_message"] = (
                f"ユーザー名に「{LAN_USERNAME}」、パスワード欄に Mac の画面に表示されている"
                "PINを入力してください。"
            )
            try:
                qr = render_qr(lan_info.url, cfg.tmp_dir)
            except QrError as e:
                log.warning("QRコードを作成できませんでした: %s", e)
                print(f"  [警告]   QRコードを作成できませんでした（URLは手で入力できます）: {e}")
            # flush=True は必須。標準出力がターミナル以外（パイプ・リダイレクト）だと
            # 8KB たまるまで書き出されず、PIN が画面に出ないままログインできなくなる。
            print(
                format_lan_banner(
                    lan_info,
                    pin,
                    qr_ascii=qr.ascii_art if qr else None,
                    qr_png=qr.png_path if qr else None,
                    interfaces=lan_interfaces,
                ),
                flush=True,
            )
            log.info("iPhone接続モードで起動します: %s", lan_info.url)  # PINは記録しない
        else:
            log.info("UI を起動します: %s", url)
            print(f"ブラウザで {url} を開いてください（終了は Ctrl+C）")

        demo.launch(**launch_kwargs)

        if args.smoke:
            import urllib.request

            import gradio

            try:
                with urllib.request.urlopen(url + "/", timeout=15) as resp:
                    http_status = resp.status
                    body_head = resp.read(2048).decode("utf-8", "replace")
            except Exception as e:
                print(f"スモークテスト: 不合格（HTTP接続失敗: {e}）")
                demo.close()
                return 1
            ok = http_status == 200 and "<html" in body_head.lower()
            print(
                f"スモークテスト: HTTP {http_status} / gradio {gradio.__version__} / "
                + ("合格" if ok else "不合格")
            )
            demo.close()
            return 0 if ok else 1
        return 0
    except KeyboardInterrupt:
        print("\n終了します…")
        return 0
    finally:
        # 停止処理を先に済ませる（QR の後始末で何かあってもワーカーは必ず止める）
        service.shutdown()
        if qr is not None:
            # QR画像は接続用URLしか含まないが、役目を終えたら残さない（設計書 §15.1.4）
            try:
                from app.core.qrgen import cleanup_qr

                cleanup_qr(qr)
            except Exception:  # noqa: BLE001 - 後始末の失敗で終了を妨げない
                log.warning("QR画像の後始末に失敗しました", exc_info=True)
        log.info("停止処理が完了しました")


if __name__ == "__main__":
    sys.exit(main())
