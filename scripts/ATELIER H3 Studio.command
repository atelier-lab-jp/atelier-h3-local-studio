#!/bin/zsh
# ATELIER H3 Local Studio — 通常モード（この Mac だけで使う）
#
# Finder でこのファイルをダブルクリックすると、ターミナルが開いてアプリが起動します。
# 初回は macOS のセキュリティ（Gatekeeper）に止められることがあります。
# そのときは、このファイルを右クリック →［開く］→［開く］を選んでください。
#
# iPhone から使いたいときは「ATELIER H3 Studio LAN.command」のほうを開いてください。

emulate -L zsh
set -u

# 自身の置き場所からプロジェクトルートを解決する（開いたときの現在地に依存しない）
SCRIPT_DIR="$(cd -- "$(dirname -- "${(%):-%x}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
START_SH="$PROJECT_ROOT/scripts/start.sh"

line() { print -r -- "----------------------------------------------------------------"; }

fail() {
    print -r -- ""
    line
    print -r -- "  起動できませんでした"
    line
    print -r -- ""
    print -r -- "  $1"
    print -r -- ""
    if [ $# -ge 2 ]; then
        print -r -- "  $2"
        print -r -- ""
    fi
    print -n -- "  Enter キーを押すとこのウィンドウを閉じられます… "
    read -r _dummy 2>/dev/null
    exit 1
}

clear 2>/dev/null
line
print -r -- "  ATELIER H3 Local Studio を起動します（通常モード）"
line
print -r -- ""
print -r -- "  この Mac のブラウザだけで使えるモードです（外部には公開しません）。"
print -r -- "  終了するときは、このウィンドウで Control + C を押してください。"
print -r -- ""

if [ ! -d "$PROJECT_ROOT" ]; then
    fail "アプリのフォルダが見つかりません: $PROJECT_ROOT"
fi
if [ ! -f "$START_SH" ]; then
    fail "起動スクリプトが見つかりません: $START_SH" \
         "アプリのフォルダごと移動・コピーし直してから、もう一度お試しください。"
fi
if [ ! -x "$START_SH" ]; then
    chmod +x "$START_SH" 2>/dev/null
fi
if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    fail "アプリ専用の Python 環境（.venv）がまだ作られていません。" \
         "ターミナルで次を実行してください:  cd \"$PROJECT_ROOT\" && ./scripts/setup.sh"
fi

# 起動前チェック（preflight）。合格したときは結果を出しすぎないよう1行だけ表示する
print -r -- "  起動前チェックを実行しています…"
CHECK_REPORT="$("$START_SH" --check 2>&1)"
CHECK_STATUS=$?
if [ $CHECK_STATUS -ne 0 ]; then
    print -r -- ""
    print -r -- "$CHECK_REPORT"
    fail "起動前チェックに合格しませんでした（上の［エラー］の行をご覧ください）。" \
         "よくある原因: 別のウィンドウでアプリが起動したまま / モデルファイルの置き場所が違う。"
fi
print -r -- "  起動前チェック: 合格"
print -r -- ""
print -r -- "  ブラウザが自動で開きます。開かないときは http://127.0.0.1:7860 を開いてください。"
print -r -- ""

# caffeinate -i: 生成中に Mac がスリープしないようにする。
# コマンドを引数に渡しているので、アプリが終わると caffeinate も一緒に終わる。
export ATELIER_CAFFEINATED=1
exec caffeinate -i "$START_SH"
