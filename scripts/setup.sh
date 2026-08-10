#!/bin/bash
# ATELIER H3 Local Studio 初回セットアップ（設計書 §8.1）
# アプリ専用 venv の作成・依存導入・モック素材生成のみを行う。
# 既存の DiffSynth-Studio（本体・モデル・.venv）には一切触れない。
#
# 使い方:
#   ./scripts/setup.sh                  通常のセットアップ
#   ./scripts/setup.sh --with-upscale   1080p高品質化のモデル（約2.4MB）も取得する
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_UPSCALE=0
for arg in "$@"; do
    case "$arg" in
        --with-upscale) WITH_UPSCALE=1 ;;
        -h|--help)
            sed -n '6,8p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "エラー: 不明な引数です: ${arg}（--with-upscale / --help が使えます）"
            exit 1
            ;;
    esac
done

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [ -z "$UV_BIN" ] && [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
if [ -z "$UV_BIN" ] || [ ! -x "$UV_BIN" ]; then
    echo "エラー: uv が見つかりません。https://docs.astral.sh/uv/ の手順で導入してください"
    exit 1
fi

echo "==> アプリ専用 venv を作成し、依存を導入します（DiffSynth-Studio には触れません）"
"$UV_BIN" sync

# 個人設定ファイルの用意。config/config.toml は各自の絶対パス（DiffSynth-Studio の場所など）を
# 含むため Git 管理から外してある。無ければ公開用のひな形から作る。既存があれば絶対に上書きしない。
if [ ! -f config/config.toml ]; then
    if [ -f config/config.example.toml ]; then
        cp config/config.example.toml config/config.toml
        echo "==> config/config.toml を作成しました（config.example.toml から複製）"
        echo "    [backends.minimax_h3] の worker_python と working_directory を"
        echo "    お使いの DiffSynth-Studio の場所に書き換えてください。"
    else
        echo "エラー: config/config.toml も config/config.example.toml も見つかりません"
        exit 1
    fi
fi

echo "==> モック素材を生成します（imageio-ffmpeg 同梱バイナリ使用）"
.venv/bin/python -m app.core.mock_assets

# 1080p高品質化のモデル（P6・設計書 §26）。
# **明示的に --with-upscale を付けたときだけ**取得する（勝手にネットへ出ない）。
# 生成そのものには不要で、この機能を使わないなら無くても起動できる。
UPSCALE_DIR="app/assets/upscale"
UPSCALE_FILE="$UPSCALE_DIR/realesr-animevideov3.pth"
UPSCALE_URL="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth"
UPSCALE_SHA256="b8a8376811077954d82ca3fcf476f1ac3da3e8a68a4f4d71363008000a18b75d"

if [ "$WITH_UPSCALE" = "1" ]; then
    if [ -f "$UPSCALE_FILE" ] \
       && [ "$(shasum -a 256 "$UPSCALE_FILE" | cut -d' ' -f1)" = "$UPSCALE_SHA256" ]; then
        echo "==> 1080p高品質化のモデルは取得済みです（SHA-256一致）"
    else
        echo "==> 1080p高品質化のモデルを取得します（約2.4MB・1回だけ）"
        mkdir -p "$UPSCALE_DIR"
        tmp="$UPSCALE_FILE.partial"
        rm -f "$tmp"
        if ! curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp" "$UPSCALE_URL"; then
            rm -f "$tmp"
            echo "エラー: モデルを取得できませんでした（${UPSCALE_URL}）"
            echo "        ネットワークをご確認のうえ、もう一度お試しください。"
            echo "        取得しなくてもアプリは起動します（高品質化だけが使えません）。"
            exit 1
        fi
        actual="$(shasum -a 256 "$tmp" | cut -d' ' -f1)"
        if [ "$actual" != "$UPSCALE_SHA256" ]; then
            rm -f "$tmp"
            echo "エラー: 取得したモデルの SHA-256 が一致しません"
            echo "        期待: $UPSCALE_SHA256"
            echo "        実際: $actual"
            exit 1
        fi
        # 検証を通ってから正式名にする（半端なファイルを残さない）
        mv "$tmp" "$UPSCALE_FILE"
        echo "==> 1080p高品質化のモデルを配置しました（${UPSCALE_FILE}）"
    fi
elif [ ! -f "$UPSCALE_FILE" ]; then
    echo "==> ヒント: 1080p高品質化を使うには ./scripts/setup.sh --with-upscale を実行してください"
fi

echo "==> セットアップ完了。./scripts/start.sh で起動できます"
