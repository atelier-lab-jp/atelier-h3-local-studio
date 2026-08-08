#!/bin/bash
# ATELIER H3 Local Studio 起動スクリプト（設計書 §8.1）
#
# 引数はすべてそのまま app.main へ渡す:
#   ./scripts/start.sh                       通常モード（127.0.0.1 のみ・認証なし）
#   ./scripts/start.sh --lan                 iPhone接続モード（同じWi-Fi内・PIN認証）
#   ./scripts/start.sh --lan --lan-host 192.168.1.23
#   ./scripts/start.sh --check [--mode real] [--deep-check]
#   ./scripts/start.sh --smoke
set -euo pipefail

# 自身の位置からプロジェクトルートを解決する（呼び出し元の cd に依存しない）
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd -- "$PROJECT_ROOT"

if [ ! -x .venv/bin/python ]; then
    echo "エラー: アプリ環境が未作成です。先に ./scripts/setup.sh を実行してください"
    exit 1
fi

# Gradio の外部送信を無効化（設計書 §15）
export GRADIO_ANALYTICS_ENABLED=False

# 生成中に Mac がスリープしないようにする（caffeinate -i）。
# --check / --smoke は数秒で終わる短命実行なので対象外。
# ATELIER_CAFFEINATED=1 が既に立っている（.command 側で包んだ）場合も二重にしない。
NEED_CAFFEINATE=1
for arg in "$@"; do
    case "$arg" in
        --check|--smoke) NEED_CAFFEINATE=0 ;;
    esac
done
if [ "${ATELIER_CAFFEINATED:-0}" = "1" ] || [ "${ATELIER_NO_CAFFEINATE:-0}" = "1" ]; then
    NEED_CAFFEINATE=0
fi

if [ "$NEED_CAFFEINATE" = "1" ] && command -v caffeinate >/dev/null 2>&1; then
    export ATELIER_CAFFEINATED=1
    # caffeinate はコマンドを子プロセスとして実行し、終了と同時に自分も終わる
    exec caffeinate -i .venv/bin/python -m app.main "$@"
fi

exec .venv/bin/python -m app.main "$@"
