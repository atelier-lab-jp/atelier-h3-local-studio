#!/bin/bash
# ATELIER H3 Local Studio 初回セットアップ（設計書 §8.1）
# アプリ専用 venv の作成・依存導入・モック素材生成のみを行う。
# 既存の DiffSynth-Studio（本体・モデル・.venv）には一切触れない。
set -euo pipefail
cd "$(dirname "$0")/.."

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

echo "==> セットアップ完了。./scripts/start.sh で起動できます"
