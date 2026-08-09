"""P1/P3/P4 UI（設計書 §5.1〜§5.5・§18）。①新規生成 ②キュー ③完成動画 ④履歴。

スレッド方針（設計書 §8 / 指示 §8）:
- UI は AppService の**不変スナップショット**だけを読む。内部の可変状態には触れない。
- 「生成をキューに追加」は登録だけ行って即座に戻る（生成完了を待たない）。
- 進捗は gr.Timer が定期的にスナップショットを取得して反映する。
- 完成動画プレーヤーは Timer で毎回書き換えると再生が中断するため、
  最新完成IDを gr.State に入れ、**変化したときだけ** .change で差し替える。
- Timer コールバックは**必ず例外を飲み込んで値を返す**（1回の失敗で更新が永久停止しない）。

②キュータブ（P3・設計書 §5.3・§9.3・§13.2・§13.3）:
- 1 tick につきスナップショットは**1回だけ**取得し、そのタブの全表示へ同じものを渡す。
- 「最終処理中」推定: 最終ステップ到達後は PROGRESS が原理的に来ないため、
  `step == total_steps` かつ GENERATING かつ最後のイベントから
  `FINALIZING_QUIET_SEC` 秒以上無音なら「最終処理中（映像・音声の変換）」と表示する。
  実機では約2.5分続く正常な処理なので、**ハング表示にしない**（経過時間は出し続ける）。

③完成動画 / ④履歴タブ（P4・設計書 §5.4・§5.5・§10.5・§10.6）:
- 一覧・連結状態は Timer で更新するが、**プレーヤーには触れない**。
  選択IDを gr.State に入れ、**変化したときだけ** .change でプレビューを差し替える
  （①タブと同じ方式。毎 tick で書き換えると再生が中断するため）。
- 一覧・選択の描画は 1 tick につきスナップショットを**1回だけ**取得して作る。
- 連結は AppService がバックグラウンドで行い、UI は `concat_status()` を読むだけ
  （コールバックを長時間ブロックしない）。
- 配信するのは `data/outputs` と `data/concat` の成果物だけ。
  履歴JSON・ログ・その他のパスは gr.Video / gr.Image / gr.File へ渡さない（設計書 §15）。
- 継続生成（§5.2・§10.5）: 親IDだけを隠しテキストボックスで持ち、キーフレームのパスは
  投入時に `continuation_context(parent_id)` から取り直す
  （ブラウザ側から任意のパスを渡せないようにするため）。
- P4 の AppService API（completed_videos / history_rows / continuation_context /
  start_concat / concat_status / reveal_in_finder）は hasattr で防御し、
  欠けていても UI は起動する（その機能だけ「未対応」と日本語で表示する）。

iPhone 対応・文言・二重投入防止（P5・§6.1〜§6.4）:
- **レスポンシブ**: 画面幅 640px 以下でのみ 1カラムへ切り替える（`MOBILE_CSS`）。
  Mac の表示を変えないよう、レイアウトを変える指定はすべて
  `@media (max-width: 640px)` の中だけに置く。共通部分は新しいクラス
  （`.h3-scroll` など）と `video/img` の `max-width` だけにとどめる。
- **横スクロールを作らない**: 幅の広い表・ログは `.h3-scroll` で囲み、
  **その要素の中だけ**を横スクロールさせる（ページ全体は横に伸びない）。
- **二重投入防止**: 押した瞬間にボタンを無効化し、応答後に戻す（3段の `.then()`）。
  サーバ側でも `AppService.submit_generation_ex()` が短時間だけ冪等化する。
  Timer の outputs に生成ボタンを**入れない**（毎秒有効化し直すと競合するため）。
- **技術情報の分離**: 内部用語・例外文は画面の主要部へ出さず「詳しい情報」
  （Accordion）へ入れる。**エラーの種類とジョブIDは主要部に残す**（調査できるように）。
- **LANモード**: `lan_info` があれば接続先URLの案内を常時表示する。
  `lan_info` に PIN は含まれない（**UI へ PIN を到達させない設計**。PIN は Mac の画面だけ）。

API 互換（P1〜P3 の回帰防止）:
- `/on_submit` `/on_tick` `/on_estimate_change` `/on_insert_hint` `/on_queue_tick`
  `/on_cancel_queued` `/on_restart_worker` は引数・戻り値の数と順序を変えない。
  継続生成に対応した投入は **新しい `/on_submit_v2`** として追加し、
  `/on_submit`（5引数）は API 専用の非表示ボタンに残してある。
- P5 で `/on_select_video` `/on_select_history` の戻り値は 2→**3**（プレビュー・
  詳細・「詳しい情報」）へ増やした。上の7つの固定 API は変えていない。
"""

from __future__ import annotations

import html
import inspect
import logging
import math
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.app_service import (
    CONTINUATION_PREFIX,  # 設計書 §10.5。プリフィルの正本は AppService 側に置く
    AppService,
    format_duration,
)
from app.core.applog import recent_logs
from app.core.config import AppConfig
from app.core.contracts import (
    ALLOWED_NUM_FRAMES,
    ALLOWED_STEPS,
    FIXED_FPS,
    FIXED_HEIGHT,
    FIXED_WIDTH,
    SEED_MAX,
    EngineState,
    JobStage,
    JobStatus,
    QueueFullError,
    QueueSnapshot,
    RestartState,
    ValidationError,
)
from app.core.fileops import disk_free_gb, disk_state
from app.core.history import HistoryError

if TYPE_CHECKING:  # LANモード（P5）。A の実装前でも import 失敗で落ちないようにする
    from app.core.network import LanInfo

log = logging.getLogger("atelier.ui")

LENGTH_CHOICES = {
    "約2.33秒（56フレーム）": 56,
    "約5.17秒（124フレーム）": 124,
}
#: 8ステップは 4ステップの **約1.67倍**（P3 実機実測 677.6 ÷ 403）。
#: 設計時の想定「約2倍」は実測で否定されたので、画面に「約2倍」とは出さない。
STEP_CHOICES = {
    "4ステップ（高速）": 4,
    "8ステップ（高品質・時間は約1.7倍）": 8,
}

#: 「実測がある組み合わせ」＝この3つ。124フレーム×8ステップだけは推定値。
#: （P3 実測: 56f/4step 403秒・124f/4step 819秒・56f/8step 678秒）
MEASURED_COMBINATIONS = {(56, 4), (124, 4), (56, 8)}

#: iPhone 縦画面へ切り替える境界（このピクセル以下で1カラムにする）
MOBILE_BREAKPOINT_PX = 640

#: 生成ボタンの文言（押下中は無効化して文言を変える。P5 §6.2）
SUBMIT_LABEL = "▶ 生成をキューに追加"
SUBMIT_LABEL_BUSY = "⏳ 登録しています…"

ENGINE_STATE_LABELS = {
    EngineState.STARTING: "起動中…",
    EngineState.INITIALIZING_MODEL: "モデル初期化中…（初回は数分かかります）",
    EngineState.INITIALIZING_LORA: "高速生成用データ（Turbo LoRA）を読込中…",
    EngineState.READY: "待機中",
    EngineState.BUSY: "生成中",
    EngineState.DEAD: "生成エンジンが停止しました",
    EngineState.HALTED: "生成エンジンが停止しています（再起動が必要です）",
}

STAGE_LABELS = {
    JobStage.PREPARING: "生成準備中…",
    JobStage.GENERATING: "生成中",
    JobStage.SAVING: "動画・音声を保存中…",
    JobStage.LOADING_MODEL: "モデル初期化中…",
    JobStage.LOADING_LORA: "高速生成用データ（Turbo LoRA）を読込中…",
}

JOB_STATUS_LABELS = {
    JobStatus.QUEUED: "生成待ち",
    JobStatus.RUNNING: "実行中",
    JobStatus.SUCCESS: "完成",
    JobStatus.FAILED: "失敗",
    JobStatus.CANCELED: "取消済み",
    JobStatus.INTERRUPTED: "中断（アプリ終了のため）",
}

#: エラー分類の日本語表示（設計書 §13.3。JobView.error_category は文字列）
#: **この分類は画面の主要部に残す**（原因の切り分けができなくなるため。P5 §6.4）
ERROR_CATEGORY_LABELS = {
    "input": "入力エラー（プロンプトや設定の問題）",
    "mps": "MPS（Metal）エラー",
    "oom": "メモリ不足（OOM）",
    "pipeline": "生成処理の内部エラー",
    "model_state": "AIモデルの状態異常",
    "worker_dead": "生成エンジンの異常終了",
}

#: エラー分類ごとの「次にすること」（P5 §6.4。初心者が読んで動ける文にする）
ERROR_CATEGORY_ADVICE = {
    "input": "プロンプトや設定を見直して、もう一度お試しください。",
    "mps": (
        "Mac の映像処理でエラーが起きました。ほかの重いアプリを閉じてから、"
        "［⚠ ワーカーを再起動する］を実行し、もう一度お試しください。"
    ),
    "oom": (
        "メモリが足りませんでした。ほかのアプリを終了してからお試しください"
        "（長さを「約2.33秒」にすると必要なメモリが減ります）。"
    ),
    "pipeline": (
        "生成の途中で問題が起きました。もう一度お試しください。"
        "続けて失敗する場合は［⚠ ワーカーを再起動する］をお使いください。"
    ),
    "model_state": (
        "AIモデルの読み込み状態に問題があります。"
        "［⚠ ワーカーを再起動する］でモデルを読み直してください。"
    ),
    "worker_dead": (
        "生成の担当プログラムが予期せず終了しました。"
        "自動で起動し直します（戻らない場合は［⚠ ワーカーを再起動する］）。"
    ),
}

#: 実行方式（Execution Engine）の初心者向け表示。内部値（real / mock）は
#: 「詳しい情報」にだけ出す（P5 §6.4）。
MODE_LABELS = {
    "real": "本番モード（実際のAIモデルで生成します）",
    "mock": "お試しモード（ダミー動画がすぐ出ます）",
}

#: エンジン状態ごとの「いま何をすればよいか」（P5 §6.4）
ENGINE_STATE_HELP = {
    EngineState.STARTING: "生成の準備をしています。少しお待ちください。",
    EngineState.INITIALIZING_MODEL: (
        "AIモデルを読み込んでいます。この間も生成の依頼は追加できます。"
    ),
    EngineState.INITIALIZING_LORA: "高速化用のデータを読み込んでいます。",
    EngineState.READY: "いつでも生成を始められます。",
    EngineState.BUSY: "生成中です。追加した依頼は順番待ちになります。",
    EngineState.DEAD: "［⚠ ワーカーを再起動する］で元に戻せます。",
    EngineState.HALTED: "［⚠ ワーカーを再起動する］で元に戻せます。",
}

DIALOGUE_HINT = "<d>[Japanese] ここに日本語のセリフ</d>"

_DISK_LABELS = {
    "ok": "問題なし",
    "warn": "⚠️ 残りわずか。Finder で不要な動画を整理してください",
    "stop": "🛑 不足。新しい生成を受け付けられません",
}

# ------------------------------------------------------------------ P4 定数

#: 成果物の種別ラベル（③完成動画・④履歴の一覧で個別と連結を見分ける）
KIND_LABELS = {"clip": "個別", "concat": "連結"}

#: 連結成果物の作り方の別（P5.3-A）。④の「連結成果物」フィルタで区別して見せる。
CONCAT_KIND_LABELS = {"chain": "チェーン連結", "manual": "指定順連結", None: "連結"}

#: ③タブの表示名（P5.3-A で「完成動画」から改称）。**内部IDは `tab_videos` のまま**。
#: 一覧表を④へ移し、③は「選ぶ・見る・つなげる」作業画面になったため。
VIDEOS_TAB_LABEL = "完成・編集"

#: 「連結成果物」フィルタの内部値（P5.3-A）。JobStatus のどの値とも重ならない
#: 番兵にしてある（状態で絞る既存の経路へ紛れ込ませないため）。
CONCAT_PRODUCTS_FILTER = "連結成果物"
CONCAT_PRODUCTS_SENTINEL = "__concat_products__"

#: ④履歴の状態フィルタ。`history_rows(status=...)` へ渡す値（すべて＝None）。
#: QUEUED / RUNNING は正常終了後には残らないが、異常データを隠さないため選択肢に残す。
#: 末尾の「連結成果物」だけは状態ではなく**種類**で絞る（P5.3-A。③から一覧表を
#: 無くしたので、連結した動画を表で見られる唯一の場所になる）。
HISTORY_FILTERS: dict[str, str | None] = {
    "すべて": None,
    "成功": "success",
    "失敗": "failed",
    "取消": "canceled",
    "中断": "interrupted",
    "実行待ち": "queued",
    "実行中": "running",
    CONCAT_PRODUCTS_FILTER: CONCAT_PRODUCTS_SENTINEL,
}

#: 連結の進行状態（`concat_service.ConcatStatus.state`）→ 日本語。
#: 本来は `ConcatStatus.state_label` を使う。ここは古い版・別実装への保険。
CONCAT_STATE_LABELS = {
    "idle": "待機中（連結は実行されていません）",
    "resolving": "チェーンを確認中…",
    "concatenating": "連結中…（映像と音声をつなげています）",
    "verifying": "検証中…（映像・音声・長さを確かめています）",
    "done": "完成",
    "failed": "失敗",
}

#: 連結の開始直後だけ「即座に失敗したか」を確認する上限（コールバックを止めない）
CONCAT_IMMEDIATE_WAIT_SEC = 0.6

#: 「この版では未対応」の共通文言（AppService 側の API がまだ無い場合に使う）
_UNSUPPORTED = "⚠️ この版では対応していません（アプリの更新が必要です）。"

#: 例外文を画面に出さない代わりの案内（P5 §6.4）。原因は必ずログへ記録している。
_LOG_HINT = (
    "。技術的な原因は「①新規生成」タブの"
    "「詳しい情報（アプリの動作ログ）」に記録しています。"
)

#: Finder 表示が Mac 専用であることの説明（iPhone から押しても手元には何も出ない）
FINDER_NOTE = (
    "ℹ️ **［Finderで表示］は Mac の画面だけで働く機能です。**"
    "iPhone・iPad から押すと **Mac 側の Finder** がその動画を選んだ状態で開きます"
    "（iPhone の画面には何も表示されません）。"
    "iPhone に動画を保存したいときは、上のプレビューを長押しして"
    "「ビデオを保存」を選んでください。"
)

#: 指定順連結（P5.2）。本数の範囲は下位層（history / concat_manifest）と同じ値。
#: UI は入口で親切に止めるだけで、**最終的な判定は必ずサーバ側が行う**。
MIN_CUSTOM_CLIPS = 2
MAX_CUSTOM_CLIPS = 20
CUSTOM_CONCAT_TITLE = "複数の動画を選んで連結（順番指定）"
CUSTOM_CONCAT_LABEL = "▶ この順番で連結（2本以上選んでください）"

#: 順番指定連結の補助操作（P5.3-A 仕上げ）。
#: 「削除」はファイルを消す操作と紛らわしいので**「候補から外す」**にした。
#: 実際に消えるのは、この画面で組み立てている**順番だけ**である。
#: アプリ内ゴミ箱（P5.3-B）。移動先は `data_root/trash`（config が正本）。
#: ここはあくまで画面に出す文字列で、実際のパスは `cfg.trash_dir` から解決する。
TRASH_DIR_LABEL = "trash"
TRASH_BUTTON_LABEL = "🗑 ゴミ箱へ移動"

CUSTOM_ADD_LABEL = "＋ 連結候補へ追加"
CUSTOM_UP_LABEL = "↑ 1つ上へ"
CUSTOM_DOWN_LABEL = "↓ 1つ下へ"
CUSTOM_REMOVE_LABEL = "－ 候補から外す"
CUSTOM_CLEAR_LABEL = "連結候補をすべて解除"


# --------------------------------------------- 指定順連結の並び操作（純粋関数）
#
# 並びを変える処理は**すべてここに置く**。サービスにも画面にも触れず、
# 「今の並び → 新しい並びと日本語メッセージ」だけを返すので、単体で試験できる。
# 画面はこの結果から毎回組み立て直す（`gr.State` の中身がそのまま入力になる）。


def custom_order_add(
    order: list[str], job_id: str, known_ids: set[str]
) -> tuple[list[str], str]:
    """候補へ1本足す。重複・上限・一覧に無いIDはここで断る。"""
    order = [str(v) for v in (order or [])]
    target = str(job_id or "").strip()
    if not target:
        return order, "⚠️ 追加する動画を選んでください。"
    if target in order:
        return order, (
            f"⚠️ `{target}` はすでに連結候補に入っています"
            "（同じ動画は1回だけ使えます）。"
        )
    if len(order) >= MAX_CUSTOM_CLIPS:
        return order, (
            f"⚠️ 一度に連結できるのは {MAX_CUSTOM_CLIPS} 本までです"
            "（不要な動画を削除してから追加してください）。"
        )
    if known_ids is not None and target not in known_ids:
        return order, f"⚠️ `{target}` は連結できる個別動画の一覧にありません。"
    order = order + [target]
    return order, f"✅ `{target}` を {len(order)} 番目に追加しました。"


def custom_order_move(order: list[str], job_id: str, delta: int) -> tuple[list[str], str]:
    """対象を1つ上（-1）／下（+1）へ動かす。端では動かさずに理由を返す。"""
    order = [str(v) for v in (order or [])]
    target = str(job_id or "").strip()
    if not target:
        return order, "⚠️ 動かす動画を「対象を選ぶ」から選んでください。"
    if target not in order:
        return order, f"⚠️ `{target}` は連結候補に入っていません。"
    index = order.index(target)
    new_index = index + delta
    if new_index < 0:
        return order, f"⚠️ `{target}` はすでに1番目です。"
    if new_index >= len(order):
        return order, f"⚠️ `{target}` はすでに最後です。"
    moved = list(order)
    moved[index], moved[new_index] = moved[new_index], moved[index]
    return moved, f"✅ `{target}` を {new_index + 1} 番目に移動しました。"


def custom_order_remove(order: list[str], job_id: str) -> tuple[list[str], str]:
    order = [str(v) for v in (order or [])]
    target = str(job_id or "").strip()
    if not target:
        return order, "⚠️ 削除する動画を「対象を選ぶ」から選んでください。"
    if target not in order:
        return order, f"⚠️ `{target}` は連結候補に入っていません。"
    return [j for j in order if j != target], f"✅ `{target}` を連結候補から外しました。"


def custom_order_clear(order: list[str]) -> tuple[list[str], str]:
    """候補リストを空にするだけ。**実行中の連結は取り消さない**。"""
    had = len(order or [])
    if not had:
        return [], "連結候補はもともと空です。"
    return [], f"✅ 連結候補（{had}本）をすべて解除しました。"

#: 未選択のときに③④の右側へ出す案内文。**選択欄の位置とボタン名がタブごとに違う**
#: ため、共通化せず別々に持つ（P5.1: ③は選択欄を一覧の上へ移した）。
VIDEOS_EMPTY_NOTE = (
    "上の選択欄から動画を選び、［選んだ動画を表示］を押すと、"
    "ここに詳細とプレビューが出ます。"
)
HISTORY_EMPTY_NOTE = "上の一覧から記録を選ぶと、ここに詳細が出ます。"

# ------------------------------------------------------------------ P5 定数（§6.1）

#: iPhone Safari 縦画面向けのスタイル。
#:
#: 方針（**Mac の表示を回帰させない**ことが最優先）:
#: - レイアウトを変える指定は、すべて `@media (max-width: 640px)` の中だけに書く。
#: - 共通部分で触るのは「新しく足したクラス」と `video / img` の最大幅だけにする。
#:   `.gradio-container` の幅やパディングは共通部分では**触らない**
#:   （触ると Mac の中央寄せレイアウトが変わってしまう）。
#: - 横スクロールは `.h3-scroll` の**内側だけ**に閉じ込める。ページ本体には
#:   `overflow-x: hidden` を掛けない（内容が読めなくなるのを避けるため）。
#: - タップ領域は 44×44px 以上（Apple の Human Interface Guidelines の最小値）。
MOBILE_CSS = f"""
/* ---- 共通（Mac・iPhone の両方に効く。既存レイアウトは変えない） ---- */
.h3-scroll {{ overflow-x: auto; -webkit-overflow-scrolling: touch; max-width: 100%; }}
.h3-scroll table {{ margin: 0; }}
/* 連結候補の一覧だけを縦スクロールにする（P5.3-A）。20本選んでもページ全体は
   伸びず、2〜5本のときは中身の高さのままなので余分な空白も出ない。
   横は広げない（`overflow-x` を触らないので横スクロールは発生しない）。 */
.h3-vscroll {{
  max-height: 14em; overflow-y: auto; -webkit-overflow-scrolling: touch;
  max-width: 100%;
}}
.h3-note {{ font-size: 0.92em; line-height: 1.6; opacity: 0.9; }}
/* ---- 順番指定連結のパネルと補助ボタン（P5.3-A 仕上げ） ----
   Gradio の Group 既定背景は薄い灰色で、secondary ボタンの灰色とほとんど同じに
   なるため、補助操作が「押せるもの」に見えなかった。パネルを白くしてボタンを
   白＋枠線のアウトライン型にし、境界をはっきりさせる。
   **すべて `.h3-` 付きの選択子だけ**で書く（グローバルな button 指定はしない）。
   クラスが button 自身に付く場合と包み要素に付く場合の両方へ効かせる。 */
/* パネルは**わずかにオフホワイト**にして、白いボタンとの差を背景でも作る
   （枠線が主役だが、背景も同一にしないことで同化の再発を防ぐ） */
.h3-panel {{
  background: #fcfcfd; border: 1px solid #d0d5dd; border-radius: 10px;
  padding: 12px 14px; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
}}
/* Gradio が内側の器（`.styler` / `.form`）へ独自の灰色を敷くため、パネルの中だけ
   透過させて上の背景を見せる。**`.h3-panel` の下に限定**しているので他画面には
   影響しない（入力欄そのものの背景は触らない）。 */
.h3-panel > .styler, .h3-panel .form {{ background: transparent !important; }}
.h3-btn, .h3-btn button {{
  background: #ffffff !important; border: 1px solid #98a2b3 !important;
  color: #1d2939 !important; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06) !important;
}}
.h3-btn:hover:not(:disabled), .h3-btn button:hover:not(:disabled) {{
  background: #f2f4f7 !important; border-color: #475467 !important;
}}
.h3-btn:focus-visible, .h3-btn button:focus-visible {{
  outline: 2px solid #1570ef !important; outline-offset: 2px !important;
}}
/* 無効時（現在この画面のボタンは無効にならないが、将来 `interactive=False` を
   使う場合に備えて定義しておく）。`cursor` は効くが**色は Gradio 6.22.0 の
   ボタン CSS が優先**するため、色の変化は保証しない（クラスを重ねて優先度を
   上げても同様だった。到達しない状態なので実害はない）。 */
.h3-btn.h3-btn:disabled, .h3-btn button:disabled {{
  background-color: #f9fafb !important; border-color: #e4e7ec !important;
  color: #98a2b3 !important; box-shadow: none !important; cursor: not-allowed !important;
}}
/* 追加は「次に進む操作」なのでオレンジの枠線だけにする（塗りつぶしは最終実行だけ） */
.h3-btn-accent, .h3-btn-accent button {{
  background: #ffffff !important; border: 1px solid #ff7c00 !important;
  color: #b35300 !important; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06) !important;
}}
.h3-btn-accent:hover:not(:disabled), .h3-btn-accent button:hover:not(:disabled) {{
  background: #fff4e6 !important; border-color: #e06c00 !important;
}}
.h3-btn-accent:focus-visible, .h3-btn-accent button:focus-visible {{
  outline: 2px solid #1570ef !important; outline-offset: 2px !important;
}}
.h3-btn-accent.h3-btn-accent:disabled, .h3-btn-accent button:disabled {{
  background-color: #f9fafb !important; border-color: #e4e7ec !important;
  color: #98a2b3 !important; box-shadow: none !important; cursor: not-allowed !important;
}}
.h3-lan {{
  border: 2px solid #2e7d32; background: #eaf5ea; color: #1b3d1b;
  border-radius: 8px; padding: 10px 12px; line-height: 1.7; word-break: break-word;
}}
.h3-lan a {{ word-break: break-all; }}
/* 動画がはみ出さないことだけは全画面共通で保証する。
   img には触れない（Gradio がアイコン等に付ける固定高さを壊さないため。
   iPhone 向けの img 調整はメディアクエリの中だけで行う）。 */
.gradio-container video {{ max-width: 100%; height: auto; }}

/* ---- iPhone Safari 縦画面（{MOBILE_BREAKPOINT_PX}px 以下）だけ ---- */
@media (max-width: {MOBILE_BREAKPOINT_PX}px) {{
  /* 1カラムにする。min-width:0 を入れないと中身が親を押し広げて横スクロールが出る */
  .h3-row {{ flex-direction: column !important; flex-wrap: nowrap !important; }}
  .h3-row > * {{
    width: 100% !important; min-width: 0 !important; flex: 1 1 100% !important;
  }}
  .gradio-container {{ max-width: 100% !important; padding: 8px !important; }}
  /* 継続元サムネイル等が画面幅を超えないようにする（iPhone のときだけ） */
  .gradio-container img {{ max-width: 100%; height: auto; }}
  .gradio-container * {{ min-width: 0; }}

  /* タップ領域を 44×44px 以上にする */
  .gradio-container button,
  .gradio-container .gr-button,
  .gradio-container select,
  .gradio-container input[type="text"],
  .gradio-container input[type="number"],
  .gradio-container summary {{ min-height: 44px !important; }}
  /* ラジオ・チェックボックスは「入力を包む label」だけを大きくする。
     すべての label を大きくすると入力欄の見出しにまで余白が付いてしまう。
     `:has()` が無い古い Safari では、下の input 自体の最小サイズだけが効く。 */
  .gradio-container label:has(> input[type="checkbox"]),
  .gradio-container label:has(> input[type="radio"]) {{
    min-height: 44px; display: flex; align-items: center;
  }}
  .gradio-container input[type="checkbox"],
  .gradio-container input[type="radio"] {{
    min-width: 24px !important; min-height: 24px !important;
  }}
  .h3-tap button {{ min-height: 44px !important; }}

  /* 表と長いテキストは、その枠の中だけで横に送る */
  .h3-scroll table {{ font-size: 0.82em; }}
  .gradio-container h1 {{ font-size: 1.35rem !important; }}
  .gradio-container pre, .gradio-container code {{ white-space: pre-wrap; word-break: break-word; }}
}}
"""


def _lan_banner_html(lan_info: "LanInfo | None") -> str:
    """LANモードの案内（接続先URL・ユーザー名）。**PIN は絶対に表示しない**。

    `lan_info` には設計上 PIN が入らない（`url` / `host` / `port` のみ）。
    PIN は Mac の画面（ターミナル）だけに出す。ここでも読み取らない。
    """
    if lan_info is None:
        return ""
    url = str(getattr(lan_info, "url", "") or "").strip()
    if not url:
        host = str(getattr(lan_info, "host", "") or "").strip()
        port = getattr(lan_info, "port", None)
        url = f"http://{host}:{port}" if host and port else ""
    try:  # ユーザー名は lanauth の正本を使う（無ければ既定の "h3"）
        from app.core.lanauth import LAN_USERNAME
    except Exception:  # A の実装前・import 失敗でも UI は起動する
        LAN_USERNAME = "h3"
    safe_url = html.escape(url)
    return (
        '<div class="h3-lan">'
        "<p><b>📱 iPhone接続モード（同じWi-Fi内だけ）</b></p>"
        + (
            f'<p>iPhone・iPad の Safari で <b>{safe_url}</b> を開いてください。</p>'
            if safe_url
            else "<p>接続先アドレスを取得できませんでした。Mac の画面をご確認ください。</p>"
        )
        + f"<p>ログインの<b>ユーザー名は「{html.escape(LAN_USERNAME)}」</b>、"
        "パスワードは <b>Mac の画面に出ている数字</b>を入力します"
        "（安全のため、この画面には数字を表示しません）。</p>"
        "<p>同じWi-Fiにつながっている機器だけが開けます。"
        "インターネットには公開していません。"
        "終了するときは Mac の画面で Control+C を押してください。</p>"
        "</div>"
    )


def _attr(row, name: str, default=None):
    """行オブジェクト（VideoRow）の属性を安全に読む。None のときは既定値にする。"""
    value = getattr(row, name, default)
    return default if value is None else value


def _row_key(row) -> str:
    """一覧の選択キー。連結行と個別行で job_id が重なりうるので種別を前置する。"""
    return f"{_attr(row, 'kind', 'clip')}:{_attr(row, 'job_id', '')}"


def _split_key(key: str) -> tuple[str, str]:
    text = str(key or "").strip()
    if ":" in text:
        kind, job_id = text.split(":", 1)
        return kind, job_id
    return "clip", text


def _status_value(row) -> str:
    """JobStatus / 文字列のどちらで来ても小文字の状態文字列にそろえる。"""
    status = getattr(row, "status", None)
    if status is None:
        return ""
    value = getattr(status, "value", status)
    return str(value).lower()


def _status_label(row) -> str:
    value = _status_value(row)
    if not value:
        return "—"
    try:
        return JOB_STATUS_LABELS.get(JobStatus(value), value)
    except ValueError:
        return value


def _fmt_dt(dt: datetime | None) -> str:
    if not isinstance(dt, datetime):
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _cell(text: object) -> str:
    """Markdown テーブルのセルに安全に入れる（区切り記号と改行を潰す）。"""
    return str(text if text is not None else "—").replace("|", "｜").replace("\n", " ")


def _seed_cell(row) -> str:
    # 指定順連結の成果物には seed という概念が無い（複数動画をつないだ結果なので、
    # 1つの値に決まらない）。「ランダム」と出すと誤解を招くので伏せる。
    if getattr(row, "concat_kind", None) == "manual":
        return "—"
    used = getattr(row, "seed_used", None)
    requested = getattr(row, "seed_requested", None)
    if used is not None:
        return f"{used}（ランダム採番）" if requested is None else str(used)
    return "ランダム" if requested is None else str(requested)


def _sorted_newest_first(rows: list) -> list:
    """新しい順。日時が欠けている／tz が混在していても並べ替えで落ちない。"""
    try:
        return sorted(
            rows,
            key=lambda r: getattr(r, "created_at", None) or datetime.min,
            reverse=True,
        )
    except TypeError:  # naive と aware の混在など。元の順序を保つ
        return list(rows)

#: 最終ステップ到達後、この秒数だけイベントが途切れたら「最終処理中」と表示する。
#: 最終ステップ以降は PROGRESS が原理的に発生しないため、この無音は
#: VAE デコードと音声・動画の書き出し（実機で約150秒）を意味する。誤検知しても
#: 表示が少し早まるだけで害はない一方、長すぎるとハングに見えるため短めに取る。
FINALIZING_QUIET_SEC = 20.0

#: 進捗バーの桁数（②キュータブ・①新規生成タブ共通）
_BAR_WIDTH = 20


def _length_label(num_frames: int) -> str:
    for label, value in LENGTH_CHOICES.items():
        if value == num_frames:
            return label
    return f"{num_frames}フレーム"


def _step_label(steps: int) -> str:
    for label, value in STEP_CHOICES.items():
        if value == steps:
            return label
    return f"{steps}ステップ"


def _progress_bar(step: int, total: int) -> str:
    filled = max(0, min(_BAR_WIDTH, int(_BAR_WIDTH * step / total)))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _seed_text(job) -> str:
    """シード表示。実際に使われた値が分かればそちらを優先する（設計書 §10.4）。"""
    if job.seed_used is not None:
        suffix = "（ランダム採番）" if job.seed_requested is None else ""
        return f"{job.seed_used}{suffix}"
    return "ランダム" if job.seed_requested is None else str(job.seed_requested)


def _is_finalizing(job, now: datetime) -> bool:
    """「最終処理中（映像・音声の変換）」を推定する（P2 実測: 4/4 到達後に約150秒無音）。

    最終ステップに到達したあとは PROGRESS が原理的に発生しないため、
    そこからの無音は必ずデコード・保存処理を意味する。ハングではない。
    """
    if job is None or job.stage != JobStage.GENERATING:
        return False
    if not job.step or not job.total_steps or job.step < job.total_steps:
        return False
    last_event_at = getattr(job, "last_event_at", None)
    if last_event_at is None:
        return False
    try:
        quiet = (now - last_event_at).total_seconds()
    except TypeError:  # tz-aware / naive の混在でも UI は壊さない
        return False
    return quiet >= FINALIZING_QUIET_SEC


def _job_stage_text(job, now: datetime) -> str:
    """ジョブのステージ文言（設計書 §9.3 ＋ P3 の「最終処理中」）。"""
    if _is_finalizing(job, now):
        return (
            f"最終処理中（映像・音声の変換）… "
            f"ステップ {job.step}/{job.total_steps} は完了しています"
        )
    if job.stage == JobStage.GENERATING and job.step and job.total_steps:
        return f"生成中 ステップ {job.step}/{job.total_steps}"
    return STAGE_LABELS.get(job.stage, "生成中")


def _engine_state_text(snap: QueueSnapshot) -> str:
    """エンジン状態の1行表示（再起動待ち・再初期化中・停止中を含む。§9.2・§13.3）。"""
    restart_state = getattr(snap, "restart_state", RestartState.IDLE)
    halted_reason = getattr(snap, "halted_reason", None)
    if restart_state == RestartState.BACKOFF:
        remaining = math.ceil(max(0.0, getattr(snap, "backoff_remaining_sec", 0.0)))
        return f"再起動待ち（あと約{remaining}秒でワーカーを起動し直します）"
    if restart_state == RestartState.RESTARTING:
        return "再初期化中…（AIモデルを読み直しています）"
    if restart_state == RestartState.HALTED or snap.engine_state == EngineState.HALTED:
        reason = halted_reason or "連続して失敗したため、生成を停止しています"
        return f"停止中（{reason}）"
    return ENGINE_STATE_LABELS.get(snap.engine_state, str(snap.engine_state))


def _halted_banner_reason(snap: QueueSnapshot) -> str | None:
    """赤色バナーに出す停止理由。自動復旧が進行中のときは出さない（誤警報を避ける）。"""
    restart_state = getattr(snap, "restart_state", RestartState.IDLE)
    if restart_state in (RestartState.BACKOFF, RestartState.RESTARTING):
        # 自動再起動の途中は engine が一時的に DEAD / STARTING になる。異常ではない。
        return None
    reason = getattr(snap, "halted_reason", None)
    if restart_state == RestartState.HALTED:
        return reason or "連続して失敗したため生成を停止しています"
    if snap.engine_state in (EngineState.HALTED, EngineState.DEAD):
        return reason or "生成エンジンが停止しています"
    return None


def _banner_html(snap: QueueSnapshot) -> str:
    """HALTED と受付停止の赤色バナー（設計書 §13.2・§13.3）。無ければ空文字。"""
    messages: list[str] = []
    halted_reason = _halted_banner_reason(snap)
    if halted_reason:
        messages.append(
            f"🛑 生成エンジンが停止しています: {halted_reason} — "
            "下の［⚠ ワーカーを再起動する］を押してください"
            "（待機中のジョブはキューに残ります）"
        )
    blocked = getattr(snap, "intake_blocked_reason", None)
    if blocked:
        messages.append(
            f"🛑 新しい生成の受付を停止しています: {blocked} — "
            "Finder で不要な動画を整理してください"
            "（すでに完成した動画の再生・表示は続けられます）"
        )
    if not messages:
        return ""
    body = "".join(
        f"<p style=\"margin:4px 0;\">{html.escape(m)}</p>" for m in messages
    )
    return (
        '<div style="background:#fdecea;border:2px solid #d32f2f;color:#8f1414;'
        'padding:12px 14px;border-radius:8px;font-weight:600;line-height:1.6;">'
        f"{body}</div>"
    )


def build_ui(
    cfg: AppConfig,
    mode: str,
    service: AppService,
    lan_info: "LanInfo | None" = None,
):
    """画面を組み立てて返す（起動は呼び出し側の責任）。

    `lan_info` を渡すと LANモード（iPhone 接続）の案内を常時表示する。
    `lan_info` に PIN は含まれない（PIN を UI 層へ到達させない設計。P5 §3）。
    """
    import gradio as gr

    def _elapsed_of(started_at: datetime | None, now: datetime | None = None) -> str:
        if started_at is None:
            return "--:--"
        return format_duration(((now or datetime.now()) - started_at).total_seconds())

    def _disk_text() -> str:
        """空き容量表示。取得できない場合も UI を壊さない。"""
        try:
            free = disk_free_gb(cfg.data_root)
        except OSError:
            log.exception("空き容量を取得できませんでした")
            return ""
        label = _DISK_LABELS[
            disk_state(free, cfg.warn_free_disk_gb, cfg.stop_free_disk_gb)
        ]
        return f" ｜ 空き容量: {free:.0f}GB（{label}）"

    def _header_text(snap) -> str:
        # 再起動待ち・再初期化中・停止中も全タブ共通のヘッダへ出す（P3）。
        engine_label = _engine_state_text(snap)
        if snap.current is not None:
            job = snap.current
            now = datetime.now()
            if _is_finalizing(job, now):
                state = "最終処理中（映像・音声の変換）"
            elif job.stage == JobStage.GENERATING and job.step and job.total_steps:
                state = f"生成中 {job.step}/{job.total_steps}ステップ"
            else:
                state = STAGE_LABELS.get(job.stage, "生成中")
            state += f" ｜ 経過 {_elapsed_of(job.started_at, now)}"
        else:
            state = engine_label
        # 「実行方式: real/mock」は内部の言い方なので、初心者向けの言葉にする
        # （内部値そのものは「詳しい情報」にだけ出す。P5 §6.4）
        return (
            f"**状態**: {state} ｜ **待機** {snap.queue_size}件 ｜ "
            f"{MODE_LABELS.get(mode, mode)}{_disk_text()}"
        )

    def _progress_text(snap) -> str:
        lines: list[str] = []
        if snap.current is None:
            engine_label = _engine_state_text(snap)
            lines.append(f"### 現在の処理\n進行中の生成はありません（{engine_label}）")
        else:
            job = snap.current
            now = datetime.now()
            stage = _job_stage_text(job, now)
            if not _is_finalizing(job, now) and (
                job.stage == JobStage.GENERATING and job.step and job.total_steps
            ):
                stage = (
                    f"生成中 ステップ {job.step}/{job.total_steps}"
                    f"  `{_progress_bar(job.step, job.total_steps)}`"
                )
            lines.append(
                "### 現在の処理\n"
                f"**{job.job_id}**\n\n"
                f"{stage}\n\n"
                f"経過 {_elapsed_of(job.started_at)} ／ 目安 "
                f"{service.estimate_text(job.num_frames, job.steps)}\n\n"
                f"{job.duration_label}・{job.steps}ステップ・"
                f"シード {job.seed_requested if job.seed_requested is not None else 'ランダム'}"
            )
        if snap.queued:
            waiting = "\n".join(
                f"{i + 1}. {j.job_id}（{j.duration_label}・{j.steps}ステップ）"
                for i, j in enumerate(snap.queued)
            )
            lines.append(f"### 待機中（{snap.queue_size}件）\n{waiting}")
        else:
            lines.append(f"### 待機中（{snap.queue_size}件）\nなし")

        if snap.last_finished is not None:
            j = snap.last_finished
            if j.status == JobStatus.SUCCESS:
                lines.append(
                    f"### 直前の結果\n完成 ✅ {j.job_id}"
                    f"（処理時間 {format_duration(j.elapsed_sec)}）"
                )
            elif j.status == JobStatus.FAILED:
                lines.append(f"### 直前の結果\nエラー ❌ {j.job_id}: {j.error or '原因不明'}")
        return "\n\n".join(lines)

    def _latest_id() -> str:
        latest = service.latest_completed()
        return latest.job_id if latest else ""

    def _snapshot_texts() -> tuple[str, str]:
        """ヘッダと進捗を**同一スナップショット**から作る（表示の食い違いを防ぐ）。"""
        snap = service.snapshot()
        return _header_text(snap), _progress_text(snap)

    # ------------------------------------------------------- ②キュータブの描画
    #
    # 描画関数はすべて「1つのスナップショット」を受け取る純粋関数にしてある。
    # 呼び出し側（_queue_view）でスナップショットを**1回だけ**取得することで、
    # エンジン状態・現在の処理・待機一覧の食い違いを構造的に防ぐ。

    _restart_available = hasattr(service, "restart_worker")
    #: Dropdown の choices は変化したときだけ送る（毎秒送ると選択中の操作を邪魔するため）

    def _queue_engine_text(snap: QueueSnapshot) -> str:
        """②タブのエンジン状態（**初心者向けの主要部**。数値は「詳しい情報」へ）。"""
        state = _engine_state_text(snap)
        help_text = ENGINE_STATE_HELP.get(snap.engine_state, "")
        lines = [
            "### 生成エンジンの状態",
            f"**{state}**",
            help_text,
            f"完了した動画 {snap.succeeded_total}本 ／ 失敗 {snap.failed_total}本",
            f"いまは{MODE_LABELS.get(mode, mode)}",
        ]
        return "\n\n".join(line for line in lines if line)

    def _queue_detail_text(snap: QueueSnapshot) -> str:
        """②タブの「詳しい情報」（技術的な値。主要部には出さない。P5 §6.4）。"""
        restart_state = getattr(snap, "restart_state", RestartState.IDLE)
        lines = [
            "困ったときにこの内容をそのままお伝えいただくと調査できます。",
            "",
            f"- 実行方式（execution_engine）: {mode}",
            f"- エンジン状態（engine_state）: "
            f"{getattr(snap.engine_state, 'value', snap.engine_state)}",
            f"- 再起動の状態（restart_state）: "
            f"{getattr(restart_state, 'value', restart_state)}",
            f"- ジョブの受け渡し（dispatcher）: "
            f"{'稼働中' if snap.running else '停止中'}",
            f"- 受付 {snap.accepted_total}件 ／ 完了 {snap.succeeded_total}件 ／ "
            f"失敗 {snap.failed_total}件",
            f"- 連続失敗 {getattr(snap, 'consecutive_failures', 0)}回 ／ "
            f"自動再起動 {getattr(snap, 'restart_total', 0)}回",
        ]
        job = snap.last_finished
        if job is not None and job.status == JobStatus.FAILED:
            lines.append(
                f"- 直前の失敗 `{job.job_id}` の内容: {job.error or '（記録なし）'}"
            )
            if job.error_category:
                lines.append(f"- 直前の失敗の分類（error_category）: {job.error_category}")
        return "\n".join(lines)

    def _queue_current_text(snap: QueueSnapshot, now: datetime) -> str:
        job = snap.current
        if job is None:
            return (
                "### 現在の処理\n"
                f"進行中の生成はありません（{_engine_state_text(snap)}）"
            )
        stage = _job_stage_text(job, now)
        bar = ""
        if job.step and job.total_steps:
            percent = int(100 * min(job.step, job.total_steps) / job.total_steps)
            bar = f"`{_progress_bar(job.step, job.total_steps)}` {percent}%\n\n"
        lines = [
            "### 現在の処理",
            f"**{job.job_id}**",
            f"「{job.prompt_head}」" if job.prompt_head else "",
            f"状態: {JOB_STATUS_LABELS.get(job.status, str(job.status))}"
            f" ／ ステージ: {stage}",
            bar.rstrip("\n"),
            f"経過 {_elapsed_of(job.started_at, now)} ／ 目安 "
            f"{service.estimate_text(job.num_frames, job.steps)}",
            f"長さ {job.duration_label}（{job.num_frames}フレーム）"
            f" ／ {job.steps}ステップ ／ シード {_seed_text(job)}",
        ]
        if _is_finalizing(job, now):
            lines.append(
                "ℹ️ 映像と音声への変換中です。実機では**2〜3分ほど**かかります"
                "（この間は進捗が動きませんが、故障ではありません）。"
            )
        if getattr(job, "stalled", False):
            lines.append(
                "⚠️ 通常より時間がかかっています"
                "（自動では止めません。どうしても戻らない場合のみ"
                "［⚠ ワーカーを再起動する］をお使いください）。"
            )
        return "\n\n".join(line for line in lines if line)

    def _queue_waiting_text(snap: QueueSnapshot) -> str:
        if not snap.queued:
            return "### 待機中のジョブ（0件）\n待機中のジョブはありません。"
        rows = [
            "| 順番 | ジョブID | 内容 | 長さ | ステップ | シード |",
            "|---:|---|---|---|---:|---|",
        ]
        for i, j in enumerate(snap.queued):
            head = (j.prompt_head or "").replace("|", "｜").replace("\n", " ")
            rows.append(
                f"| {i + 1} | `{j.job_id}` | {head} | {j.duration_label} | "
                f"{j.steps} | {_seed_text(j)} |"
            )
        table = "\n".join(rows)
        return (
            f"### 待機中のジョブ（{snap.queue_size}件）\n{table}\n\n"
            "取り消したいときは、下の「取り消す待機ジョブ」から選んでボタンを押してください。"
        )

    def _queue_error_text(snap: QueueSnapshot) -> str:
        """直前の失敗。**エラーの種類とジョブIDは必ず残す**（P5 §6.4）。

        英語の例外文などの技術的な内容は下の「詳しい情報」へ回し、ここには
        「次に何をすればよいか」を日本語で書く。
        """
        j = snap.last_finished
        if j is None or j.status != JobStatus.FAILED:
            return "### 直前の失敗\n直近で失敗した生成はありません。"
        category = j.error_category
        label = ERROR_CATEGORY_LABELS.get(category, category or "分類なし")
        advice = ERROR_CATEGORY_ADVICE.get(
            category, "もう一度お試しください。続けて失敗する場合は下の「詳しい情報」をご確認ください。"
        )
        return (
            "### 直前の失敗\n"
            f"❌ **{j.job_id}**\n\n"
            f"エラーの種類: {label}\n\n"
            f"{advice}\n\n"
            "技術的な内容は下の「詳しい情報」に出しています。"
        )

    def _cancel_choices_update(snap: QueueSnapshot):
        """取消候補（待機中の job_id）を毎回そのまま送る。

        以前は「前回と同じなら送らない」キャッシュを持っていたが、キャッシュが
        プロセス全体で1つだったため、ブラウザを再読込した新しいセッションでは
        初期値（空）のまま更新が飛ばず、待機ジョブがあるのに取消候補が空になった。
        `value` は送らないので、選択中の項目が候補に残っていれば保持される。
        """
        return gr.update(choices=[j.job_id for j in snap.queued])

    def _render_queue(snap: QueueSnapshot) -> tuple:
        now = datetime.now()
        return (
            _banner_html(snap),
            _queue_engine_text(snap),
            _queue_current_text(snap, now),
            _queue_waiting_text(snap),
            _queue_error_text(snap),
            _cancel_choices_update(snap),
        )

    def _queue_view() -> tuple:
        """スナップショットを**1回だけ**取り、②タブの全表示を作る。例外は飲み込む。"""
        try:
            return _render_queue(service.snapshot())
        except Exception:  # 設計書 §13.2: UI の更新を永久に止めない
            log.exception("キュー状態の取得に失敗しました")
            unavailable = "⚠️ 状態を取得できません（「詳しい情報（アプリの動作ログ）」をご確認ください）"
            return (
                "",
                f"### 生成エンジンの状態\n{unavailable}",
                f"### 現在の処理\n{unavailable}",
                f"### 待機中のジョブ\n{unavailable}",
                f"### 直前の失敗\n{unavailable}",
                gr.update(),
            )

    def _queue_detail_view() -> str:
        """②タブ「詳しい情報」の更新（主要部と分けた低頻度 Timer で回す）。"""
        try:
            return _queue_detail_text(service.snapshot())
        except Exception:  # 設計書 §13.2
            log.exception("キューの詳しい情報を取得できませんでした")
            return "⚠️ 詳しい情報を取得できません（アプリの動作ログをご確認ください）。"

    # --------------------------------------------------- ③④のデータ取得（P4）
    #
    # AppService 側の P4 API が古い版で欠けていても UI を起動できるよう、
    # すべて hasattr で防御し、無い機能は日本語で「未対応」と表示する。
    # UI 側で履歴を組み立て直すことはしない（表示ロジックは AppService に集約する）。

    _has_completed_videos = hasattr(service, "completed_videos")
    _has_history_rows = hasattr(service, "history_rows")
    _has_continuation = hasattr(service, "continuation_context")
    _has_concat = hasattr(service, "start_concat")
    _has_concat_status = hasattr(service, "concat_status")
    _has_reveal = hasattr(service, "reveal_in_finder")
    # P5.2: 指定順連結。サービス側が未対応でも③タブは今までどおり動く
    _has_custom_concat = hasattr(service, "start_custom_concat")
    _has_concat_candidates = hasattr(service, "concat_candidates")
    # P5.3-B: アプリ内ゴミ箱（未対応のサービスでも③は従来どおり使える）
    _has_trash = hasattr(service, "move_to_trash")

    def _submit_accepts_continuation() -> bool:
        """`submit_generation` が parent_id / keyframe_path を受けるか調べる。"""
        try:
            params = inspect.signature(service.submit_generation).parameters
        except (TypeError, ValueError):  # pragma: no cover - 実装依存
            return False
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return True
        return "parent_id" in params and "keyframe_path" in params

    def _servable(path, *, allow_tmp: bool = False) -> Path | None:
        """ブラウザへ渡してよいパスだけを通す（設計書 §15）。

        `data/outputs` と `data/concat` の成果物のみ。履歴JSON・ログ・data_root 外は
        たとえ履歴に書かれていても配信しない。実在しないものは None にする。
        """
        if path is None or path == "":
            return None
        bases = [cfg.outputs_dir, cfg.concat_dir]
        if allow_tmp:
            bases.append(cfg.tmp_dir)
        try:
            resolved = Path(path).resolve()
            for base in bases:
                if resolved.is_relative_to(Path(base).resolve()):
                    return resolved if resolved.is_file() else None
        except OSError:  # pragma: no cover - 実行環境依存
            return None
        return None

    def _completed_rows() -> list:
        """③完成動画タブの一覧（成功した個別動画 ＋ 連結動画。新しい順）。"""
        if not _has_completed_videos:
            return []
        return _sorted_newest_first(list(service.completed_videos()))

    def _concat_row_of(row):
        """個別行に対応する連結成果物の行（無ければ None）。

        `history_rows()` は個別動画だけを返すため、連結済みのレコードについては
        `find_row(job_id, "concat")` で連結行を取り直して④にも並べる。
        すでに連結行が来ている場合は二重に増やさない。
        """
        if _attr(row, "kind", "clip") != "clip":
            return None
        if not getattr(row, "concat_sources", None):
            return None
        finder = getattr(service, "find_row", None)
        if not callable(finder):
            return None
        try:
            concat_row = finder(_attr(row, "job_id", ""), "concat")
        except Exception:  # 連結行が取れなくても履歴一覧は出す
            log.exception("連結行を取得できませんでした: %s", _attr(row, "job_id", ""))
            return None
        # 連結動画のファイルが無ければ④にも出さない（P5.3-B: 実在が表示の正本）
        if concat_row is None or not _attr(concat_row, "exists", False):
            return None
        return concat_row

    def _concat_product_rows() -> list:
        """④の「連結成果物」フィルタ用（チェーン連結＋指定順連結・新しい順）。"""
        getter = getattr(service, "concat_product_rows", None)
        if not callable(getter):
            # 旧サービスでも④が壊れないよう、完成一覧から絞り込む経路を残す
            return [r for r in _completed_rows() if _attr(r, "kind", "clip") == "concat"]
        try:
            return _sorted_newest_first(list(getter()))
        except Exception:  # 設計書 §13.2
            log.exception("連結成果物の一覧を取得できませんでした")
            return []

    def _history_rows(status: str | None) -> list:
        """④履歴タブの一覧（全状態。フィルタは念のため UI 側でも適用する）。

        「連結成果物」だけは状態ではなく**種類**で絞るので、番兵を見て
        別経路（`concat_product_rows()`）へ分岐する（P5.3-A）。
        """
        if status == CONCAT_PRODUCTS_SENTINEL:
            return _concat_product_rows()
        if not _has_history_rows:
            return []
        rows: list = []
        for row in service.history_rows(status):
            if status and _status_value(row) != status:
                continue  # サービス側が絞っていない場合の保険
            rows.append(row)
            concat_row = _concat_row_of(row)
            if concat_row is not None:
                rows.append(concat_row)
        return _sorted_newest_first(rows)

    def _find_row(rows: list, key: str):
        for row in rows:
            if _row_key(row) == key:
                return row
        return None

    # --------------------------------------------------- ③④の描画（純粋関数）

    def _videos_summary(rows: list) -> str:
        """③完成・編集タブの要約（P5.3-A）。**一覧表の代わり**。

        以前はここに全動画の Markdown 表を出していたが、27件で3,000px を超え、
        下にある順番指定連結まで延々とスクロールする必要があった。表は④履歴タブへ
        一本化し、③は「何件あるか」だけを1行で示す。件数は選択候補と同じ行から
        数えるので、画面と候補が食い違わない。

        数えるのは**実際にファイルがある動画だけ**（P5.3-B）。Finder で消せば
        次の更新で件数から減り、正式パスへ戻せばまた増える。
        """
        if not _has_completed_videos:
            return f"### 完成した動画\n{_UNSUPPORTED}"
        if not rows:
            return (
                "**完成した動画: 0件**\n\n"
                "まだ完成した動画はありません。①新規生成タブから作ってください。"
            )
        clips = sum(1 for r in rows if _attr(r, "kind", "clip") == "clip")
        chains = sum(1 for r in rows if getattr(r, "concat_kind", None) == "chain")
        manuals = sum(1 for r in rows if getattr(r, "concat_kind", None) == "manual")

        parts = [f"個別{clips}件"]
        if chains:
            parts.append(f"チェーン連結{chains}件")
        if manuals:
            parts.append(f"指定順連結{manuals}件")
        return (
            f"**完成した動画: {len(rows)}件**（{'・'.join(parts)}）\n\n"
            "一覧は「履歴」タブで見られます。"
        )

    def _concat_products_table(rows: list) -> str:
        """④履歴タブの「連結成果物」フィルタ用の表（P5.3-A）。

        ジョブ用の列（step / seed / 状態遷移）は連結成果物に意味が無いので使わず、
        **連結成果物にとって意味のある列**だけを出す。③から一覧表を無くしたため、
        チェーン連結と指定順連結を表で見られる唯一の場所になる。
        """
        if not rows:
            return (
                "### 履歴（連結成果物・0件）\n"
                "まだ連結した動画はありません。"
                "「完成・編集」タブでつなげると、ここに出ます。"
            )
        lines = [
            f"### 履歴（連結成果物・{len(rows)}件・新しい順）",
            "",
            "| 種類 | ID | 作成日時 | 長さ | 本数 | 元の動画（順番どおり） | ファイル |",
            "|---|---|---|---:|---:|---|---|",
        ]
        for row in rows:
            sources = tuple(getattr(row, "concat_sources", ()) or ())
            if sources:
                # 順番が分かることが重要なので、多いときは前後を残して省略する
                if len(sources) > 4:
                    shown = f"{sources[0]} → … → {sources[-1]}"
                else:
                    shown = " → ".join(str(s) for s in sources)
            else:
                shown = "—"
            exists = _attr(row, "exists", False)
            lines.append(
                f"| {CONCAT_KIND_LABELS.get(getattr(row, 'concat_kind', None), '連結')} "
                f"| `{_cell(_attr(row, 'job_id', ''))}` "
                f"| {_cell(_fmt_dt(getattr(row, 'created_at', None)))} "
                f"| {_cell(_attr(row, 'duration_label', '—'))} "
                f"| {_cell(len(sources) if sources else '—')} "
                f"| {_cell(shown)} "
                f"| {'あり' if exists else '⚠️ 見つかりません'} |"
            )
        return "\n".join(lines)

    def _row_detail(row, *, with_status: bool) -> str:
        """選択行の詳細（③④共通の**主要部**）。

        backend / model / execution_engine などの内部の言い方は
        `_row_tech_detail()`（「詳しい情報」）へ分けている（P5 §6.4）。
        **ジョブIDとエラーの種類はここに残す**（調査できなくなるため）。
        """
        kind = _attr(row, "kind", "clip")
        lines = [
            f"### {KIND_LABELS.get(kind, kind)}動画 `{_attr(row, 'job_id', '')}`",
        ]
        if with_status:
            lines.append(f"状態: **{_status_label(row)}**")
        lines.append(
            f"作成日時: {_fmt_dt(getattr(row, 'created_at', None))} ／ "
            f"長さ: {_attr(row, 'duration_label', '—')}"
            + (
                f"（{getattr(row, 'num_frames', None)}フレーム）"
                if getattr(row, "num_frames", None)
                else ""
            )
            + f" ／ ステップ: {_attr(row, 'steps', '—')}"
        )
        seed_requested = getattr(row, "seed_requested", None)
        lines.append(
            f"seed: {_seed_cell(row)}"
            + (
                ""
                if not with_status
                else f"（指定: {seed_requested if seed_requested is not None else 'ランダム'}）"
            )
        )
        lines.append(
            f"親ID: {getattr(row, 'parent_id', None) or 'なし（ルート）'} ／ "
            f"チェーン長: {_attr(row, 'chain_length', '—')}"
        )
        lines.append(f"処理時間: {format_duration(getattr(row, 'elapsed_sec', None))}")
        sources = getattr(row, "concat_sources", None)
        if sources:
            chain = " → ".join(str(s) for s in sources)
            lines.append(
                f"連結元（{len(sources)}本）: {chain}"
                if kind == "concat"
                else f"連結済み（{len(sources)}本）: {chain}"
            )
        prompt_head = _attr(row, "prompt_head", "")
        if prompt_head:
            lines.append(f"プロンプト概要: {prompt_head}")
        error = getattr(row, "error", None)
        if error:
            category = getattr(row, "error_category", None)
            if category:
                lines.append(
                    f"エラーの種類: {ERROR_CATEGORY_LABELS.get(category, category)}"
                )
                advice = ERROR_CATEGORY_ADVICE.get(category)
                if advice:
                    lines.append(advice)
            lines.append("くわしい内容は下の「詳しい情報」に出しています。")
        if not _attr(row, "exists", False):
            lines.append(
                "⚠️ **ファイルが見つかりません**"
                "（削除・移動された可能性があります。再生はできません）"
            )
        return "\n\n".join(lines)

    def _row_tech_detail(row) -> str:
        """選択行の「詳しい情報」（技術的な値。Accordion の中だけに出す）。"""
        lines = [
            "困ったときにこの内容をそのままお伝えいただくと調査できます。",
            "",
            f"- ジョブID: `{_attr(row, 'job_id', '')}`",
            f"- 種別（kind）: {_attr(row, 'kind', 'clip')}",
            f"- 状態（status）: {_status_value(row) or '—'}",
            f"- backend: {_attr(row, 'backend_id', '—')} ／ "
            f"model: {_attr(row, 'model_revision', '—')}",
            f"- 実行方式（execution_engine）: {_attr(row, 'execution_engine', '—')}",
            f"- 画面サイズ: {FIXED_WIDTH}×{FIXED_HEIGHT} ／ {FIXED_FPS}fps ／ "
            f"{_attr(row, 'num_frames', '—')}フレーム ／ "
            f"{_attr(row, 'steps', '—')}ステップ",
        ]
        path = getattr(row, "video_path", None)
        if path:
            lines.append(f"- ファイル名: `{Path(path).name}`")
        error = getattr(row, "error", None)
        if error:
            category = getattr(row, "error_category", None)
            if category:
                lines.append(f"- エラー分類（error_category）: {category}")
            lines.append(f"- エラー内容: {error}")
        return "\n".join(lines)

    def _select_choices(rows: list):
        """Dropdown の choices だけを更新する（value は送らず選択を保持する）。"""
        choices = []
        for row in rows:
            kind = _attr(row, "kind", "clip")
            label = (
                f"[{KIND_LABELS.get(kind, kind)}] {_attr(row, 'job_id', '')}"
                f" ｜ {_fmt_dt(getattr(row, 'created_at', None))}"
                f" ｜ {_attr(row, 'duration_label', '—')}"
            )
            if not _attr(row, "exists", False):
                label += " ｜ ファイル欠損"
            choices.append((label, _row_key(row)))
        return gr.update(choices=choices)

    def _concat_status_text() -> str:
        """連結の進行状態（解決中・連結中・検証中・完成・失敗）を日本語で出す。"""
        if not _has_concat_status:
            return f"### 連結の状態\n{_UNSUPPORTED}"
        status = service.concat_status()
        if status is None:
            return "### 連結の状態\n⚠️ 連結機能を利用できません（ffmpeg をご確認ください）。"
        state = str(getattr(status, "state", "") or "idle").lower()
        label = getattr(status, "state_label", None) or CONCAT_STATE_LABELS.get(
            state, state or "不明"
        )
        mark = {"done": " ✅", "failed": " ❌"}.get(state, "")
        lines = [f"### 連結の状態\n**{label}{mark}**"]
        clips = getattr(status, "clips", 0)
        if clips:
            lines.append(f"対象: {clips}本（ルートから選択した動画まで）")
        message = getattr(status, "message", None)
        if message:
            lines.append(str(message))
        for warning in getattr(status, "warnings", ()) or ():
            lines.append(f"⚠️ {warning}")
        output_path = getattr(status, "output_path", None)
        if output_path:
            lines.append(f"出力: `{Path(output_path).name}`")
        return "\n\n".join(lines)

    # ------------------------------------------------------ ③④のビュー生成

    def _videos_view() -> tuple:
        """③タブの Timer 表示。スナップショットは1 tick に**1回だけ**取得する。

        一覧と連結状態は別々に例外を握る（片方が壊れてももう片方は出し続ける）。

        **戻り値は4つ**（P5.2 で末尾に「指定順連結の候補」を追加した）。
        既存の3つの順番は変えていない。候補は `choices` だけを更新するので、
        ユーザーが選びかけている値も、編集中の並びも Timer では壊れない。

        **P5.3-A で先頭の意味だけを「一覧表」→「短い要約」へ変えた**
        （出力の数・順序・残り3つの意味は不変。設計書 §24.4）。
        """
        try:
            rows = _completed_rows()
            listing, choices = _videos_summary(rows), _select_choices(rows)
        except Exception:  # 設計書 §13.2: UI の更新を永久に止めない
            log.exception("完成動画の一覧を取得できませんでした")
            listing = "⚠️ 完成した動画の件数を取得できません（「詳しい情報（アプリの動作ログ）」をご確認ください）"
            choices = gr.update()
        try:
            concat = _concat_status_text()
        except Exception:  # 設計書 §13.2
            log.exception("連結の状態を取得できませんでした")
            concat = "### 連結の状態\n⚠️ 状態を取得できません（「詳しい情報（アプリの動作ログ）」をご確認ください）"
        try:
            clip_choices = _clip_choices()
        except Exception:  # 設計書 §13.2
            log.exception("指定順連結の候補を取得できませんでした")
            clip_choices = gr.update()
        return listing, choices, concat, clip_choices

    def _history_view(filter_label) -> tuple:
        """④タブの Timer 表示。スナップショットは1 tick に**1回だけ**取得する。"""
        try:
            status = HISTORY_FILTERS.get(filter_label)
            rows = _history_rows(status)
            return (_history_table(rows, filter_label), _select_choices(rows))
        except Exception:  # 設計書 §13.2
            log.exception("履歴の一覧を取得できませんでした")
            return (
                "### 履歴\n⚠️ 一覧を取得できません（「詳しい情報（アプリの動作ログ）」をご確認ください）",
                gr.update(),
            )

    def _history_table(rows: list, filter_label) -> str:
        label = filter_label if filter_label in HISTORY_FILTERS else "すべて"
        if label == CONCAT_PRODUCTS_FILTER:
            # 連結成果物には step / seed / 状態遷移が無いので、専用の列で出す
            return _concat_products_table(rows)
        if not _has_history_rows:
            return f"### 履歴\n{_UNSUPPORTED}"
        if not rows:
            return f"### 履歴（{label}・0件）\n該当する記録はありません。"
        lines = [
            f"### 履歴（{label}・{len(rows)}件・新しい順）",
            "",
            "| 状態 | 種別 | 日時 | ID | 親ID | 長さ | step | seed指定 | seed実際 "
            "| backend・model | 処理時間 | 実行方式 | エラー |",
            "|---|---|---|---|---|---:|---:|---|---|---|---|---|---|",
        ]
        abnormal = False
        for row in rows:
            status_value = _status_value(row)
            if status_value in ("queued", "running"):
                abnormal = True
            kind = _attr(row, "kind", "clip")
            seed_requested = getattr(row, "seed_requested", None)
            error = getattr(row, "error", None)
            category = getattr(row, "error_category", None)
            error_cell = "—"
            if error:
                error_cell = (
                    f"{ERROR_CATEGORY_LABELS.get(category, category)}: {error}"
                    if category
                    else error
                )
            seed_used = getattr(row, "seed_used", None)  # 0 も有効な値なので None 判定
            mark = " ⚠️" if status_value in ("queued", "running") else ""
            lines.append(
                f"| {_status_label(row)}{mark} | {KIND_LABELS.get(kind, kind)} "
                f"| {_cell(_fmt_dt(getattr(row, 'created_at', None)))} "
                f"| `{_cell(_attr(row, 'job_id', ''))}` "
                f"| {_cell(getattr(row, 'parent_id', None) or '—')} "
                f"| {_cell(_attr(row, 'duration_label', '—'))} "
                f"| {_cell(_attr(row, 'steps', '—'))} "
                f"| {_cell(seed_requested if seed_requested is not None else 'ランダム')} "
                f"| {_cell(seed_used if seed_used is not None else '—')} "
                f"| {_cell(_attr(row, 'backend_id', '—'))}・"
                f"{_cell(_attr(row, 'model_revision', '—'))} "
                f"| {_cell(format_duration(getattr(row, 'elapsed_sec', None)))} "
                f"| {_cell(_attr(row, 'execution_engine', '—'))} "
                f"| {_cell(error_cell)} |"
            )
        if abnormal:
            lines.append("")
            lines.append(
                "⚠️ 「生成待ち」「実行中」のまま残っている記録があります"
                "（前回のアプリ終了が正常に完了しなかった可能性のある異常データです）。"
            )
        return "\n".join(lines)

    def _preview_of(
        rows: list, key: str, *, with_status: bool, empty_message: str
    ) -> tuple:
        """選択キーから（動画パス・詳細・「詳しい情報」）の3つを作る（P5 §6.4）。

        未選択のときの案内文は③と④で異なる（選択欄の位置もボタン名も違う）ため、
        呼び出し側から `empty_message` を受け取る。
        """
        if not key:
            return (
                None,
                empty_message,
                "動画を選ぶと、ここに技術的な情報が出ます。",
            )
        row = _find_row(rows, key)
        if row is None:
            return (
                None,
                f"⚠️ 選択された記録が見つかりません: `{key}`",
                f"- 選択キー: `{key}`（一覧に該当なし）",
            )
        detail = _row_detail(row, with_status=with_status)
        tech = _row_tech_detail(row)
        if not _attr(row, "exists", False):
            return None, detail, tech
        video = _servable(getattr(row, "video_path", None))
        if video is None:
            return (
                None,
                detail + "\n\nⓘ この動画は再生できません（安全のため配信していない場所にあります）。",
                tech,
            )
        return str(video), detail, tech

    # ---------------------------------------------------------------- callbacks

    def _do_submit(prompt, length_label, step_label, seed_random, seed_value, parent_id):
        """①の投入処理本体（単発／継続の両方。キューへ登録して即座に戻る）。"""
        num_frames = LENGTH_CHOICES.get(length_label, cfg.default_num_frames)
        steps = STEP_CHOICES.get(step_label, cfg.default_steps)

        seed_requested: int | None = None
        if not seed_random:
            if seed_value is None or str(seed_value).strip() == "":
                header, progress = _snapshot_texts()
                return (
                    "❌ シード値を入力するか、「ランダム」にチェックを入れてください",
                    header,
                    progress,
                )
            try:
                seed_requested = int(seed_value)
            except (TypeError, ValueError):
                header, progress = _snapshot_texts()
                return "❌ シード値は整数で入力してください", header, progress

        parent = str(parent_id or "").strip()
        extra: dict = {}
        try:
            if parent:
                # キーフレームのパスはブラウザから受け取らず、必ずここで取り直す。
                if not _has_continuation:
                    raise ValidationError(
                        "この版では継続生成に対応していません（アプリの更新が必要です）"
                    )
                if not _submit_accepts_continuation():
                    raise ValidationError(
                        "この版では継続生成をキューへ登録できません（アプリの更新が必要です）"
                    )
                ctx = service.continuation_context(parent)
                extra = {
                    "parent_id": getattr(ctx, "parent_id", parent),
                    "keyframe_path": getattr(ctx, "keyframe_path", None),
                }
            submit_kwargs = dict(
                prompt=prompt or "",
                num_frames=num_frames,
                steps=steps,
                seed_requested=seed_requested,
                **extra,
            )
            # 二重投入の冪等化つきの投入口があればそちらを使う（P5 §6.2）。
            # 古い AppService でも動くよう hasattr で防御する。
            if hasattr(service, "submit_generation_ex"):
                result = service.submit_generation_ex(**submit_kwargs)
                view = getattr(result, "view", result)
                duplicate = bool(getattr(result, "duplicate", False))
            else:
                view = service.submit_generation(**submit_kwargs)
                duplicate = False
            kind = f"継続生成（親: {parent}）" if parent else "新規生成"
            if duplicate:
                message = (
                    f"ℹ️ 同じ内容がすでに登録されています: **{view.job_id}**\n\n"
                    "続けて2回押されたため、**1件だけ**登録しました。"
                    "同じ内容をもう1本作りたいときは、数秒おいてからもう一度押してください。"
                )
            else:
                message = (
                    f"✅ キューに追加しました: **{view.job_id}**"
                    f"（{kind}・{view.duration_label}・{steps}ステップ・シード "
                    f"{'ランダム' if seed_requested is None else seed_requested}）\n\n"
                    f"目安時間: {service.estimate_text(num_frames, steps)}"
                    "（進捗は下の「現在の処理」に表示されます）"
                )
        except (ValidationError, QueueFullError) as e:
            message = f"❌ {e}"
        except Exception:  # UI を落とさない（設計書 §13.2）
            # 例外文をそのまま画面へ出さない（P5 §6.4）。原因はログに残す
            log.exception("ジョブ投入で内部エラーが発生しました")
            message = (
                "❌ 予期しない問題が起きたため、登録できませんでした。"
                "もう一度お試しください"
                "（詳しい原因は下の「詳しい情報（アプリの動作ログ）」に記録しています）。"
            )

        header, progress = _snapshot_texts()
        return message, header, progress

    def on_submit(prompt, length_label, step_label, seed_random, seed_value):
        """P1 互換の投入 API（`/on_submit`。引数・戻り値の数と順序を変えない）。"""
        return _do_submit(
            prompt, length_label, step_label, seed_random, seed_value, ""
        )

    def on_submit_v2(
        prompt, length_label, step_label, seed_random, seed_value, parent_id
    ):
        """継続対応の投入 API（P4。`parent_id` が空なら単発生成と同じ挙動）。"""
        return _do_submit(
            prompt, length_label, step_label, seed_random, seed_value, parent_id
        )

    def on_tick():
        """Timer から毎秒呼ばれる。例外で更新が止まらないよう必ず値を返す。"""
        try:
            header, progress = _snapshot_texts()
            return header, progress, recent_logs(100), _latest_id()
        except Exception:  # 設計書 §13.2: サーバは落とさない
            log.exception("状態の取得に失敗しました")
            return (
                "⚠️ 状態を取得できません（「詳しい情報（アプリの動作ログ）」をご確認ください）",
                "状態を取得できません。",
                recent_logs(100),
                "",
            )

    def on_latest_changed(latest_id):
        try:
            latest = service.latest_completed()
            if not latest_id or latest is None:
                return None, "まだ完成した動画はありません。"
            info = (
                f"**{latest.job_id}**\n\n"
                f"シード {latest.seed_used if latest.seed_used is not None else '不明'} ／ "
                f"処理時間 {format_duration(latest.elapsed_sec)}\n\n"
                f"{latest.prompt_head}"
            )
            return str(latest.video_path), info
        except Exception:  # 設計書 §13.2
            log.exception("完成動画の取得に失敗しました")
            return None, "⚠️ 完成動画を読み込めませんでした（「詳しい情報（アプリの動作ログ）」をご確認ください）。"

    def on_estimate_change(length_label, step_label):
        """目安時間の表示（P3 実測値。実測が無い組み合わせは「推定」と明示する）。"""
        num_frames = LENGTH_CHOICES.get(length_label, cfg.default_num_frames)
        steps = STEP_CHOICES.get(step_label, cfg.default_steps)
        source = (
            "実機で計測した値です"
            if (num_frames, steps) in MEASURED_COMBINATIONS
            else "この組み合わせだけは計測しておらず、**推定値**です"
        )
        return (
            f"解像度 {FIXED_WIDTH}×{FIXED_HEIGHT}・{FIXED_FPS}fps 固定 ｜ "
            f"この設定の目安時間: **{service.estimate_text(num_frames, steps)}**"
            f"（{source}）"
        )

    def on_insert_hint(prompt):
        base = (prompt or "").rstrip()
        return (base + "\n" + DIALOGUE_HINT).strip() if base else DIALOGUE_HINT

    def on_clear_prompt():
        """プロンプト欄だけを空にする（P5.1）。

        **引数を取らず、戻り値も空文字1つだけ**にしてある。長さ・ステップ・
        シード・継続モードはこの関数から見えないので、構造的に書き換えようがない。
        投入済みのジョブ・履歴・完成動画にも触れない（サービス層を呼ばない）。
        空欄で押しても空文字を返すだけで、例外にはならない。
        """
        return ""

    # ---------------------------------------------------- ②キュータブの callbacks

    def on_queue_tick():
        """②タブ用の Timer。例外を投げず必ず6値を返す（更新の永久停止を防ぐ）。"""
        return _queue_view()

    def on_cancel_queued(job_id):
        """待機中ジョブの取消（設計書 §9.1 決定D14a。実行中は取り消せない）。"""
        target = str(job_id or "").strip()
        if not target:
            message = "⚠️ 取り消す待機ジョブを選んでください。"
        else:
            try:
                canceled = service.queue.cancel_queued(target)
                if canceled:
                    message = f"✅ 待機中のジョブを取り消しました: **{target}**"
                else:
                    message = (
                        f"⚠️ 取り消せませんでした: **{target}**"
                        "（すでに生成が始まっている・終了済み・存在しないジョブIDです。"
                        "実行中のジョブを止めたい場合は［⚠ ワーカーを再起動する］をお使いください）"
                    )
            except Exception:  # 設計書 §13.2: サーバは落とさない
                log.exception("待機ジョブの取消に失敗しました: %s", target)
                message = (
                    f"❌ 取り消せませんでした: **{target}**。もう一度お試しください"
                    f"{_LOG_HINT}"
                )
        return (message, *_queue_view())

    def on_restart_worker(acknowledged):
        """ワーカーの手動再起動（設計書 §13.3。唯一の「詰まり解消」手段）。"""
        if not _restart_available:
            message = (
                "⚠️ この版ではワーカーの再起動に対応していません"
                "（アプリ自体を再起動してください）。"
            )
        elif not acknowledged:
            message = (
                "⚠️ 先に「実行中のジョブが失敗になることを理解しました」に"
                "チェックを入れてください。"
            )
        else:
            try:
                result = service.restart_worker()
                if result is False:
                    message = (
                        "⚠️ いま再起動できませんでした"
                        "（終了処理中の可能性があります）。"
                    )
                else:
                    message = (
                        "✅ ワーカーの再起動を開始しました。"
                        "実行中だったジョブは失敗として記録されます。"
                        "モデルと Turbo LoRA の読み直しに数分かかることがあります。"
                    )
            except Exception:  # 設計書 §13.2: サーバは落とさない
                log.exception("ワーカーの再起動に失敗しました")
                message = (
                    "❌ 再起動できませんでした。もう一度お試しください"
                    f"{_LOG_HINT}"
                )
        return (message, *_queue_view())

    # ------------------------------------------- ③完成動画タブの callbacks（P4）

    def on_videos_tick():
        """③タブ用の Timer。**プレーヤーには触れない**（再生の中断を防ぐ）。"""
        return _videos_view()

    def on_select_video(key):
        """選択された完成動画のプレビュー・詳細・「詳しい情報」（3値。P5 §6.4）。"""
        try:
            return _preview_of(
                _completed_rows(),
                str(key or "").strip(),
                with_status=False,
                empty_message=VIDEOS_EMPTY_NOTE,
            )
        except Exception:  # 設計書 §13.2
            log.exception("完成動画の詳細を取得できませんでした")
            return (
                None,
                "⚠️ 動画を読み込めませんでした（下の「詳しい情報」とアプリの動作ログをご確認ください）。",
                "- 動画の詳細取得でエラーが発生しました（アプリの動作ログを参照）",
            )

    def _concat_message(key) -> str:
        """[ルートからここまでを連結]。開始はバックグラウンドで、ここでは待たない。

        AppService は開始できた場合に内部キー（英数字）を返すことがあるため、
        日本語（非ASCII）を含む場合だけそのまま表示し、それ以外は UI 側で文言を作る。
        解決に失敗するケース（2本未満・欠損・非互換）は開始直後に確定するので、
        ごく短時間（`CONCAT_IMMEDIATE_WAIT_SEC`）だけ結果を見てから返す。
        """
        _kind, job_id = _split_key(key)
        if not job_id:
            return "⚠️ 連結する動画を一覧から選んでください。"
        if not _has_concat:
            return _UNSUPPORTED
        previous_key = _concat_key()
        try:
            result = str(service.start_concat(job_id) or "")
        except (ValidationError, HistoryError) as e:
            return f"❌ 連結できません: {e}"
        except Exception:  # 設計書 §13.2
            log.exception("連結の開始に失敗しました: %s", job_id)
            return f"❌ 連結を開始できませんでした（対象: {job_id}）{_LOG_HINT}"

        failure = _immediate_concat_failure(previous_key)
        if failure:
            return f"❌ {failure}"
        if any(ord(ch) > 127 for ch in result):
            return result  # AppService が日本語メッセージを返した場合はそのまま出す
        return (
            f"▶ ルートから **{job_id}** までの連結を開始しました。"
            "進行状況は下の「連結の状態」に表示されます"
            "（完了までこの画面を閉じても処理は続きます）。"
        )

    def _concat_key():
        """現在の連結実行キー（前回の結果と今回の実行を見分けるために使う）。"""
        if not _has_concat_status:
            return None
        try:
            return getattr(service.concat_status(), "key", None)
        except Exception:  # 設計書 §13.2
            log.exception("連結状態の取得に失敗しました")
            return None

    def _immediate_concat_failure(previous_key) -> str | None:
        """開始直後に失敗が確定したかだけを短時間確認する（UI をブロックしない）。

        チェーン解決の失敗（2本未満・欠損・非互換）はミリ秒で確定するので、
        ここで拾えるとボタンの手応えが正しくなる。**前回の失敗を今回の結果と
        取り違えない**よう、実行キーが変わった場合だけ判定する。
        """
        if not _has_concat_status:
            return None
        deadline = time.monotonic() + CONCAT_IMMEDIATE_WAIT_SEC
        while time.monotonic() < deadline:
            try:
                status = service.concat_status()
            except Exception:  # 設計書 §13.2
                log.exception("連結状態の取得に失敗しました")
                return None
            if getattr(status, "key", None) == previous_key:
                return None  # 新しい連結が始まっていない（状態は前回のまま）
            state = str(getattr(status, "state", "") or "").lower()
            if state == "failed":
                return str(
                    getattr(status, "error", None)
                    or getattr(status, "message", "")
                    or "連結に失敗しました"
                )
            if state not in ("resolving", ""):
                return None  # 連結が動き出した（この先は Timer が状態を出す）
            time.sleep(0.02)
        return None

    def on_start_concat(key):
        return _concat_message(key), _concat_status_text()

    # ------------------------------------- ③アプリ内ゴミ箱（P5.3-B・設計書 §25）
    #
    # 「消す」は **`data/trash/` へ移すだけ**。削除台帳も復元UIも依存関係の検査も
    # 持たない。画面から消えるのは「ファイルが無くなったから」であって、
    # どこかに消したと記録するからではない。

    def _trash_controls(key) -> tuple:
        """選択に応じて整理セクションの出し入れを決める（3値）。

        **実在する動画を選んだときだけ**出す。記録だけの動画は一覧にも
        候補にも出ないので、ここへ来ることも基本的に無い。
        """
        if not _has_trash:
            return gr.update(visible=False), gr.update(visible=False), gr.update(value=False)
        row = _find_row(_completed_rows(), str(key or "").strip())
        if row is None or not _attr(row, "exists", False):
            return gr.update(visible=False), gr.update(visible=False), gr.update(value=False)
        job_id = _attr(row, "job_id", "")
        return (
            gr.update(visible=True),
            gr.update(visible=True, value=f"🗑 ゴミ箱へ移動: {job_id}"),
            gr.update(value=False),  # 選び直したら確認は必ず外す
        )

    def on_selection_changed_for_trash(key):
        try:
            return _trash_controls(key)
        except Exception:  # 設計書 §13.2
            log.exception("整理セクションの表示を更新できませんでした")
            return gr.update(visible=False), gr.update(visible=False), gr.update(value=False)

    def on_move_to_trash(key, confirmed):
        """[ゴミ箱へ移動]。送るのは選択キーだけで、パスはサーバ側で解決する。

        成功したら、その場で選択・プレビュー・確認チェックを片づけ、件数と
        候補も更新する（別セッションは次の Timer 更新で消える）。
        """
        kind, job_id = _split_key(key)
        unchanged = (
            gr.update(),  # 選択欄
            gr.update(),  # プレビュー
            gr.update(),  # メタ
            gr.update(),  # 詳しい情報
            gr.update(),  # 要約
            gr.update(),  # 追加候補
            gr.update(),  # 整理セクション
            gr.update(),  # ゴミ箱ボタン
            gr.update(),  # 確認チェック
        )
        if not _has_trash:
            return (*unchanged, f"⚠️ {_UNSUPPORTED}")
        if not job_id:
            return (*unchanged, "⚠️ 整理する動画を選んでください。")
        if not confirmed:
            return (
                *unchanged,
                "⚠️ 先に「ゴミ箱へ移動することを確認しました」にチェックを入れてください。",
            )

        try:
            ok, message = service.move_to_trash(job_id, kind)
        except Exception:  # 設計書 §13.2
            log.exception("ゴミ箱への移動に失敗しました: %s", key)
            return (*unchanged, f"❌ ゴミ箱へ移動できませんでした{_LOG_HINT}")

        if not ok:
            # 失敗時は表示を変えない（消えたように見せない）
            return (*unchanged, f"❌ {message}")

        rows = _completed_rows()
        return (
            gr.update(value=None),                       # 選択解除
            None,                                        # プレビューを消す
            VIDEOS_EMPTY_NOTE,                           # メタを初期表示へ
            "動画を選ぶと、ここに技術的な情報が出ます。",      # 「詳しい情報」も初期化
            _videos_summary(rows),                       # 件数を更新
            _clip_choices(),                             # 連結の追加候補を更新
            gr.update(visible=False),                    # 整理セクションを隠す
            gr.update(visible=False),                    # ゴミ箱ボタンを隠す
            gr.update(value=False),                      # 確認チェックを戻す
            message,
        )

    # ------------------------------------------- ③指定順連結（P5.2・設計書 §23）
    #
    # 連結候補の並びは `gr.State(list[str])` に持つ＝**ブラウザセッションごと**。
    # Mac と iPhone で同時に開いても互いの選択は混ざらない（実行だけはサーバ側の
    # ConcatService が全体で1本に排他する）。並びを変える関数はすべて
    # 「今の並び＋対象 → 新しい並び」の純粋関数にしてあり、画面は毎回そこから
    # 組み立て直す。**Timer の outputs にこれらを一切入れない**ので、
    # 1秒ごとの更新でユーザーが編集中の順番が壊れることがない。

    def _clip_rows() -> list:
        """指定順連結の素材にできる行（成功した個別動画のみ）。"""
        if not _has_concat_candidates:
            return [r for r in _completed_rows() if _attr(r, "kind", "clip") == "clip"]
        try:
            return list(service.concat_candidates())
        except Exception:  # 設計書 §13.2
            log.exception("連結候補の取得に失敗しました")
            return []

    def _clip_choices():
        """候補ドロップダウンの選択肢（値は job_id そのもの）。"""
        choices = []
        for row in _clip_rows():
            job_id = _attr(row, "job_id", "")
            label = (
                f"{job_id} ｜ {_fmt_dt(getattr(row, 'created_at', None))}"
                f" ｜ {_attr(row, 'duration_label', '—')}"
            )
            if not _attr(row, "exists", False):
                label += " ｜ ファイルなし"
            choices.append((label, job_id))
        return gr.update(choices=choices)

    def _order_choices(order: list[str]):
        """「対象を選ぶ」の選択肢。**現在の並び順のまま**番号を振る。"""
        rows = {_attr(r, "job_id", ""): r for r in _clip_rows()}
        choices = []
        for i, job_id in enumerate(order, start=1):
            row = rows.get(job_id)
            length = _attr(row, "duration_label", "—") if row else "—"
            choices.append((f"{i}. {job_id} ｜ {length}", job_id))
        return gr.update(choices=choices, value=None)

    def _order_text(order: list[str]) -> str:
        """現在の連結順（番号付きの本体だけ）。

        **合計はここに入れない**（P5.3-A）。この文章は局所縦スクロールの器
        `.h3-vscroll` の中に置くので、合計まで一緒に入れると20本選んだときに
        スクロールしないと合計が読めなくなる。合計は `_order_total_text()` が
        器の外へ出す。
        """
        if not order:
            return (
                "まだ選ばれていません。上の欄で動画を選んで"
                "［連結候補へ追加］を押してください（2本以上で連結できます）。"
            )
        rows = {_attr(r, "job_id", ""): r for r in _clip_rows()}
        lines = []
        for i, job_id in enumerate(order, start=1):
            row = rows.get(job_id)
            if row is None:
                lines.append(f"{i}. `{job_id}` ⚠️ 一覧に見つかりません")
                continue
            missing = "" if _attr(row, "exists", False) else " ⚠️ ファイルなし"
            lines.append(
                f"{i}. `{job_id}` ｜ {_attr(row, 'duration_label', '—')}"
                f" ｜ {_fmt_dt(getattr(row, 'created_at', None))}{missing}"
            )
        return "\n".join(lines)

    def _order_total_text(order: list[str]) -> str:
        """合計本数と合計時間（**スクロール領域の外**に出す・P5.3-A）。"""
        if not order:
            return f"**現在の連結順: 0本**（{MIN_CUSTOM_CLIPS}本以上で連結できます）"
        rows = {_attr(r, "job_id", ""): r for r in _clip_rows()}
        total_frames = sum(
            int(_attr(rows.get(j), "num_frames", 0) or 0) for j in order if j in rows
        )
        text = f"**現在の連結順: {len(order)}本**"
        if total_frames:
            text += f" ｜ 合計 約{total_frames / FIXED_FPS:.1f}秒"
        if len(order) < MIN_CUSTOM_CLIPS:
            text += f"（あと{MIN_CUSTOM_CLIPS - len(order)}本で連結できます）"
        return text

    def _concat_button_label(order: list[str]) -> str:
        if len(order) < MIN_CUSTOM_CLIPS:
            return CUSTOM_CONCAT_LABEL
        rows = {_attr(r, "job_id", ""): r for r in _clip_rows()}
        frames = sum(
            int(_attr(rows.get(j), "num_frames", 0) or 0) for j in order if j in rows
        )
        seconds = frames / FIXED_FPS if frames else 0
        return f"▶ この順番で連結（{len(order)}本・約{seconds:.1f}秒）"

    def _custom_view(order: list[str], message: str = "") -> tuple:
        """並びから画面を組み立て直す（全ハンドラ共通の戻り値）。

        P5.3-A で合計表示（`custom_total_md`）を**末尾に追加**した。
        既存の5つの順番と意味は変えていない。
        """
        return (
            list(order),
            _order_text(order),
            _order_choices(order),
            gr.update(value=_concat_button_label(order)),
            message,
            _order_total_text(order),
        )

    def on_custom_add(order, job_id):
        known = {_attr(r, "job_id", "") for r in _clip_rows()}
        return _custom_view(*custom_order_add(order, job_id, known))

    def on_custom_up(order, job_id):
        return _custom_view(*custom_order_move(order, job_id, -1))

    def on_custom_down(order, job_id):
        return _custom_view(*custom_order_move(order, job_id, 1))

    def on_custom_remove(order, job_id):
        return _custom_view(*custom_order_remove(order, job_id))

    def on_custom_clear(order):
        return _custom_view(*custom_order_clear(order))

    def on_custom_start(order):
        """指定順の連結を開始する（送るのはジョブIDの並びだけ）。"""
        order = [str(v) for v in (order or [])]
        if len(order) < MIN_CUSTOM_CLIPS:
            return (
                *_custom_view(
                    order,
                    f"⚠️ 連結するには {MIN_CUSTOM_CLIPS} 本以上を選んでください"
                    f"（いまは {len(order)} 本）。",
                ),
                _concat_status_text(),
            )
        if not _has_custom_concat:
            return (*_custom_view(order, f"⚠️ {_UNSUPPORTED}"), _concat_status_text())

        previous_key = _concat_key()
        try:
            result = service.start_custom_concat(order)
        except Exception:  # 設計書 §13.2
            log.exception("指定順の連結を開始できませんでした: %s", order)
            return (
                *_custom_view(order, f"❌ 連結を開始できませんでした{_LOG_HINT}"),
                _concat_status_text(),
            )

        failure = _immediate_concat_failure(previous_key)
        if failure:
            return (*_custom_view(order, f"❌ {failure}"), _concat_status_text())
        message = (
            result
            if any(ord(ch) > 127 for ch in str(result))
            else f"▶ {len(order)}本の連結を開始しました。"
        )
        return (
            *_custom_view(order, f"▶ {message}"),
            _concat_status_text(),
        )

    def _reveal_target(kind: str, job_id: str):
        """Finder へ渡す対象を**サーバ側で解決**して返す（見つからなければ None）。

        clip / concat のどちらも一覧から引いて `_servable()` を通す。
        こうしないと (a) 連結ファイルが欠損したとき個別動画が黙って開く
        (b) ブラウザから `data_root` 内の履歴JSONやログのパスを送られて開ける、
        という2つの穴が残る。
        """
        row = _find_row(_completed_rows(), f"{kind}:{job_id}")
        if row is None:
            return None
        return _servable(getattr(row, "video_path", None))

    def _reveal_message(key) -> str:
        """[Finderで表示]。実際の subprocess 呼び出しは AppService 側が行う。"""
        kind, job_id = _split_key(key)
        if not job_id:
            return "⚠️ Finder で表示する動画を一覧から選んでください。"
        if not _has_reveal:
            return _UNSUPPORTED
        try:
            target = _reveal_target(kind, job_id)
            if target is None:
                return "⚠️ この動画のファイルが見つかりません（削除・移動された可能性があります）。"
            return str(service.reveal_in_finder(target, kind))
        except (ValidationError, HistoryError) as e:
            return f"❌ Finder で表示できません: {e}"
        except Exception:  # 設計書 §13.2
            log.exception("Finder 表示に失敗しました: %s", job_id)
            return f"❌ Finder で表示できませんでした（対象: {job_id}）{_LOG_HINT}"

    def on_reveal_video(key):
        return _reveal_message(key)

    # ------------------------------------------------- 継続生成（P4・§5.2・§10.5）

    def _continuation_banner_md(ctx, parent_id: str) -> str:
        duration = _attr(ctx, "duration_label", "—")
        steps = _attr(ctx, "steps", "—")
        seed_used = getattr(ctx, "seed_used", None)
        seed_text = (
            f"{seed_used}（親と同じ値を引き継ぎます）"
            if seed_used is not None
            else "親のシードが記録されていないため、ランダムのままです"
        )
        return "\n\n".join(
            [
                "### ▶ 続きを作成中",
                f"継続元（親動画）: **{parent_id}**　／　{duration}・{steps}ステップ",
                "この生成は左の**先頭フレーム画像**から始まります。",
                "**キャラクターと声の説明は変えず、セリフと動きだけ書き換えると安定します。**",
                f"継承シード: {seed_text}",
                "声と見た目は seed と先頭フレームから**近似的に引き継ぎます**"
                "（前の動画と同じになるとは限りません）。",
            ]
        )

    def _prefill_prompt(ctx) -> str:
        """親プロンプトの先頭に定型文を1行付ける（設計書 §10.5。他は書き換えない）。"""
        prefill = str(_attr(ctx, "prompt_prefill", "") or "").strip()
        if not prefill:
            prefill = str(_attr(ctx, "prompt", "") or "").strip()
        if not prefill.startswith(CONTINUATION_PREFIX):
            prefill = (CONTINUATION_PREFIX + "\n" + prefill).strip()
        return prefill

    def _no_continuation_update(message: str) -> tuple:
        """継続モードへ入らないときの戻り値（バナー・入力欄には触れない）。"""
        return (
            gr.update(),  # tabs
            gr.update(),  # banner group
            gr.update(),  # thumbnail
            gr.update(),  # banner text
            gr.update(),  # parent id (hidden)
            gr.update(),  # prompt
            gr.update(),  # length
            gr.update(),  # steps
            gr.update(),  # seed random
            gr.update(),  # seed value
            message,
        )

    def _start_continuation(key) -> tuple:
        _kind, parent_id = _split_key(key)
        if not parent_id:
            return _no_continuation_update("⚠️ 続きを作る動画を一覧から選んでください。")
        if _kind == "concat":
            return _no_continuation_update(
                "⚠️ 連結動画の続きは作れません"
                "（続きを作れるのは個別の生成動画だけです）。"
            )
        if not _has_continuation:
            return _no_continuation_update(_UNSUPPORTED)
        try:
            ctx = service.continuation_context(parent_id)
        except (ValidationError, HistoryError) as e:
            return _no_continuation_update(f"❌ この動画の続きは作れません: {e}")
        except Exception:  # 設計書 §13.2
            log.exception("継続元の取得に失敗しました: %s", parent_id)
            return _no_continuation_update(
                f"❌ 続きのもとになる動画を読み込めませんでした（対象: {parent_id}）{_LOG_HINT}"
            )

        thumbnail = _servable(
            getattr(ctx, "thumbnail", None) or getattr(ctx, "keyframe_path", None),
            allow_tmp=True,
        )
        seed_used = getattr(ctx, "seed_used", None)
        num_frames = _attr(ctx, "num_frames", cfg.default_num_frames)
        steps = _attr(ctx, "steps", cfg.default_steps)
        return (
            gr.Tabs(selected="tab_new"),
            gr.update(visible=True),
            (str(thumbnail) if thumbnail is not None else None),
            _continuation_banner_md(ctx, str(_attr(ctx, "parent_id", parent_id))),
            str(_attr(ctx, "parent_id", parent_id)),
            _prefill_prompt(ctx),
            gr.update(value=_length_label(num_frames)),
            gr.update(value=_step_label(steps)),
            gr.update(value=seed_used is None),
            gr.update(value=seed_used if seed_used is not None else 42),
            f"▶ 継続モードにしました（親: **{parent_id}**）。①新規生成タブをご覧ください。",
        )

    def on_start_continuation(key):
        """③完成動画タブの［この動画の続きを作る］。"""
        return _start_continuation(key)

    def on_history_continuation(key):
        """④履歴タブの［この動画の続きを作る］（③と同じ処理）。"""
        return _start_continuation(key)

    def on_clear_continuation():
        """［継続モードを解除］。バナーを消し、親ID（＝keyframe）を外す。"""
        return (
            gr.update(visible=False),
            None,
            "",
            "",
            "ℹ️ 継続モードを解除しました。通常の新規生成に戻ります"
            "（プロンプトとシードはそのまま残しています）。",
        )

    # ------------------------------------------- ④履歴タブの callbacks（P4）

    def on_history_tick(filter_label):
        """④タブ用の Timer。**プレーヤーには触れない**。"""
        return _history_view(filter_label)

    def on_history_filter(filter_label):
        return _history_view(filter_label)

    def on_select_history(key, filter_label):
        """選択された履歴レコードの詳細（3値。成功動画のみプレビューを出す）。"""
        try:
            rows = _history_rows(HISTORY_FILTERS.get(filter_label))
            return _preview_of(
                rows,
                str(key or "").strip(),
                with_status=True,
                empty_message=HISTORY_EMPTY_NOTE,
            )
        except Exception:  # 設計書 §13.2
            log.exception("履歴の詳細を取得できませんでした")
            return (
                None,
                "⚠️ 記録を読み込めませんでした（下の「詳しい情報」とアプリの動作ログをご確認ください）。",
                "- 履歴の詳細取得でエラーが発生しました（アプリの動作ログを参照）",
            )

    def on_history_concat(key):
        return _concat_message(key)

    def on_history_reveal(key):
        return _reveal_message(key)

    # ---------------------------------------------------------------- layout

    # `css=` は Gradio 6 では launch() 側へ移った引数だが、Blocks へ渡した値は
    # launch() が `_deprecated_css` として拾って /config へ載せる（6.22.0 で確認済み）。
    # ここで渡しておくと、起動側（app/main.py）を変えずに iPhone 対応を効かせられる。
    # Gradio 6.22.0 はこの渡し方に対して英語の UserWarning を出す。動作は正しく、
    # 初心者が見るターミナルに英語の警告が混ざるだけなので、この1件だけを抑止する
    # （他の警告は消さない）。Gradio が `css=` を完全に廃止したら launch() 側へ移す。
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*moved from the Blocks constructor to the launch\(\) method.*",
                category=UserWarning,
            )
            demo = gr.Blocks(title=cfg.name, analytics_enabled=False, css=MOBILE_CSS)
    except TypeError:  # 古い/新しい Gradio の差異でも起動を止めない
        demo = gr.Blocks(title=cfg.name)

    with demo:
        gr.Markdown(f"# {cfg.name}")
        if lan_info is not None:
            # LANモードの案内（PIN は含まれない。表示もしない）
            gr.HTML(_lan_banner_html(lan_info))
        header_md = gr.Markdown(_snapshot_texts()[0])

        if service.startup_warnings:
            gr.Markdown(
                "⚠️ " + "　/　".join(service.startup_warnings)
            )

        with gr.Tabs() as main_tabs:
            # ------------------------------------------------ ① 新規生成
            with gr.Tab("新規生成", id="tab_new"):
                # 継続モードのバナー（設計書 §5.2）。通常時は隠しておく。
                with gr.Group(visible=False) as continuation_group:
                    with gr.Row(elem_classes=["h3-row"]):
                        continuation_thumb = gr.Image(
                            label="継続元の先頭フレーム",
                            interactive=False,
                            height=180,
                        )
                        with gr.Column(scale=3):
                            continuation_md = gr.Markdown("")
                            continuation_clear_btn = gr.Button(
                                "継続モードを解除", size="sm"
                            )
                # 親IDだけを保持する（キーフレームのパスはブラウザへ出さない）
                continuation_parent = gr.Textbox(
                    value="", visible=False, label="継続元の親ID"
                )
                with gr.Row(elem_classes=["h3-row"]):
                    with gr.Column(scale=3):
                        prompt_box = gr.Textbox(
                            label="プロンプト（英語推奨・日本語セリフは <d>[Japanese] …</d> で囲みます）",
                            placeholder=(
                                "A cute small green dinosaur wizard stands inside a magical atelier.\n"
                                "He raises his wooden staff and says clearly in Japanese:\n"
                                f"{DIALOGUE_HINT}\n"
                                "Cinematic lighting, smooth natural motion.\n"
                                "No subtitles, no captions, no watermark."
                            ),
                            lines=10,
                        )
                        # プロンプト欄の直下に補助ボタンを2つ並べる。どちらも
                        # 主操作（生成をキューに追加）より小さい副ボタンにする。
                        with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                            hint_btn = gr.Button("＋日本語セリフ記法を挿入", size="sm")
                            clear_prompt_btn = gr.Button(
                                "プロンプトを消去", size="sm", variant="secondary"
                            )

                        length_radio = gr.Radio(
                            choices=list(LENGTH_CHOICES.keys()),
                            value=_length_label(cfg.default_num_frames),
                            label="動画の長さ",
                        )
                        step_radio = gr.Radio(
                            choices=list(STEP_CHOICES.keys()),
                            value=_step_label(cfg.default_steps),
                            label="品質（Turbo ステップ数）",
                        )
                        with gr.Row(elem_classes=["h3-row"]):
                            seed_random = gr.Checkbox(value=True, label="シードをランダムにする")
                            seed_number = gr.Number(
                                value=42,
                                label=f"シード値（0〜{SEED_MAX}）",
                                precision=0,
                                interactive=True,
                            )
                        fixed_md = gr.Markdown(
                            on_estimate_change(
                                _length_label(cfg.default_num_frames),
                                _step_label(cfg.default_steps),
                            )
                        )
                        submit_btn = gr.Button(
                            SUBMIT_LABEL, variant="primary", elem_classes=["h3-tap"]
                        )
                        submit_msg = gr.Markdown("")
                        gr.Markdown(
                            "ボタンは押した直後に一時的に押せなくなります"
                            "（続けて2回押しても、生成は1本だけ登録されます）。",
                            elem_classes=["h3-note"],
                        )

                    with gr.Column(scale=2):
                        gr.Markdown("### 直近の完成動画")
                        video_player = gr.Video(
                            label="完成動画", interactive=False, autoplay=False
                        )
                        video_info = gr.Markdown("まだ完成した動画はありません。")
                        progress_md = gr.Markdown(_snapshot_texts()[1])

                with gr.Accordion("詳しい情報（アプリの動作ログ）", open=False):
                    gr.Markdown(
                        "うまくいかないときに中身を確認するための記録です。"
                        "ふだんは開かなくてかまいません。",
                        elem_classes=["h3-note"],
                    )
                    log_box = gr.Textbox(
                        value=recent_logs(100),
                        lines=12,
                        label="アプリの動作ログ 直近100行",
                        interactive=False,
                        elem_classes=["h3-scroll"],
                    )

            # ------------------------------------------------ ② キュー（P3）
            with gr.Tab("キュー", id="tab_queue"):
                initial_queue = _queue_view()
                queue_banner = gr.HTML(initial_queue[0])
                with gr.Row(elem_classes=["h3-row"]):
                    with gr.Column(scale=3):
                        queue_current_md = gr.Markdown(initial_queue[2])
                        queue_waiting_md = gr.Markdown(
                            initial_queue[3], elem_classes=["h3-scroll"]
                        )
                    with gr.Column(scale=2):
                        queue_engine_md = gr.Markdown(initial_queue[1])
                        queue_error_md = gr.Markdown(initial_queue[4])

                with gr.Accordion("待機中のジョブを取り消す", open=True):
                    gr.Markdown(
                        "取り消せるのは**まだ始まっていない（待機中の）ジョブだけ**です。"
                        "実行中のジョブは取り消せません。"
                    )
                    with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                        cancel_dropdown = gr.Dropdown(
                            choices=[],
                            value=None,
                            label="取り消す待機ジョブ（ジョブIDを選ぶ）",
                            allow_custom_value=True,
                            interactive=True,
                        )
                        cancel_btn = gr.Button("選んだジョブを取り消す")
                    cancel_msg = gr.Markdown("")

                with gr.Accordion(
                    "ワーカーを再起動する（生成が戻らないときの最終手段）", open=False
                ):
                    gr.Markdown(
                        "⚠️ **実行中のジョブは失敗になります**（自動では作り直しません）。"
                        "待機中のジョブはキューに残ります。"
                        "モデルと Turbo LoRA を読み直すため、次の生成が始まるまで"
                        "数分かかることがあります。"
                        + (
                            ""
                            if _restart_available
                            else "\n\nℹ️ この版では再起動ボタンを使えません"
                            "（アプリ自体を再起動してください）。"
                        )
                    )
                    restart_ack = gr.Checkbox(
                        value=False,
                        label="実行中のジョブが失敗になることを理解しました",
                        interactive=_restart_available,
                    )
                    restart_btn = gr.Button(
                        "⚠ ワーカーを再起動する",
                        variant="stop",
                        interactive=_restart_available,
                    )
                    restart_msg = gr.Markdown("")

                with gr.Accordion("詳しい情報（サポート用）", open=False):
                    queue_detail_md = gr.Markdown(_queue_detail_view())

            # --------------------------------- ③ 完成・編集（P4 → P5.3-A で再設計）
            #
            # 「見る画面」から「作業する画面」へ。全動画の一覧表は④へ一本化し、
            # ここは 上段（選ぶ／プレビューと操作）＋ 下段（順番指定連結）の2段にした。
            # **DOM の並び順がそのまま iPhone の1カラム表示順**になるので、
            # CSS の order 指定や独自 JavaScript を使わずに希望順を満たせる（§24.3）。
            with gr.Tab(VIDEOS_TAB_LABEL, id="tab_videos"):
                initial_videos = _videos_view()
                # ---- 上段: 左＝作業する動画を選ぶ／右＝プレビューと操作
                #
                # 一覧表が無くなって左が短くなったので、**右を広く**とる
                # （プレビューと操作ボタンが横に並び、右カラムの背が低くなって
                # 左側にできる空白が小さくなる）。iPhone では順番に畳まれる。
                with gr.Row(elem_classes=["h3-row"]):
                    with gr.Column(scale=2):
                        gr.Markdown("### 作業する動画")
                        with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                            video_select = gr.Dropdown(
                                choices=[],
                                value=None,
                                label="表示する動画を選ぶ（個別／連結）",
                                allow_custom_value=True,
                                interactive=True,
                            )
                            video_reload_btn = gr.Button(
                                "↻ 選んだ動画を表示", size="sm"
                            )
                        # P5.3-A: 長大な一覧表の代わりに件数だけを出す（表は④へ）
                        videos_summary_md = gr.Markdown(
                            initial_videos[0], elem_classes=["h3-note"]
                        )
                    with gr.Column(scale=3):
                        video_player3 = gr.Video(
                            label="プレビュー", interactive=False, autoplay=False
                        )
                        video_meta_md = gr.Markdown(VIDEOS_EMPTY_NOTE)
                        with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                            video_continue_btn = gr.Button(
                                "この動画の続きを作る", variant="primary"
                            )
                            video_concat_btn = gr.Button("ルートからここまでを連結")
                            video_reveal_btn = gr.Button("Finderで表示（Macのみ）")
                        # ここが将来 [1080p高品質化] を足す場所（P6）。今は何も置かない
                        videos_msg_md = gr.Markdown("")
                        gr.Markdown(FINDER_NOTE, elem_classes=["h3-note"])

                        # ---- P5.3-B: アプリ内ゴミ箱。**実在する動画を選んだ
                        # ときだけ**現れる（記録だけの動画はそもそも一覧に出ない）
                        with gr.Group(visible=False, elem_classes=["h3-panel"]) as trash_group:
                            gr.Markdown("### この動画を整理する")
                            gr.Markdown(
                                f"動画は `data/{TRASH_DIR_LABEL}/` へ移動します。"
                                "**アプリ上の復元機能はありません**"
                                "（戻したいときは Finder で元のフォルダへ移してください）。"
                                "この動画を使った既存の連結動画は消えません。",
                                elem_classes=["h3-note"],
                            )
                            trash_ack = gr.Checkbox(
                                value=False,
                                label="この動画をアプリのゴミ箱へ移動することを確認しました",
                            )
                            trash_btn = gr.Button(
                                TRASH_BUTTON_LABEL,
                                variant="stop",
                                visible=False,
                                elem_classes=["h3-tap"],
                            )

                        with gr.Accordion("詳しい情報（サポート用）", open=False):
                            video_tech_md = gr.Markdown(
                                "動画を選ぶと、ここに技術的な情報が出ます。"
                            )

                # ---- 下段: 順番指定連結（P5.3-A で Accordion をやめ常時表示）
                #
                # 一覧表が無くなってページが短くなったので、隠す理由が消えた。
                # 機能・配線・純粋関数は P5.2 のまま（部品の置き場所だけを変えた）。
                with gr.Group(elem_classes=["h3-panel"]):
                    gr.Markdown(f"### {CUSTOM_CONCAT_TITLE}")
                    gr.Markdown(
                        "好きな動画を好きな順番でつなげます。"
                        "**上から順に**再生される1本の動画になります"
                        f"（{MIN_CUSTOM_CLIPS}〜{MAX_CUSTOM_CLIPS}本）。"
                        "**元の動画ファイルは変わりません。**"
                        "［候補から外す］［連結候補をすべて解除］は、"
                        "ここで組み立てている順番から外すだけで、動画は削除されません。",
                        elem_classes=["h3-note"],
                    )
                    with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                        custom_pick = gr.Dropdown(
                            choices=[],
                            value=None,
                            label="追加する動画を選ぶ（個別動画のみ）",
                            allow_custom_value=True,
                            interactive=True,
                        )
                        custom_add_btn = gr.Button(
                            CUSTOM_ADD_LABEL, size="sm", elem_classes=["h3-btn-accent"]
                        )
                    # 合計は**スクロール領域の外**に出す（20本でも常に読める）
                    custom_total_md = gr.Markdown(_order_total_text([]))
                    custom_order_md = gr.Markdown(
                        _order_text([]), elem_classes=["h3-vscroll"]
                    )
                    with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                        custom_target = gr.Dropdown(
                            choices=[],
                            value=None,
                            label="対象を選ぶ（順番の入れ替え・候補から外す）",
                            allow_custom_value=True,
                            interactive=True,
                        )
                        custom_up_btn = gr.Button(
                            CUSTOM_UP_LABEL, size="sm", elem_classes=["h3-btn"]
                        )
                        custom_down_btn = gr.Button(
                            CUSTOM_DOWN_LABEL, size="sm", elem_classes=["h3-btn"]
                        )
                        custom_remove_btn = gr.Button(
                            CUSTOM_REMOVE_LABEL, size="sm", elem_classes=["h3-btn"]
                        )
                    with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                        custom_clear_btn = gr.Button(
                            CUSTOM_CLEAR_LABEL,
                            size="sm",
                            variant="secondary",
                            elem_classes=["h3-btn"],
                        )
                        # **塗りつぶしのオレンジはこの最終実行だけ**にする
                        custom_start_btn = gr.Button(
                            CUSTOM_CONCAT_LABEL, variant="primary"
                        )
                    custom_msg_md = gr.Markdown("")

                # ---- 連結の状態（チェーン連結・指定順連結の共通表示）はフル幅
                concat_status_md = gr.Markdown(initial_videos[2])

            # ------------------------------------------------ ④ 履歴（P4）
            with gr.Tab("履歴", id="tab_history"):
                history_filter = gr.Radio(
                    choices=list(HISTORY_FILTERS.keys()),
                    value="すべて",
                    label="状態フィルタ",
                )
                initial_history = _history_view("すべて")
                with gr.Row(elem_classes=["h3-row"]):
                    with gr.Column(scale=3):
                        gr.Markdown(
                            "表は横に長いので、iPhone では**表の中を横になぞる**と"
                            "残りの列を見られます。",
                            elem_classes=["h3-note"],
                        )
                        history_list_md = gr.Markdown(
                            initial_history[0], elem_classes=["h3-scroll"]
                        )
                        with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                            history_select = gr.Dropdown(
                                choices=[],
                                value=None,
                                label="詳細を見る記録を選ぶ",
                                allow_custom_value=True,
                                interactive=True,
                            )
                            history_reload_btn = gr.Button(
                                "↻ 選んだ記録を表示", size="sm"
                            )
                    with gr.Column(scale=2):
                        history_player = gr.Video(
                            label="プレビュー（成功した動画のみ）",
                            interactive=False,
                            autoplay=False,
                        )
                        history_detail_md = gr.Markdown(HISTORY_EMPTY_NOTE)
                        with gr.Row(elem_classes=["h3-row", "h3-tap"]):
                            history_continue_btn = gr.Button("この動画の続きを作る")
                            history_concat_btn = gr.Button("ルートからここまでを連結")
                            history_reveal_btn = gr.Button("Finderで表示（Macのみ）")
                        history_msg_md = gr.Markdown("")
                        gr.Markdown(FINDER_NOTE, elem_classes=["h3-note"])
                        with gr.Accordion("詳しい情報（サポート用）", open=False):
                            history_tech_md = gr.Markdown(
                                "記録を選ぶと、ここに技術的な情報が出ます。"
                            )

        latest_state = gr.State("")
        selected_video_state = gr.State("")
        selected_history_state = gr.State("")
        # P5.2: 指定順連結の並び。**ブラウザセッションごと**に独立して持つので、
        # Mac と iPhone を同時に開いても互いの選択が混ざらない（設計書 §23.5）。
        custom_order_state = gr.State([])

        # 送信は即座に戻る（生成完了を待たない）。継続モードのときは親IDも一緒に渡す。
        #
        # 二重投入防止（P5 §6.2）は2段構え:
        #   1. 押した瞬間にボタンを無効化し、応答後に必ず戻す（下の3段の `.then()`）。
        #      `.then()` は前段が失敗しても実行されるので、ボタンが押せないまま
        #      取り残されることがない。
        #   2. それでもすり抜けた同時押しは AppService 側が短時間だけ冪等化する。
        # **Timer の outputs にこのボタンを入れてはいけない**（毎秒上書きされて
        # 無効化が即座に取り消されてしまうため）。
        submit_btn.click(
            lambda: gr.update(interactive=False, value=SUBMIT_LABEL_BUSY),
            outputs=submit_btn,
            api_name=False,
        ).then(
            on_submit_v2,
            inputs=[
                prompt_box,
                length_radio,
                step_radio,
                seed_random,
                seed_number,
                continuation_parent,
            ],
            outputs=[submit_msg, header_md, progress_md],
            api_name="on_submit_v2",
        ).then(
            lambda: gr.update(interactive=True, value=SUBMIT_LABEL),
            outputs=submit_btn,
            api_name=False,
        )
        # P1 互換の `/on_submit`（5引数・3戻り値）を温存するための API 専用トリガ。
        # 画面には出さないが、既存のクライアント・テストはこちらを呼び続けられる。
        legacy_submit_btn = gr.Button("（互換API）生成をキューに追加", visible=False)
        legacy_submit_btn.click(
            on_submit,
            inputs=[prompt_box, length_radio, step_radio, seed_random, seed_number],
            outputs=[submit_msg, header_md, progress_md],
            api_name="on_submit",
        )
        hint_btn.click(on_insert_hint, inputs=prompt_box, outputs=prompt_box)
        # 繰り返し使う操作なので確認は挟まない。**outputs はプロンプト欄だけ**に
        # 限定してあるので、他の入力欄・キュー・履歴は書き換わらない。
        clear_prompt_btn.click(
            on_clear_prompt, outputs=prompt_box, api_name="on_clear_prompt"
        )
        length_radio.change(
            on_estimate_change, inputs=[length_radio, step_radio], outputs=fixed_md
        )
        step_radio.change(
            on_estimate_change, inputs=[length_radio, step_radio], outputs=fixed_md
        )
        seed_random.change(
            lambda is_random: gr.update(interactive=not is_random),
            inputs=seed_random,
            outputs=seed_number,
        )

        queue_outputs = [
            queue_banner,
            queue_engine_md,
            queue_current_md,
            queue_waiting_md,
            queue_error_md,
            cancel_dropdown,
        ]
        cancel_btn.click(
            on_cancel_queued,
            inputs=cancel_dropdown,
            outputs=[cancel_msg, *queue_outputs],
            api_name="on_cancel_queued",
        )
        restart_btn.click(
            on_restart_worker,
            inputs=restart_ack,
            outputs=[restart_msg, *queue_outputs],
            api_name="on_restart_worker",
        )

        # ------------------------------------------------ ③完成動画タブの配線（P4）
        # P5.2 で末尾に custom_pick を追加した（既存3つの順番は変えていない）。
        # **candidate の choices だけ**を更新するので、編集中の並びには触れない。
        # P5.3-A では**先頭の中身**を一覧表から要約へ替えただけで、数も順番も不変。
        videos_outputs = [
            videos_summary_md,
            video_select,
            concat_status_md,
            custom_pick,
        ]
        video_reload_btn.click(
            on_select_video,
            inputs=video_select,
            outputs=[video_player3, video_meta_md, video_tech_md],
            api_name="on_select_video",
        )
        video_concat_btn.click(
            on_start_concat,
            inputs=video_select,
            outputs=[videos_msg_md, concat_status_md],
            api_name="on_start_concat",
        )
        video_reveal_btn.click(
            on_reveal_video,
            inputs=video_select,
            outputs=videos_msg_md,
            api_name="on_reveal_video",
        )

        # ---- P5.2: 指定順連結。**Timer はこれらの outputs に触れない**ので、
        # 1秒ごとの更新でユーザーが編集中の並びが巻き戻ることがない。
        custom_outputs = [
            custom_order_state,
            custom_order_md,
            custom_target,
            custom_start_btn,
            custom_msg_md,
            # P5.3-A: 合計（スクロール領域の外）。末尾追加で既存5つは不変
            custom_total_md,
        ]
        custom_add_btn.click(
            on_custom_add,
            inputs=[custom_order_state, custom_pick],
            outputs=custom_outputs,
            api_name="on_custom_add",
        )
        custom_up_btn.click(
            on_custom_up,
            inputs=[custom_order_state, custom_target],
            outputs=custom_outputs,
            api_name="on_custom_up",
        )
        custom_down_btn.click(
            on_custom_down,
            inputs=[custom_order_state, custom_target],
            outputs=custom_outputs,
            api_name="on_custom_down",
        )
        custom_remove_btn.click(
            on_custom_remove,
            inputs=[custom_order_state, custom_target],
            outputs=custom_outputs,
            api_name="on_custom_remove",
        )
        custom_clear_btn.click(
            on_custom_clear,
            inputs=custom_order_state,
            outputs=custom_outputs,
            api_name="on_custom_clear",
        )
        custom_start_btn.click(
            on_custom_start,
            inputs=custom_order_state,
            outputs=[*custom_outputs, concat_status_md],
            api_name="on_custom_start",
        )

        continuation_outputs = [
            main_tabs,
            continuation_group,
            continuation_thumb,
            continuation_md,
            continuation_parent,
            prompt_box,
            length_radio,
            step_radio,
            seed_random,
            seed_number,
        ]
        video_continue_btn.click(
            on_start_continuation,
            inputs=video_select,
            outputs=[*continuation_outputs, videos_msg_md],
            api_name="on_start_continuation",
        )
        continuation_clear_btn.click(
            on_clear_continuation,
            outputs=[
                continuation_group,
                continuation_thumb,
                continuation_md,
                continuation_parent,
                submit_msg,
            ],
            api_name="on_clear_continuation",
        )

        # ------------------------------------------------ ④履歴タブの配線（P4）
        history_outputs = [history_list_md, history_select]
        history_filter.change(
            on_history_filter,
            inputs=history_filter,
            outputs=history_outputs,
            api_name="on_history_filter",
        )
        history_reload_btn.click(
            on_select_history,
            inputs=[history_select, history_filter],
            outputs=[history_player, history_detail_md, history_tech_md],
            api_name="on_select_history",
        )
        history_concat_btn.click(
            on_history_concat,
            inputs=history_select,
            outputs=history_msg_md,
            api_name="on_history_concat",
        )
        history_reveal_btn.click(
            on_history_reveal,
            inputs=history_select,
            outputs=history_msg_md,
            api_name="on_history_reveal",
        )
        history_continue_btn.click(
            on_history_continuation,
            inputs=history_select,
            outputs=[*continuation_outputs, history_msg_md],
            api_name="on_history_continuation",
        )

        timer = gr.Timer(1.0)
        timer.tick(
            on_tick, outputs=[header_md, progress_md, log_box, latest_state]
        )
        # ②キュータブは専用ハンドラで更新する。①の on_tick の戻り値（既存 api_name
        # `/on_tick`）を変えないための分離で、②タブ内の表示は1スナップショットで揃う。
        timer.tick(on_queue_tick, outputs=queue_outputs, api_name="on_queue_tick")
        # ③④も専用ハンドラ。**プレーヤーとプレビューは outputs に入れない**ので、
        # 毎 tick の更新で再生が中断したり選択が飛んだりしない。
        timer.tick(on_videos_tick, outputs=videos_outputs, api_name="on_videos_tick")
        timer.tick(
            on_history_tick,
            inputs=history_filter,
            outputs=history_outputs,
            api_name="on_history_tick",
        )
        # 「詳しい情報」は毎秒でなくてよい。iPhone（Wi-Fi 越し）での通信を増やさない
        # よう、専用の低頻度 Timer で更新する。**生成ボタンは outputs に入れない**。
        detail_timer = gr.Timer(5.0)
        detail_timer.tick(
            lambda: _queue_detail_view(),
            outputs=queue_detail_md,
            api_name="on_queue_detail_tick",
        )

        # 完成動画は「最新IDが変わったときだけ」差し替える（再生の中断を防ぐ）
        latest_state.change(
            on_latest_changed, inputs=latest_state, outputs=[video_player, video_info]
        )
        # ③④のプレビューも同じ方式。Dropdown の値を gr.State へ写し、
        # **State が変化したときだけ** プレーヤーを差し替える（二重の歯止め）。
        video_select.change(
            lambda key: str(key or ""),
            inputs=video_select,
            outputs=selected_video_state,
            api_name=False,
        )
        selected_video_state.change(
            on_select_video,
            inputs=selected_video_state,
            outputs=[video_player3, video_meta_md, video_tech_md],
            api_name=False,
        )
        # P5.3-B: 整理セクションは**選択が変わったときだけ**出し入れする。
        # Timer には一切載せない（1秒ごとに確認チェックが外れると使えないため）。
        selected_video_state.change(
            on_selection_changed_for_trash,
            inputs=selected_video_state,
            outputs=[trash_group, trash_btn, trash_ack],
            api_name=False,
        )
        trash_btn.click(
            on_move_to_trash,
            inputs=[video_select, trash_ack],
            outputs=[
                video_select,
                video_player3,
                video_meta_md,
                video_tech_md,
                videos_summary_md,
                custom_pick,
                trash_group,
                trash_btn,
                trash_ack,
                videos_msg_md,
            ],
            api_name="on_move_to_trash",
        )
        history_select.change(
            lambda key: str(key or ""),
            inputs=history_select,
            outputs=selected_history_state,
            api_name=False,
        )
        selected_history_state.change(
            on_select_history,
            inputs=[selected_history_state, history_filter],
            outputs=[history_player, history_detail_md, history_tech_md],
            api_name=False,
        )

    return demo
