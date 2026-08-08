#!/bin/zsh
# ATELIER H3 Local Studio — iPhone接続モード（同じWi-Fi内だけ）
#
# Finder でこのファイルをダブルクリックすると、ターミナルが開いてアプリが起動します。
# 起動すると、このウィンドウに「iPhone用のURL」「QRコード」「PIN」が表示されます。
# 初回は macOS のセキュリティ（Gatekeeper）に止められることがあります。
# そのときは、このファイルを右クリック →［開く］→［開く］を選んでください。
#
# 公開されるのは「同じWi-Fi・同じルーターの中」だけです。インターネットには公開しません。

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
print -r -- "  ATELIER H3 Local Studio を起動します（iPhone接続モード）"
line
print -r -- ""
print -r -- "  ・同じWi-Fi・同じルーターにつないだ iPhone から使えるモードです"
print -r -- "  ・インターネットには公開しません（外部公開ではありません）"
print -r -- "  ・PIN（数字）でログインします。PIN は起動のたびに新しくなります"
print -r -- "  ・macOS が「ネットワーク接続を許可しますか？」と聞いてきたら［許可］を選んでください"
print -r -- "  ・終了するときは、このウィンドウで Control + C を押してください"
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
CHECK_REPORT="$("$START_SH" --check --lan 2>&1)"
CHECK_STATUS=$?
if [ $CHECK_STATUS -ne 0 ]; then
    print -r -- ""
    print -r -- "$CHECK_REPORT"
    fail "起動前チェックに合格しませんでした（上の［エラー］の行をご覧ください）。" \
         "よくある原因: 別のウィンドウでアプリが起動したまま / モデルファイルの置き場所が違う。"
fi
print -r -- "  起動前チェック: 合格"
print -r -- ""
print -r -- "  iPhone用のURL・QRコード・PIN をこのあと表示します…"
print -r -- ""

# caffeinate -i: 生成中に Mac がスリープしないようにする。
# コマンドを引数に渡しているので、アプリが終わると caffeinate も一緒に終わる。
export ATELIER_CAFFEINATED=1
exec caffeinate -i "$START_SH" --lan
