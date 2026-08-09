# ai_summary.md — 現在状態の正本

> この文書は「最新状態の正本」であり、過去ログは積まず常に現在地を表すよう上書き更新する。
> 経緯は `changelog.md`、恒久ルールは `CLAUDE.md`、設計は `docs/v1-design.md`（v1.7）を参照。

- **最終更新日**: 2026-08-10
- **プロジェクトの目的**: Mac mini（M4・24GB・MPS）上で動く完全ローカルの音声付き動画生成アプリ。初心者がブラウザ（Gradio・日本語UI・localhost限定）から MiniMax-H3 動画を生成できるようにする。
- **現在フェーズ**: **P5.3-A 完了（未コミット）。V1.0.0 Release Candidate（実iPhoneでの最終確認待ち）**

## P5.3-A で入れた完成・編集ワークスペース（2026-08-10）

③タブを「一覧を見る画面」から「**選ぶ・見る・つなげる作業画面**」へ再設計した。
**機能追加はなく画面構成だけの変更**。設計は `docs/v1-design.md` §24（決定D19）。

- **③の全動画一覧表を廃止し④へ一本化** — 27件で3,000px超になり主機能まで長くスクロール
  していた。③には**件数と欠損件数の要約**だけを出す（Mac のページ全長は **1110px** へ）
- **タブ名「完成動画」→「完成・編集」**（**内部ID `tab_videos` は不変**）
- **上段2カラム＋下段（順番指定連結）＋フル幅の連結状態**。**DOM の並び順＝iPhone の表示順**
  なので、CSS の order も独自 JS も要らない
- **指定順連結は常時表示**（Accordion 廃止）。連結順の一覧だけ `.h3-vscroll` で局所縦スクロールし、
  合計は器の外に出す（20本でもページは伸びない）
- **`on_videos_tick` は4出力・同じ順序のまま、先頭の意味だけ**を一覧表→要約へ変更
- **④に「連結成果物」フィルタ**を追加（既存7フィルタは不変）。`c_*` と `cm_*` を連結向けの列で表示
- **順番指定連結の見え方**（仕上げ）— Group 既定背景と secondary ボタンの灰色が同化して
  補助操作がボタンに見えなかった。パネルを `.h3-panel`、補助操作を `.h3-btn`（白＋枠線）、
  追加を `.h3-btn-accent`（オレンジの枠線）にし、**塗りつぶしのオレンジは最終実行だけ**に。
  文言も「削除」→「**候補から外す**」へ変え、ファイル削除と区別した
- **動画の整理（除外・ゴミ箱）は P5.3-B で未実装**。欠損記録は候補に残し件数を知らせるだけ

## P5.2 で入れた任意順序連結（2026-08-09）

好きな動画を好きな順番でつなぐ機能。**生成エンジン・キュー契約・履歴スキーマは無変更**。
設計は `docs/v1-design.md` §23（決定D18）。

- **成果物は専用台帳** `data/concat_manifest.json` へ記録する（`history.json` は1バイトも変えない）。
  チェーン連結の `concat_path` は1レコード1件しか持てず、A→B→C と A→D→C のような
  別の組み合わせで**上書き**が起きるため。旧版互換も壊さない
- **台帳の保存規律は HistoryStore と同じ** — 一意tmp→fsync→`os.replace()`／検証済み primary のみ
  `.bak` 更新／破損は隔離／`.bak` も検証してから復旧／両方壊れても**MP4 は消さない**
- **昇格後に台帳保存が失敗したら正式 MP4 をロールバック削除**（できなければ隔離、
  それも無理なら正確なパスをログへ）。孤児 MP4 を残さない
- **チェーン連結と同じ `ConcatService`** に第2の入口を足したので、両者は自動的に相互排他
- 入力は**ジョブIDの並びだけ**。実行スレッド内で9項目を再検証し、**指定順をそのまま**使う
  （互換性はチェーンの上位集合。音声仕様だけは実ファイルを見て判定）
- UI は③タブの初期 closed な折りたたみ。並びは `gr.State`＝**セッション独立**で、
  Timer は候補の choices しか触らないため編集中の順番が壊れない

## P5.1 で入れた UI 改修（2026-08-09・小規模）

生成エンジン・キュー契約・履歴スキーマ・設計契約は**無変更**（設計書と CLAUDE.md も更新不要）。

- **［プロンプトを消去］**（①新規生成）— プロンプト欄の直下、［＋日本語セリフ記法を挿入］と
  同じ行の副ボタン。`on_clear_prompt()` は**引数なし・空文字1つを返すだけ**で、配線の
  `outputs` もプロンプト欄1つに限定。他の設定・キュー・履歴には**構造的に**触れられない
- **③完成動画タブの選択欄を一覧の上へ移動** — 一覧が長くなってもタブを開いた直後に選べる。
  複製ではなく移動で、配線と Timer の出力順は無変更（選択状態の保持もそのまま）
- **案内文の整合** — ③の右側は「上の選択欄から動画を選び、［選んだ動画を表示］を押すと…」。
  ③④で共有していた文言を `VIDEOS_EMPTY_NOTE` / `HISTORY_EMPTY_NOTE` に分離し、
  選択欄が一覧の下にある④へ③の文言が出ないようにした

## 現在の完成状況

設計書 `docs/v1-design.md` は **v1.3**。P0（基盤）→ P0.5（バックエンド境界）→ P1（履歴・キュー・Engine契約・MockEngine・新規生成タブ）→ **P2（実機エンジン）** まで完了。
**実モデル（MiniMax-H3-NF4 + Turbo LoRA）でブラウザから動画生成できる状態**。config の既定は `engine.mode = "real"`。

### P5 で完成した機能

- **2つの起動モード** — 通常（既定・`127.0.0.1`・認証なし）と **iPhone接続モード**（`--lan` を明示したときだけ）。**config や環境変数では LANモードを有効にできない**（決定D13）
- **LAN IP 検出** — RFC1918 の3レンジだけを許可し、public IP・`0.0.0.0`・link-local・CGNAT・IPv6・ホスト名を拒否。選択順序は `en0`→`en1`→名前昇順で予測可能。検出できなければ `0.0.0.0` へフォールバックせず中止
- **PIN 認証** — 起動ごとに `secrets` で6桁。**メモリとターミナル表示にしか存在しない**（config・履歴・ログ・URL・QR・プロセス引数・環境変数のどこにも出ない。実測で0件確認）。HMAC ダイジェストの定数時間比較、10回失敗で30秒ロック
- **QR コード** — `segno` で完全ローカル生成。**URL のみを符号化**（クエリ・フラグメント・userinfo は関数が拒否）。PNG は `0o600`、正常終了時に削除
- **iPhone 向けUI** — `@media (max-width: 640px)` の中だけで1カラム化・44pxタップ領域・局所横スクロール。**Mac の表示は構造的に非回帰**
- **二重投入防止** — UI のボタン無効化＋サーバ側2秒の冪等化。取消・失敗の直後の再投入は正当な新規投入として通す
- **P4残件** — キーフレーム寸法検証（`input`・非fatal・モデルを捨てない）、長チェーンの検証式（§10.6.2）、`concat.reencode` の排除
- **`.command` 2種** — 通常用と iPhone接続用。空白・日本語を含むパスでも動作、`caffeinate` でスリープ防止、Ctrl+C で1秒・orphanなし

## P5 実機試験の結果（Mac mini M4・実LAN 192.168.32.145）

| 項目 | 実測 |
|---|---|
| 通常モード | `127.0.0.1:7860` のみで待受。**LAN IP からは接続不可** |
| LANモード bind | `192.168.32.145:7860` のみ（`0.0.0.0` ではない） |
| 認証前（LAN経由） | `/config`・`/info`・`/openapi.json`・`file=`・`run/predict`・`queue/join` すべて **401** |
| 誤PIN | **400**（Cookie は発行されない） |
| 認証後 | `/config` 200、連結動画が **206 / video/mp4**（Range 動作） |
| 履歴JSON・ログ・`/etc/hosts` | 認証後でも **403** |
| PIN 非漏洩 | ログ・履歴・config・プロセス引数・環境変数・QR画像すべて **0件** |
| QR | URL のみから再生成した画像と**バイト単位で一致**＝URL以外を埋め込んでいない証明 |
| 二重投入 | 同一内容の連続投入が1件に集約（「すでに登録されています」） |
| 再接続 | 別セッションからキュー状態を取得できる |
| Ctrl+C | **1.0秒・終了コード0**・QR削除済み・orphanなし・ポート解放 |

### P4 で完成した機能

- **継続生成** — 成功動画から「この動画の続きを作る」。親の正式な最終フレーム PNG を先頭キーフレーム（`keyframe_indices=[0]` 固定）として渡し、親 seed・長さ・ステップ・プロンプトをプリフィル。継続元の不備（未成功・backend/実行方式/解像度の不一致・PNG 欠損/破損・チェーン深さ上限）は**投入した瞬間に日本語で拒否**
- **多層防御** — AppService（投入時）→ RealEngine/MockEngine（`validate_keyframe`）→ h3_worker（data_root 境界・PIL 検証）の3層。real/mock で拒否文言まで一致
- **全チェーン連結** — `resolve_concat_chain()` が root→選択ノードを解決し、SUCCESS 以外・backend/model/解像度/fps 非互換・成果物欠損・2本未満を拒否。`ConcatService` がバックグラウンドで PTS正規化つき再エンコード連結を行い、partial→検証→`os.replace()` 昇格後にのみ履歴へ記録
- **③完成動画タブ / ④履歴タブ** — 個別動画と連結動画の一覧・プレビュー・メタ表示・継続生成・連結・Finder 表示。状態フィルタ、異常データ（QUEUED/RUNNING 残存）の表示
- **Finder 表示** — `["open","-R",path]` の引数配列（`shell=True` 不使用）。**サーバ側で解決した正式成果物のみ**許可
- **履歴スキーマは変更なし**（既存の `parent_id`/`keyframe_path`/`concat_path`/`concat_sources` を使用）。ユーザーの既存動画・履歴は無変更

## P4 実機試験の結果（Mac mini M4 24GB・白狐探偵）

| 項目 | 実測 |
|---|---|
| **S6** 親（124f/4step・seed 505406688） | **806.4秒**成功 |
| **S6** 子（継続生成・同一 seed・親の最終フレーム） | **845.4秒**成功。`parent_id` と `type="continuation"` を履歴へ記録 |
| **S7** 全チェーン連結（2本） | **248フレーム・10.33秒**。H.264 High/yuv420p/576×320、AAC 32000Hz ステレオ。**A/V skew +0.030s**。source の size/mtime 不変 |
| **S11** Finder | 連結動画を表示成功。`/etc/hosts` は日本語で拒否 |
| HTTP 配信 | 親・子・連結とも **HTTP 206 / video/mp4**。履歴JSON・ログは **403** |
| **重複フレーム測定** | 親 `_last.png` vs 子 frame0 = **平均10.222 / 最大221**。同条件（親MP4末尾 vs 子frame0）= **平均9.762 / 最大200** |

**重複1フレーム自動除去は採用せず既定 OFF**。キーフレームは VAE latent の条件アンカーであり出力ピクセルへコピーされないため（DiffSynth ソースで確認）、親子の境界フレームはピクセル一致しない。実測差（平均9.8〜10.2）は「別の絵」（13.5）と同オーダーで、一致判定が成立する閾値は存在しない。詳細は設計書 §10.6.1。

### P3 で完成した機能

- **②キュータブ**（`app/ui/minimal.py`）— 現在の処理（ID・概要・長さ・ステップ・seed・状態・ステージ・進捗バー・経過・目安）／エンジン状態（初期化中・待機・生成中・**再起動待ち（残り秒）**・再初期化中・停止中）／待機一覧＋取消／直前の失敗の分類表示／[ワーカーを再起動]（確認チェック必須）／赤色バナー（HALTED・受付停止）
- **「最終処理中」表示** — 実機は 4/4 到達後に約150秒イベントが来ない（VAE・音声デコードが `pipe()` 内部で進むため）。`step == total_steps` かつ `GENERATING` かつ20秒無音で「最終処理中」に切り替え、**ハングに見せない**
- **自動再起動とバックオフ**（`app/core/job_queue.py`）— fatal／ワーカー異常終了で実行中ジョブを FAILED 確定（自動再実行なし）→ 連続失敗カウント → バックオフ（5秒→30秒）→ `engine.restart()` → **READY 到達まで次ジョブを開始しない**。SUCCESS でカウント0リセット。上限超過で HALTED（**待機ジョブは QUEUED のまま保持**）。非fatal（input）では再起動もカウントもしない
- **手動再起動** — `restart_worker()` は要求を置くだけでディスパッチャが実行（再起動経路を1本に保ち、UI をブロックしない）。idle／running／dead／backoff／HALTED すべてで安全。HALTED からの復帰も兼ねる
- **watchdog** — 判定は**総経過時間 vs 目安×係数**（イベント間隔では判定しない）。警告 `stall_warn_factor=3.0`（56f/4step で約20分）で `stalled=True` にするのみ。強制終了 `stall_abort_factor` は**既定 0.0＝無効**（§13.2「自動停止はしない」を既定で守る）
- **ディスク容量ガード** — 投入時と**ディスパッチ直前**の両方で確認。受付停止中はジョブを QUEUED のまま保持（失敗させない）。生成中も1秒間隔で再評価しバナーを最新に保つ
- **MockEngine.restart() の既知問題を解消** — 中断 ERROR(fatal, worker_dead) を先に発行してから再初期化する RealEngine と同一契約に統一。イベントキューを作り直さない

## P3 実機試験の結果（Mac mini M4 24GB・実測）

| 試験 | 結果 |
|---|---|
| **S3** 124フレーム・4ステップ | **819.0秒**で成功（目安819秒に対し比1.00）。H.264/yuv420p/576×320、AAC 32kHz、124フレーム・5.16秒 |
| **S4** 56フレーム・8ステップ | **677.6秒**で成功。**8ステップは4ステップの約1.67倍**（設計時の想定2.0倍より速いことが判明）→ `step8_factor` を 1.67 へ更新 |
| **S8** 生成中に SIGKILL → 自動復旧 | step 1/4 でワーカーを SIGKILL → 1本目が `worker_dead` で FAILED（成果物なし）→ **backoff → restarting → idle** を観測 → 新ワーカー（PID 変化）→ 2本目 400.7秒で SUCCESS。orphan なし |
| **ブラウザ（②キュータブ）** | 直列処理（実行中1・待機1）／現在の処理にステップ・経過・目安／待機一覧の表示／**UI から待機ジョブを取消 → 履歴 CANCELED**／**手動再起動 → 実行中ジョブが `worker_dead` で FAILED・ワーカー PID 変化・READY 復帰**／動画配信 HTTP 206 |
| 目安値の精度 | 更新後の `estimates` は 56f/4step・124f/4step・56f/8step のいずれも実測との比 **1.00〜1.01** |

## P2 実機試験の結果（Mac mini M4 24GB・実測）

### P2 で完成した機能

- **`app/engine/backends/minimax_h3/h3_worker.py`** — DiffSynth venv で動く自己完結ワーカー（`app.*` 非依存・標準ライブラリ＋torch/diffsynth のみ）。実証スクリプトの構成を一字一句移植（vram_config 全disk・bfloat16・mps・`vram_limit=0`）。JSON Lines プロトコル、入力再検証、`progress_bar_cmd` ラッパ、partial 保存、エラー分類、ジョブ後のメモリ解放
- **`app/engine/real_engine.py`** — P1 の Engine 契約を満たす実機エンジン。subprocess 起動（`shell=False`・cwd=DiffSynth-Studio）、stdout/stderr の独立 drain、handshake 照合、**partial の検証と正式名への昇格**（昇格後にのみ DONE）、ワーカー終了検知と `worker_dead` 合成、shutdown（grace→terminate→kill）
- **AppService / main.py の real 対応** — `engine.mode` で RealEngine/MockEngine を選択。`--smoke` は実機モデルを読まないよう mock を強制
- **preflight のワーカースクリプト検査**

### （P2 実測表）

| 項目 | 実測値 |
|---|---|
| モデル初期化（Stage 0） | **4.8秒**。ディスクオフロード構成のため `from_pretrained` は構造構築のみで、重みは生成中にストリーミングされる（設計時の仮値300秒は誤り） |
| Turbo LoRA 適用 | 成功（worker.log に `259 tensors are patched by LoRA`） |
| 1本目 56f/4step（Stage 1） | **405.8秒**（約6.8分。設計の目安6〜7分と一致） |
| 2本目 同一ワーカー（Stage 2） | **401.1秒**（1本目比 0.99）。**worker PID 18203 が同一**＝常駐再利用（S5）成立 |
| ブラウザ経由（Stage 3） | 398.4秒。投入コールバックの応答 **0.06秒**（非ブロッキング） |
| 進捗取得 | **1/4 → 4/4 を実機で取得**（`progress_bar_cmd` がデノイズループの1箇所でのみ呼ばれ、`len(timesteps)==num_inference_steps` であることをソースで確認済み） |
| 成果物 | H.264 High / yuv420p / 576×320 / AAC 32000Hz ステレオ / 56フレーム / 2.33秒。最終フレーム PNG 576×320 |
| メモリ（ワーカー RSS） | 初期化後 約2116MiB → 生成中 約4225MiB。**2本目も横ばい**（蓄積なし） |
| 配信 | HTTP 206 `video/mp4`（ブラウザのレンジ取得） |
| オフライン性 | worker.log に `[√] Skip download and load only pre-downloaded model files`。モデルファイルの mtime/サイズ変化ゼロ＝追加ダウンロードなし |
| DiffSynth-Studio | **全作業・実生成3本を通じて変更ファイルゼロ** |

### P1 で完成した機能

- **履歴ストア** `app/core/history.py` — v1.2 スキーマ、原子的保存（一意tmp→`os.replace()`）、検証後のみ `.bak` 更新（プロセス内1回）、破損時の隔離＋`.bak`検証つき復旧、起動時 QUEUED/RUNNING→INTERRUPTED、親子チェーン解決（欠損親・循環・深さ20検出）、data_root 相対保存と境界検証、保存失敗時のロールバック
- **単一ジョブキュー** `app/core/job_queue.py` — 直列ディスパッチャ（daemon スレッド1本）、FIFO、同時 RUNNING 最大1、QUEUED取消、上限、`can_transition` による遷移検証、失敗しても後続継続、不変 snapshot、安全な shutdown
- **Engine 共通契約** `app/engine/base.py` — `Protocol`（identity / capabilities / state / start / submit / poll_event / shutdown / restart）。RealEngine は P2
- **MockEngine** `app/engine/mock_engine.py` — 実機と同順のイベント、capabilities 固定値、4/8ステップの progress、56/124f の素材選択、seed 採番と返却、`[MOCK_FAIL]` 失敗注入、成果物の原子的昇格（両方昇格後にのみ DONE）、sleep 注入でテスト高速化
- **統合層** `app/core/app_service.py` — ID採番（衝突回避）、JobSpec 生成と検証、`JobRecorder` 実装で JobQueue→HistoryStore を配線、空き容量ガード、起動/停止の順序、冪等な `start()`
- **共有契約** `app/core/contracts.py` — 状態列挙・遷移表・JobSpec/JobView/QueueSnapshot・EngineEvent・capabilities・入力検証（UI を迂回しても不正値を通さない）
- **①新規生成タブ** `app/ui/minimal.py` — プロンプト・長さ・ステップ・seed（ランダム/指定）・目安時間・投入・状態/待機数/進捗/経過時間・完成動画プレビュー・詳細ログ。Timer ポーリング、投入は非ブロッキング、コールバックは例外で壊れない

②キュー / ③完成動画 / ④履歴タブは骨格のまま（P3・P4）。

## テスト結果（最新）

- **1170 passed + 1 xpassed**（P4 時点は 625+1。P5 で 367 件、P5.1 で 15 件、P5.2 で 137 件、P5.3-A で 25 件追加）
  - キュー 70／RealEngine 50／MockEngine 41／ワーカー 163／P2統合 13／UI経路 17／履歴 38／P1結合 10／基盤系ほか
- `setup.sh` 成功、`start.sh --check`（mock/real/deep-check）合格、`--smoke` HTTP 200
- **ブラウザ相当の実配信検証**: 動画は HTTP 206・`video/mp4` で配信、履歴JSON/ログ/data_root外は 403 拒否
- **Ctrl+C 実測**: exit 0・5.2秒で停止（非デーモンスレッドの残留なし）
- **実機3ステージ合格**（Stage 0 初期化 / Stage 1 生成 / Stage 2 常駐再利用 / Stage 3 ブラウザ）

## 採用技術と固定バージョン

Python 3.11.15（uv 管理）／ **gradio 6.22.0**（uv.lock 固定）／ **imageio-ffmpeg 0.6.0**（同梱 ffmpeg 7.1）／ **pillow>=11**（`verify_png` の明示依存）／ pytest 9.1.1。
生成側（P2 で使用）: DiffSynth-Studio 既存 venv（torch 2.13.0・av 18.0.0 ほか）— **無変更で外部から利用**。

## 実機で確認済みの事実（変更禁止の根拠）

- 約2.33秒（56f）・約5.17秒（124f）は 576×320・24fps で正常生成済み
- Turbo LoRA は 4ステップ・8ステップとも動作（同一 LoRA ファイル・alpha=1）
- **10秒一発生成（243f）は 576×320 で白黒3×3分割になり使用不可**（4/8step・VAEタイル変更でも再現）
- 長尺は 2.33/5.17秒クリップの継続生成（最終フレーム→先頭キーフレーム）＋連結で作る
- 最終フレーム＋同一 seed＋同一キャラ・声質記述で、キャラクターと声の**近似**継承が可能（完全一致は保証されない。MPS では seed 同一でもビット再現は保証されない）
- 連結部では親の最終フレーム＝子の先頭フレームが同一画像となり2フレーム連続する（24fpsで1フレーム約41.7ms。同一画像の表示は合計約83.3ms、重複による追加停止は約41.7ms）。V1では自動トリミングしない
- 実機の `-c copy` 連結は Non-monotonic DTS 警告 → V1 既定は PTS正規化つき再エンコード連結
- FFmpeg 再エンコード連結・正確な最終フレーム抽出（index 55/123）を P0 で検証済み
- `DIFFSYNTH_SKIP_DOWNLOAD=True` は DiffSynth 公式のオフライン機構（ソース確認済み）
- **DiffSynth-Studio 側は無変更**（全作業を通じてマーカー比較で検証済み）

## 重要な設計判断

- **2プロセス分離**（決定D1）: UI（アプリvenv・torch非依存）と生成ワーカー（DiffSynth venv）
- **成果物は原子的昇格**（§10.7）: partial→検証→`os.replace()`。**履歴 SUCCESS は昇格後のみ**。DONE が成果物を報告しない／実在しない場合は SUCCESS にせず FAILED にする
- **履歴の .bak** は「起動時点で検証済みのスナップショット」。破損した現行ファイルで上書きしない
- **JobQueue は HistoryStore に直接依存しない**（`JobRecorder` プロトコル越し。統合層が配線）
- **パス境界**: JobSpec・Engine・UI は絶対パス、履歴は data_root 相対。`to_absolute()` は data_root 外を返さない
- **状態機械**: ジョブは必ず RUNNING を経てから FAILED になる（`QUEUED→FAILED` は意図的に不許可）
- 完全オフライン（`DIFFSYNTH_SKIP_DOWNLOAD` / `PYTHONDONTWRITEBYTECODE` / preflight 事前検査）
- Generation Backend 境界（§22）: V1 は minimax_h3 専用のまま、config/履歴/プロトコルにだけ交換点を確保

## ディレクトリ構成（要約）

```
app/main.py              エントリポイント（--check / --mode / --deep-check / --smoke）
app/core/contracts.py    ★全層が共有する契約（型・遷移表・検証）
app/core/app_service.py  統合層（UI が触る唯一の窓口）
app/core/history.py      履歴 v1.2（原子的保存・復旧・チェーン解決）
app/core/job_queue.py    単一ジョブキュー（直列ディスパッチャ）
app/core/                config・applog・naming・fileops・ffmpeg_ops・mock_assets・preflight
app/engine/base.py       Engine 共通契約（Protocol）
app/engine/mock_engine.py  MockEngine（実機と同形のイベント）
app/engine/real_engine.py  実機エンジン（プロセス管理・検証・昇格）
app/engine/backends/minimax_h3/h3_worker.py  実機ワーカー（DiffSynth venv で動く自己完結）
app/ui/minimal.py        UI（新規生成タブ＋②〜④の骨格）
scripts/real_stage_test.py  実機試験（stage0/1/2。pytest には含めない）
config/config.toml       [engine] mode/backend ＋ [backends.minimax_h3]
data/                    出力・履歴・ログ（唯一の書き込み先・gitignore）
tests/                   388テスト（実モデルは使わない）
```

## 変更禁止領域

- `~/AI/DiffSynth-Studio` 全体（本体・models・.venv・既存スクリプト）— 読み取り専用
- `reference_scripts/` — 参照のみ
- V1 固定仕様（576×320・24fps・56/124f・4/8step・直列1本・localhost限定）

## 現在残っている警告・未解決事項

- `config.toml` の既定は `engine.mode = "real"`。モックで動かすには `./scripts/start.sh --mode mock`
- `estimates.init_sec` は実測値（約5秒）へ更新済み
- **S9（Wi-Fi 断での完走）は未実施＝ユーザーによる手動確認項目**。P2 では代替として (1) worker.log の `[√] Skip download and load only pre-downloaded model files` (2) モデルファイルの mtime/サイズ変化ゼロ、で追加ダウンロードが無いことを確認した
- 再起動中にアプリを終了すると体感の終了時間が10〜15秒になりうる（無応答ワーカーの terminate 待ちが積み上がるため。孤児ワーカーは残らない）

## V1 完成後の状態（P5 完了時点）

**コード上は V1.0.0 Release Candidate。** 残るのは**実 iPhone での確認**だけ（QR読取・PIN入力・動画再生・生成投入）。
それが済めば V1 完成。**Git 管理はユーザーが全確認後に開始する方針のため、commit・tag・push・remote 設定は一切行っていない**（リポジトリにコミットは1件もない）。

### V2 以降の候補（V1 には入れない）

- 10秒/15秒の一発生成（576×320・243フレームは白黒3×3分割になる既知の破綻）
- 任意解像度・並列生成・モデル選択UI・プラグイン機構
- 外部公開・クラウド同期・ユーザー認証基盤・外部API
- HTTPS 対応（LANモードは HTTP のみ。敵対的ネットワーク向けではない）
- 送信元IP別のログイン失敗カウンタ（現在は全クライアント共通のため、LAN内から30秒未満の間隔で誤PINを打ち続けると正規ユーザーもログインできない）

## P4 で確定した事項（重複フレーム）

**結論: 自動除去は採用せず既定 OFF。** 実機測定（白狐探偵の親子）で、親の最終フレームと子の先頭フレームは**ピクセル一致しない**ことが判明した（平均差10.222 / 最大差221）。理由は、キーフレームが VAE latent の条件アンカーとして注入されるだけで出力ピクセルへコピーされないため（DiffSynth ソースで確認）。詳細は設計書 §10.6.1。比較の仕組み（`compare_frames`）と config フラグは残してあり、判定結果は必ず `warnings` に記録される。当初の検討事項は以下のとおりで、すべて満たしたうえでこの結論に至った:

- 親の最終フレームと子の先頭フレームを**比較してから**除去する。**無条件に削除しない**
- 判定は完全一致または安全側の類似判定。**閾値は実機素材で検証してから決める**（推測値で実装しない）
- 24fps で1フレーム＝**約41.7ms**。同一画像2フレームの表示は約83.3ms、重複による追加停止は約41.7ms
- 複数連結時の**映像・音声同期**を実素材で検証する
- **音声の冒頭を不用意に切らない**（子クリップの語頭が欠ける危険）
- 自動除去を**無効化できる安全なフォールバック**（config フラグ）を残す
- 判定・除去の実施有無を記録し、後から検証できるようにする
- **最終判断は、実際の継続生成素材が揃う P4 で行う**

## 次の AI エージェントが最初に確認すべきこと

1. `CLAUDE.md` の絶対制約（DiffSynth 読み取り専用・V1固定仕様・フェーズ境界）
2. 本書の「V1 完成後の状態」と「残っている警告・未解決事項」
3. `.venv/bin/python -m pytest tests/ -q` が **1170 passed + 1 xpassed** であること
4. `./scripts/start.sh --check --mode real --deep-check` が合格すること（実機資産が揃っているか）
5. 設計書 §22.2（バックエンド契約）・付録A（ワーカープロトコルと A.1 の2層構造）・§10.7（原子的保存）・§13.3（fatal 分類）— P2 実装の直接仕様
6. `app/core/contracts.py` — 全層の契約。ここを変えると全層に波及するので慎重に
