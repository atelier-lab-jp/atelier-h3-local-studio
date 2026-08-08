# changelog.md — ATELIER H3 Local Studio 変更履歴

時系列の変更履歴（新しいものを上に追記）。現在状態の正本は `ai_summary.md`。
未公開プロジェクトのためリリース番号は付けず、フェーズ表記で管理する。

---

## [Unreleased] 2026-08-08 — P5: V1総仕上げ・LANモード・残件修正

### Added
- **iPhone接続モード（LANモード）** — `python -m app.main --lan`（または `ATELIER H3 Studio LAN.command` をダブルクリック）でのみ有効。**config や環境変数では有効化できない**（設計書 決定D13）
- `app/core/network.py` — RFC1918 の private IPv4 だけを許可する検出・検証（`en0`→`en1`→名前昇順の予測可能な選択）。public IP・`0.0.0.0`・link-local・**CGNAT(100.64/10)**・IPv6・ホスト名・URL を日本語で拒否。検出できなければ `0.0.0.0` へフォールバックせず中止
- `app/core/lanauth.py` — 起動ごとのランダムPIN（`secrets`）、HMAC ダイジェストによる**定数時間比較**、連続失敗のロックアウト。`__repr__` を伏字にし PIN を露出しない
- `app/core/qrgen.py` — `segno` による**完全ローカル**QR生成。**URL のみ**を符号化し、クエリ・フラグメント・userinfo を関数が拒否。PNG は `0o600`・正常終了時に削除
- `scripts/ATELIER H3 Studio LAN.command` — iPhone接続モード用のダブルクリック起動（通常用も改良）
- **iPhone 向けレスポンシブUI** — `@media (max-width: 640px)` の中だけで1カラム化・44pxタップ領域・局所横スクロール
- **二重投入防止** — UIのボタン無効化＋サーバ側2秒の冪等化（履歴・JobSpec には何も残さない）
- **キーフレーム寸法検証** — ワーカー自身が PNG形式・576×320ちょうどを確認し、違反は `input` 系の**非fatal**（モデルを捨てない）
- `config/config.example.toml`（個人名・絶対パスなし）、`[lan]` セクション（**有効化キーは持たない**）
- 起動前チェックに LANモード用の項目（private IPv4 検出・ポート空き・日本語の対処案内）
- テスト367件追加

### Changed
- `docs/v1-design.md` を **v1.5** へ（§15.1 LANモードと脅威モデル、§10.6.2 連結検証式、§19.1 受け入れ基準18〜30）
- `README.md` を初心者向けに全面書き換え（LANモード・QR・PIN・iPhone操作・トラブルシューティング）
- **8ステップの所要時間表記を「約2倍」→「約1.67倍」へ全面修正**（P3実測。設計書・UI・configの3箇所）
- `.gitignore` を大幅に拡張。`config/config.toml`（絶対パスを含む）を除外し、`config.example.toml` を公開用に。`setup.sh` が config.toml 不在時にひな形から複製（既存があれば絶対に上書きしない）
- 日本語文言の総点検（内部用語・例外文を画面から排除し「詳しい情報」へ分離。エラー分類とジョブIDは調査用に残す）

### Removed
- **`concat.reencode` を config から排除** — `false`（`-c copy`）は配線されておらず、動かない設定が選べる状態だった。書かれていたら日本語の ConfigError。`concat_copy()` は比較検証用にコードだけ残す

### Fixed（相互レビュー由来）
- **取消・失敗の直後に同じ内容を再投入すると「登録済み」と出るのに1件も走らなかった** — 冪等化がキャッシュ済みジョブの生死を見ていなかった。二重タップ防止は維持したまま、正当なやり直しを通すよう修正
- **`dedupe_boundary_frame = true` で10本以上の連結が必ず失敗した** — 映像だけ1フレーム落とすと音声との差が量子化幅を超え ffmpeg が埋め戻すため、検証式が破綻していた。上限に除去枚数を加算して修正。あわせて「除去します」という**実態と異なる報告**を「除去を試みます（実際に減る枚数は依頼より少ない）」へ訂正
- **iPhone接続モードの `.command` が起動前チェックを `--lan` 抜きで実行**していた。LAN固有の失敗を事前に検出できず、合格表示の直後に別の失敗が出ていた
- **PIN バナーが端末以外へ出力すると表示されなかった**（8KBバッファ。`> log.txt` や `| tee` で PIN が見えずログインできない）→ `flush=True`
- **MockEngine がキーフレームの寸法・PNG形式を見ておらず**、モックで通るのに実機で失敗する乖離があった → 実機と同一条件・同一文言に統一
- **ワーカーのエラーに内部クラス名（`InputValidationError:`）が前置され**、履歴と画面にそのまま出ていた → 入力エラーは日本語のみ
- QR のターミナル表示の余白が規格の半分（2モジュール）で iPhone カメラの読み取りが不安定になりうる → 規格どおり4へ
- `auth_lockout_sec = 0` でレート制限が丸ごと無効化できた → 下限を1秒に
- `img { height: auto }` が Mac にも効き Gradio のアイコン高さを上書きしうる → iPhone のときだけに限定
- 起動のたびに Gradio の英語 UserWarning が出ていた → その1件だけ抑止

### Known limitations
- **HTTPS ではない。** 同一LAN上で盗聴できる相手には PIN も動画も見える。**信頼できる家庭内LAN専用**であり、カフェ・公共Wi-Fi・共用オフィスでは使わないこと
- ログイン失敗のロックアウトは全クライアント共通。LAN内から30秒未満の間隔で誤PINを打ち続けると正規ユーザーもログインできない（LAN限定の脅威モデルでは許容）
- 重複1フレーム自動除去は**既定 OFF のまま**。有効にしても ffmpeg が埋め戻すため依頼した枚数は減らない（P4の実測結論を維持）
- Gradio の `gr.Blocks(css=...)` は非推奨。動作はするが、将来 `launch(css=...)` へ移す必要がある
- 実 iPhone での QR読取・PIN入力・再生・生成投入は**未確認**（エージェントが実行できないため）

---

## [Unreleased] 2026-08-08 — P4: 継続生成・全チェーン連結・完成動画／履歴UI

### Added
- **継続生成** — 成功動画から「この動画の続きを作る」。親の正式な最終フレーム PNG を先頭キーフレーム（`keyframe_indices=[0]` 固定）として渡す。実証スクリプト `run_h3_mac_turbo_4step_clip2_samevoice.py` の方式を移植
- **継続元の多層検証** — AppService（投入時）→ RealEngine/MockEngine（`validate_keyframe`）→ h3_worker（data_root 境界・PIL 検証）。未成功・backend/実行方式/解像度の不一致・PNG 欠損/破損・チェーン深さ上限を**投入した瞬間に日本語で拒否**
- **`HistoryStore.resolve_concat_chain()` / `mark_concat()`** — root→選択ノードの解決に加え、SUCCESS 以外・backend/model/解像度/fps 非互換・成果物欠損・2本未満を拒否。履歴スキーマは**変更なし**
- **`app/core/concat_service.py`** — 連結をバックグラウンドで単一実行。partial→検証→`os.replace()` 昇格後にのみ履歴へ記録。失敗時は正式名も孤児 partial も残さない
- **`app/core/reveal.py`** — Finder 表示（`["open","-R",path]` の引数配列・`shell=True` 不使用・data_root 境界）
- **`ffmpeg_ops.compare_frames()` / `concat_reencode(trim_first_frame_of=...)`** — 重複フレームの比較と除去（**必ず比較してから除去**。既定 OFF）
- **③完成動画タブ / ④履歴タブ** — 一覧・プレビュー・メタ表示・継続生成・連結・Finder 表示・状態フィルタ・異常データ表示
- **継続バナー** — 親ID・サムネイル・設定表示、seed/長さ/ステップ/プロンプトのプリフィル、継続モード解除
- config: `concat.dedupe_boundary_frame`（既定 false）／`dedupe_max_mean_diff`／`dedupe_max_max_diff`
- テスト178件追加（継続生成・チェーン・連結・重複除去・Finder・UI）

### Changed
- `docs/v1-design.md` を **v1.4** へ。§10.6.1 を実測で全面改訂、付録A の `keyframe_path` を P4 仕様へ更新
- `AppService` に `completed_videos` / `history_rows` / `continuation_context` / `start_concat` / `concat_status` / `reveal_in_finder` を追加

### Fixed（相互レビュー由来）
- **チェーン深さ上限（20）に達しても継続を受け付けていた** — 21本目は生成に14分かかるうえ、以後 `resolve_chain` が常に失敗し**二度と連結できない動画**になる。投入時に拒否するよう修正
- **mock で作った動画を real の継続元にできた**（`execution_engine` が互換条件に無かった）
- **`_chain_length()` が解決失敗時に 1 を返し**「親IDがあるのにチェーン長1」という矛盾表示になっていた（None を返すよう修正）
- **Finder: 連結ファイルが欠損すると個別動画が黙って開いた**／**data_root 内の履歴JSON・ログも開けた** — 一覧からサーバ側で解決した正式成果物のみを渡すよう修正
- 連結行に対する再連結の抑止、「連結できません」の二重表示、解像度不一致メッセージへの実測値併記

### Verified（実機・白狐探偵・Mac mini M4 24GB）
- **S6**: 親 124f/4step・seed 505406688 = **806.4秒**成功／子（継続生成・同一 seed・親の最終フレーム）= **845.4秒**成功。`parent_id` と `type="continuation"` を履歴へ記録
- **S7**: 全チェーン連結 = **248フレーム・10.33秒**。H.264 High/yuv420p/576×320、AAC 32000Hz ステレオ、**A/V skew +0.030s**、source の size/mtime 不変
- **S11**: Finder 表示成功、`/etc/hosts` は日本語で拒否
- HTTP: 親・子・連結とも **206 / video/mp4**、履歴JSON・ログは **403**
- **重複フレーム測定**: 親 `_last.png` vs 子 frame0 = **平均10.222 / 最大221**、同条件比較 = **平均9.762 / 最大200**
- 全テスト **625 passed + 1 xpassed**。`setup` / `--check` / `--smoke` 合格
- DiffSynth-Studio 無変更、孤児 partial なし、ユーザーの既存動画・履歴は無変更

### Known limitations
- **重複1フレーム自動除去は採用せず既定 OFF。** キーフレームは VAE latent の条件アンカーで出力ピクセルへコピーされないため（DiffSynth ソースで確認）、親子の境界はピクセル一致しない。実測差（平均9.8〜10.2）は「別の絵」（13.5）と同オーダーで、一致判定が成立する閾値は存在しない。比較の仕組みと config フラグは残してあり、ON にしても安全に通常連結へフォールバックする
- 映像のみ1フレーム除去すると A/V が 41.7ms ずれる（ITU-R BT.1359 の映像先行検知閾値 45ms の直前）
- ~~`concat.reencode = false` は未配線~~ → **P5 で config から排除**（V1 は常に再エンコード）
- ~~長いチェーンで duration 許容 0.5 秒が不足しうる~~ → **P5 で境界数比例の式へ修正（§10.6.2）**
- P5（LANモード・QR・PIN・モバイル最適化・初回コミット）は未着手

---

## [Unreleased] 2026-08-07 — P3: キューと進捗の仕上げ

### Added
- **②キュータブ**（`app/ui/minimal.py`）— 現在の処理（ID・概要・長さ・ステップ・seed・状態・ステージ・進捗バー・経過・目安）／エンジン状態（再起動待ち（残り秒）・再初期化中・停止中を含む）／待機一覧＋取消ドロップダウン／直前の失敗の分類表示／[ワーカーを再起動]（確認チェック必須）／赤色バナー（HALTED・受付停止）。api: `/on_queue_tick` `/on_cancel_queued` `/on_restart_worker`
- **「最終処理中」表示** — `step == total_steps` かつ `GENERATING` かつ20秒無音で切り替え。実機の 4/4 到達後の約150秒（VAE・音声デコードが `pipe()` 内部で進む区間）をハングに見せない
- **自動再起動・バックオフ・HALTED**（`app/core/job_queue.py`）— 連続失敗カウント（SUCCESS で0リセット）、バックオフ 5秒→30秒、READY 到達まで次ジョブを開始しない、上限超過で HALTED（待機ジョブは QUEUED のまま保持）
- **手動再起動** `JobQueue.restart_worker()` / `AppService.restart_worker()` — 要求を置くだけでディスパッチャが実行する構造（再起動経路を1本に保ち UI をブロックしない）。HALTED からの復帰も兼ねる
- **stalled watchdog** — 総経過時間 vs 目安×係数で判定（イベント間隔では判定しない）。警告のみで停止しない
- **ディスク容量ガード** — 投入時に加えディスパッチ直前・生成中（1秒間隔）にも再評価。受付停止中はジョブを QUEUED のまま保持
- config: `estimates.stall_abort_factor`（既定 0.0＝強制終了を無効化）
- `scripts/real_stage_test.py` に P3 の実機ステージ `s3`（124f/4step）・`s4`（56f/8step）・`s8`（生成中 SIGKILL → 自動復旧）を追加
- 設計書 §10.6.1「P4 検討事項: 連結部の重複1フレーム自動除去」を新設

### Changed
- `app/core/contracts.py` — `RestartState`（idle/backoff/restarting/halted）、`JobView.last_event_at` / `stalled`、`QueueSnapshot` に再起動・受付停止の6フィールドを追加
- `app/engine/base.py` — `Engine.restart()` の docstring に P3 で確定した契約（中断 ERROR 先出し・READY 再送・イベントキュー非再作成・shutdown 後は `EngineBusyError`）を明記
- `MockEngine` の `[MOCK_FAIL]` を `category=pipeline` → **`input`** へ（`fatal=False` と `pipeline`（§13.3 では fatal）の組み合わせが自動再起動の判定と矛盾していた）
- `tests/test_preflight.py` — 総合判定テストが**ポート7860の空きに依存**していたため空きポートを注入（開発者がアプリを起動しているだけで落ちる脆弱性）

### Fixed（相互レビュー由来）
- **`MockEngine.restart()` の既知問題を解消** — 生成中に呼ぶと実行中ジョブが終端イベントなしで消え、ディスパッチャが永久に待ってキューが停止していた。中断 `ERROR(fatal=True, worker_dead)` を先に発行し、イベントキューを作り直さない RealEngine と同一契約へ統一
- **RealEngine の再起動で偽の `worker_dead` が出うる経路**（旧世代スレッドが新世代のキューへ終了マーカーを積む）
- **RealEngine の Popen 失敗エラーが再起動成功後に消費され、健全な新ワーカーを落とす経路**（`_is_stale_fatal_during_restart` で解消）
- **①新規生成タブがバックオフ中に「生成エンジンが停止しました」と誤警報**（同一画面のヘッダは「再起動待ち」を表示しており矛盾していた）
- **実行中に終端イベントが失われるとジョブが RUNNING のまま永久に残る**（`_idle_step` と対称の DEAD 検知を実行ループにも追加）
- 生成中（約400秒）に受付可否が一度も再評価されず、ディスク枯渇バナーが最大1ジョブ分古くなる問題
- 取消ドロップダウンのキャッシュがプロセス全体で1つだったため、ブラウザ再読込後に候補が空のままになる問題

### Verified
- 全テスト **447 passed + 1 xpassed**（P2 の 387+1 から60件増）。実モデルは一切使わない（fake worker・stub runtime・MockEngine・時計/sleep 注入）
- `setup.sh` 成功。`--check` は**ポート使用中を正しく検出**（ユーザーのアプリ稼働中のため）。smoke 相当は空きポートで HTTP 200 合格
- DiffSynth-Studio 無変更、ユーザーの既存成果物・履歴とも無変更（削除・上書きなし）
- **実機試験（ユーザーがアプリを終了した後に直列実行）**:
  - **S3** 124f/4step = **819.0秒**（目安比 1.00）／**S4** 56f/8step = **677.6秒**（8step は 4step の**約1.67倍**と判明。想定2.0倍より速い）
  - **S8** 生成中 SIGKILL → 1本目 `worker_dead` で FAILED（成果物なし）→ **backoff → restarting → idle** を観測 → ワーカー PID 変化 → 2本目 400.7秒 SUCCESS。orphan なし
  - **ブラウザ②キュータブ**: 直列処理・進捗/経過/目安・待機一覧・**UI からの取消 → 履歴 CANCELED**・**手動再起動 → 実行中ジョブ FAILED(worker_dead) → READY 復帰**・動画配信 HTTP 206
  - 実測を `estimates` へ反映（56f/4step=403 / 124f/4step=819 / step8_factor=1.67）。更新後の目安は実測比 1.00〜1.01

### Known limitations
- 124フレーム・**8ステップ**の組み合わせは未実測（S3 で 124f/4step、S4 で 56f/8step は実測済み。`step8_factor=1.67` からの外挿）
- 再起動中にアプリを終了すると体感の終了時間が10〜15秒になりうる（無応答ワーカーの terminate 待ちが積み上がるため。孤児ワーカーは残らない）
- P4（継続生成・連結・③④タブ完成版）は未着手

---

## [Unreleased] 2026-08-07 — P2: 実機エンジン（RealEngine + h3_worker）

実モデル（MiniMax-H3-NF4 + Turbo LoRA）でブラウザから動画生成できる状態にした。

### Added
- `app/engine/backends/minimax_h3/h3_worker.py` — DiffSynth venv で動く自己完結ワーカー。`app.*` に依存せず、torch/diffsynth は遅延 import（アプリ venv でもプロトコル層をテストできる）。実証スクリプトの構成を一字一句移植（vram_config 全 disk・bfloat16・mps・`vram_limit=0`・LoRA alpha=1）。JSON Lines プロトコル、入力の再検証、`progress_bar_cmd` ラッパ、partial 保存、例外分類、ジョブ後の `gc.collect()` + `torch.mps.empty_cache()`
- `app/engine/real_engine.py` — P1 の Engine 契約を満たす実機エンジン。`shell=False` の subprocess 起動（cwd=DiffSynth-Studio）、stdout/stderr の独立 drain スレッド、`@@EVT ` 行のみ解析、handshake 照合（backend/model/capabilities）、**partial の検証と正式名への原子的昇格**（昇格後にのみ DONE）、ワーカー終了検知と `worker_dead` 合成、shutdown（grace→terminate→kill）、`worker.log`（アプリのログ階層に載せない）
- `tests/fixtures/fake_h3_worker.py`（23シナリオ）と `tests/test_real_engine.py`（40件）
- `tests/fixtures/stub_runtime_worker.py` + `tests/test_p2_integration.py`（13件）— **実 h3_worker.py を実 RealEngine で駆動**し、DiffSynth ランタイムだけスタブ化してワイヤの噛み合いを実モデルなしで検証
- `tests/test_h3_worker.py`（163件）
- `scripts/real_stage_test.py` — 実機試験（stage0/1/2）。pytest には含めない
- preflight にワーカースクリプトの存在検査

### Changed
- `config/config.toml` — 実機試験合格後、既定を `engine.mode = "real"` へ。`estimates.init_sec` を実測値（約5秒）へ修正
- `app/core/app_service.py` / `app/main.py` — real モードで RealEngine を構築。MockEngine を real として記録させない双方向ガード。`--smoke` は実機モデルを読まないよう mock を強制
- `app/core/fileops.py` — 孤児列挙にワーカーの隠し中間ファイル `.*.tmp.mp4` を追加
- `docs/v1-design.md` — v1.3。付録 A.1（2層構造）・A.2（`pong` ワイヤ制御）・A.3（ワーカー起動時の環境変数）を追加。generate コマンドを partial 明示へ、イベント種別「7種」→5種、`worker_dead` の出所、`MODELSCOPE_DOMAIN` を「設定しない」で確定。§10.7 に PyAV の拡張子制約を追記。§0.3 の実測値を更新

### Fixed（相互レビュー由来）
- **ランダムシードで実機生成が100%失敗するバグ** — UI 既定の「シードをランダム」は `seed_requested=None` を渡すが、RealEngine が素通しで送るためワーカーの整数検証で必ず弾かれていた。MockEngine と同じくエンジン層で採番する形に統一
- **UI のステップ進捗バーが一度も表示されない P1 バグ** — `entry.stage` はディスパッチ時に必ず `PREPARING` が入るのに、PROGRESS 処理が `if entry.stage is None` を条件に `GENERATING` を設定していた。JobQueue の1行修正で real/mock 両方を修正（プロトコルは増やさない）
- 保存段階の失敗（ディスク満杯・サイズ0）が fatal に誤分類され、健全なモデルを捨てて5分の再初期化を強いていた → 非 fatal へ
- ワーカーが `preparing` ステージを出さず、生成開始から最初の進捗まで UI が無反応だった
- 報告された partial パスが指示と異なる場合に警告だけで昇格していた（`promote()` の `os.replace` が既存の完成動画を上書きしうる）→ 拒否へ
- `scripts/real_stage_test.py` の `engine.ping(timeout=...)`（シグネチャ不一致で初期化直後に落ちる）と `getattr(engine, "pid")`（常に None で **Stage 2 の常駐再利用検証が空振り**）
- `shutdown_grace` を 10秒→2秒へ（実機ワーカーは生成中 stdin を読まないため必ず terminate 経路になり、P1 の Ctrl+C 応答が劣化していた）
- ワーカーに `sys.dont_write_bytecode = True`（DiffSynth 配下への `__pycache__` 生成の二重防止）

### Verified（実機・Mac mini M4 24GB）
- **Stage 0**: モデル初期化 4.8秒 → `259 tensors are patched by LoRA` → READY → ping/pong → shutdown（orphan なし）。worker.log に `[√] Skip download and load only pre-downloaded model files`、モデルファイルの mtime/サイズ変化ゼロ
- **Stage 1**: 56f/4step を **405.8秒**で生成。H.264 High/yuv420p/576×320、AAC 32000Hz ステレオ、56フレーム・2.33秒、最終フレーム PNG 576×320。進捗 1/4→4/4 を実機で取得
- **Stage 2**: 同一ワーカー（**PID 18203 が同一**）で2本目 401.1秒（比 0.99）。モデル再初期化なし。ランダムシード経路も実機で成功（`seed_used=57701299`）
- **Stage 3**: ブラウザ UI から投入。**投入コールバックの応答 0.06秒**（非ブロッキング）、初期化表示 →`preparing`→`generating` 1..4 →完成、HTTP 206 `video/mp4` で配信、履歴に `execution_engine="real"`
- ワーカー RSS: 初期化後 約2116MiB → 生成中 約4225MiB。**2本目も横ばい**（蓄積なし）
- 全テスト **387 passed + 1 xpassed**。`setup.sh` / `--check`（real・mock・deep-check）/ `--smoke` すべて成功
- **DiffSynth-Studio は全作業・実生成3本を通じて変更ファイルゼロ**

### Known limitations
- **S9（Wi-Fi 断での完走）は未実施＝ユーザーによる手動確認項目**。代替として追加ダウンロードが無いことを2通りで確認済み
- 生成中は step 4/4 のまま約2.5分続く（デノイズ後の VAE・音声デコードが `pipe()` 内部で進むため進捗が出ない）
- 124フレーム・8ステップの実測は P3
- fatal 後の自動再起動・②キュータブ完成版・停滞警告は P3。継続生成・連結UIは P4
- `MockEngine.restart()` は稼働中に呼ぶと実行中ジョブが終端イベントなしで中断される（P1 からの既知事項。P3 で RealEngine と同じ方式へ揃える）

---

## [Unreleased] 2026-08-07 — P1: 履歴・単一キュー・Engine契約・MockEngine・新規生成タブ

実モデルを使わずに「入力 → キュー → 生成 → 原子的昇格 → 履歴 → ブラウザ再生」の全フローが成立する状態にした。

### Added
- `app/core/contracts.py` — 全層が共有する契約（JobStatus と遷移表、JobSpec / JobView / QueueSnapshot、EngineEvent、BackendIdentity / Capabilities、`JobRecorder` プロトコル、入力検証 `validate_job_spec`、`resolve_seed`）
- `app/core/history.py` — 履歴ストア v1.2。原子的保存（一意tmp→`os.replace()`＋fsync）、検証後のみ `.bak` 更新（プロセス内1回）、破損の隔離退避と `.bak` 検証つき復旧、起動時 QUEUED/RUNNING→INTERRUPTED、親子チェーン解決（欠損親・循環・深さ20）、data_root 相対保存
- `app/core/job_queue.py` — 単一ジョブキュー。daemon ディスパッチャ1本、FIFO、同時 RUNNING 最大1、QUEUED取消、上限、遷移検証、失敗後も継続、不変 snapshot、安全な shutdown
- `app/engine/base.py` — Engine 共通契約（`Protocol`: identity / capabilities / state / start / submit / poll_event / shutdown / restart）と `backend_identity()`
- `app/engine/mock_engine.py` — MockEngine。実機と同順のイベント、capabilities 固定値、4/8ステップ progress、56/124f 素材選択、seed 採番と返却、`[MOCK_FAIL]` 失敗注入、成果物の原子的昇格（両方昇格後にのみ DONE）、`sleep_fn` 注入でテスト高速化
- `app/core/app_service.py` — 統合層。ID採番（衝突回避）、JobSpec 生成と検証、`HistoryRecorder`（JobQueue→HistoryStore の配線）、空き容量ガード、冪等な `start()`
- `app/ui/minimal.py` — ①新規生成タブ最小完成版（プロンプト・長さ・ステップ・seed・目安時間・投入・状態/待機数/進捗/経過・完成動画プレビュー・詳細ログ）。Timer ポーリング、非ブロッキング投入、完成動画は State 変化時のみ差し替え
- テスト130件追加（履歴38・キュー38・エンジン40・結合8・UI経路6）。結合テスト 17.2-1 / 17.2-2 と `[MOCK_FAIL]` 混在ケース、gradio_client による HTTP 経由の UI 駆動テストを含む

### Changed
- `app/main.py` — AppService の構築・起動・停止を配線。`allowed_paths` を成果物ディレクトリ（outputs / concat）のみに限定し、履歴JSON・ログを配信しないようにした
- `app/core/ffmpeg_ops.py` — `_video_validator` を公開 `video_validator()` に昇格し、MockEngine と共通化（検証の重複と将来の乖離を解消）
- `app/core/config.py` — `speed_factor > 0` の検証、`data_root` を常に `resolve()`、型判定の可読性改善
- `pyproject.toml` — `pillow>=11` を明示依存に追加（`fileops.verify_png` が直接使用）
- `docs/v1-design.md` — §11.2 に `error_category` を追記、`seed_used` を「成功時に確定（それ以外は null）」へ是正、付録 A.1（プロセス間 JSON Lines は partial パス／プロセス内 EngineEvent は昇格後の正式パス）を追加。フレーム重複の記述を「同一画像2フレーム＝約83.3ms 表示、追加停止 約41.7ms」へ精密化

### Fixed（相互レビュー由来）
- **DONE が成果物を報告しない／実在しない場合に SUCCESS が確定していた** — 予定パスでのフォールバックを廃止し、報告なし・ファイル不在はいずれも FAILED にした（「履歴 SUCCESS は昇格後のみ」の実効化）
- `job_id` を欠いた DONE / PROGRESS を実行中ジョブのものとして扱っていた（DONE・PROGRESS は厳密一致を要求）
- 履歴の保存失敗時にメモリとディスクが乖離していた（`add` / 状態更新 / 起動時復旧をロールバック）
- 履歴 tmp ファイル名が固定で、同一 data_root の複数インスタンスが衝突すると保存が黙って失敗しえた（PID+乱数で一意化）
- セッション中に `load()` を再実行すると `.bak` が実行中スナップショットに汚染された（プロセス内1回に限定）
- `to_absolute()` に境界検証がなく、手編集・破損した履歴の絶対パスや `..` が UI へ出えた（data_root 外は None）
- 初回作成時の書き込み失敗が例外として素通りしていた（警告に落として起動を止めない）
- UI の Timer コールバック（`on_tick` / `on_latest_changed`）が無防備で、一度例外が出ると画面更新が恒久的に止まっていた（§13.2 準拠の例外ガード）
- `AppService.start()` が冪等でなく、再実行すると実行中ジョブを INTERRUPTED に落としていた
- `AppService.build(mode="real", engine=...)` がモック成果物を `execution_engine="real"` として記録しえた（mode を無条件検証）
- 停止処理中にディスパッチャがエンジンへ投入し、「終了しただけ」のジョブが FAILED として記録されえた
- MockEngine で PNG 昇格に失敗すると履歴に載らない孤児 MP4 が残りえた（昇格済み MP4 を撤去）
- ヘッダと進捗が別々のスナップショットを読み、同一 tick 内で表示が食い違いえた（単一スナップショット化）

### Verified
- 全テスト **166 passed + 1 xpassed**
- `setup.sh` 成功 ／ `start.sh --check`（mock・real・deep-check）合格 ／ `--smoke` HTTP 200
- **ブラウザ相当の実配信**: 動画 HTTP 206・`video/mp4`（レンジ取得）、履歴JSON・ログ・data_root 外はいずれも 403
- **Ctrl+C**（実端末相当の SIGINT）で exit 0・5.2秒で停止、非デーモンスレッドの残留なし
- 実データ領域で 56f/4step の生成が完走し、`data/history.json` が v1.2 スキーマ・UTF-8・data_root 相対で記録された
- DiffSynth-Studio 側無変更（マーカー比較）

### Known limitations
- 実モデル生成は未実装（P2）。`engine.mode = "mock"` 固定
- ②キュー / ③完成動画 / ④履歴タブは骨格のまま（P3・P4）
- `MockEngine.restart()` を稼働中に呼ぶと実行中ジョブが終端イベントなしで中断される（P1 では呼ばない。P3 で対応必須。docstring に警告記載）
- `JobQueue.submit()` は通知順序保証のため履歴書き込みをロック保持中に行う（数百件規模では体感差なし）

---

## [Unreleased] 2026-08-07 — P0.5: 開発継続基盤と将来拡張境界

### Added
- 開発継続文書 3点: `CLAUDE.md`（AI作業規約）／`ai_summary.md`（現在状態の正本）／`changelog.md`（本書）
- 設計書 §22「Generation Backend 境界」: Execution Engine（real/mock）と Generation Backend（minimax_h3）の用語分離、最小バックエンド契約（identity・lifecycle・capabilities・成果物契約）
- config: `[engine]`（mode / backend）と `[backends.minimax_h3]` セクション（model_id / model_revision / worker_script 等の identity を含む）
- preflight: 未登録 backend_id の日本語エラー拒否＋登録済み backend の情報表示
- backend 配置ディレクトリ雛形 `app/engine/backends/minimax_h3/`（h3_worker.py は P2 実装）
- テスト4件追加（engine.mode 不正値／[backends.*] 欠落／未登録 backend 拒否／登録 backend 表示）→ 36 passed + 1 xpassed

### Changed
- 設計書を v1.2 に更新（§7・§8・§11・§12・§13.1・§16・§17.1・§18・付録A・変更履歴）
- 履歴スキーマ: `engine` を `execution_engine` に改名し、`backend_id` / `model_id` / `model_revision` / `backend_params` を追加（実データ発生前のため互換処理なし・最も単純な形に整理）
- ワーカープロトコル: generate に `backend_id`、ready に `backend_id`+`capabilities`、done に backend/model 識別と `warnings` を追加（イベント種別は7種を維持）
- `config.toml`: 旧 `[app] mock_mode`・`[paths] diffsynth_root/worker_python`・`[models]` を新構造へ統合
- README に継続文書3点への案内を追加

### Verified
- 全テスト 36 passed + 1 xpassed
- `start.sh --check`（mock/real）合格、`--smoke` HTTP 200 合格（V1 挙動不変）
- DiffSynth-Studio 側無変更（マーカー比較）

### Known limitations
- V1 は minimax_h3 専用（モデル選択UI・複数バックエンド常駐・ホットスワップ・プラグイン機構は作らない）
- P1（履歴ストア・キュー・MockEngine 本実装）は未着手

---

## 2026-08-07 — P0 完了: 基盤ビルド

### Added
- アプリ専用 venv（uv 管理・pyproject.toml・uv.lock）— DiffSynth 環境と完全分離
- 採用固定バージョン: **Gradio 6.22.0**（実機スモーク合格でロック）／**imageio-ffmpeg 0.6.0**（同梱 ffmpeg 7.1・macOS arm64）／Python 3.11.15
- config 読込・検証（日本語エラー・V1固定値の強制）／回転ログ＋UIリングバッファ
- preflight（モデル4ファイル・processor・LoRA・既存venvパッケージ・ポート・ディスク2段階閾値。正常系/異常系とも日本語）
- 原子的ファイル操作（partial→検証→os.replace 昇格）／ID採番（`v_YYYYMMDD_HHMMSS_xxxx`）
- FFmpeg処理: モック動画生成（H.264/yuv420p/AAC・576×320・24fps）・正確な index 指定の最終フレーム抽出（select フィルタ）・PTS正規化つき再エンコード連結・デコード検証（ffprobe 非同梱のため ffmpeg デコードで duration 確認）
- モック素材（56f/124f＋最終フレームPNG）／最小 Gradio UI（4タブ骨格＋動作確認タブ）
- 起動スクリプト: `setup.sh`／`start.sh`（--check / --mode / --deep-check / --smoke）／ダブルクリック用 `.command`
- README・.gitignore

### Verified
- テスト 33件（32 passed + 1 xpassed）
- **FFmpeg 検証10項目合格**: バイナリ解決／-version／H.264/yuv420p/AAC 生成／56f・124f 生成（フレーム数完全一致）／index 55・123 の PNG 抽出／PIL で開けること／PTSリセット＋再エンコード連結／連結動画のデコード／duration 7.5秒±0.5 一致／元動画のサイズ・mtime 不変
- preflight 実機モード合格（モデル・LoRA・processor 実在、既存venvの torch/diffsynth/av/PIL import 合格・PYTHONDONTWRITEBYTECODE=1）
- ポート占有時の日本語エラー、`--smoke` HTTP 200
- **DiffSynth-Studio 側は完全無変更**（.venv 含むマーカー比較で確認）

### Known limitations
- 実機生成は未実装（P2）。config は mock モード
- `estimates.init_sec` は仮値／完全オフライン実機確認（S9）は P2 以降

---

## 2026-08-07 — 設計書 v1.1: レビュー7点反映

### Changed
- 連結の既定を **PTS正規化＋再エンコード**へ変更（実機 `-c copy` で Non-monotonic DTS 警告を確認したため）。`-c copy` は config の実験モードへ格下げ。連結部でのフレーム重複（親の最終フレームと子の先頭フレームが同一画像 → 2フレーム＝約83.3ms 表示、重複による追加停止は約41.7ms）を既知仕様として明記
- 全成果物の**原子的保存**を追加（§10.7 新設: partial→検証→os.replace 昇格。履歴 SUCCESS は昇格後のみ）
- history.json の**バックアップ順序を安全化**（検証後のみ .bak 更新。破損ファイルで正常バックアップを上書きしない。.bak も検証してから復旧）
- **完全オフライン保証**: `DIFFSYNTH_SKIP_DOWNLOAD=True`（DiffSynth 公式機構をソース確認）・`PYTHONDONTWRITEBYTECODE=1` を追加。MODELSCOPE_DOMAIN 非依存化。preflight にパッケージ検査追加
- error イベントへ **fatal / category** を追加。fatal 後はワーカーを再利用せず再起動（連続失敗カウント・成功でリセット・バックオフ 5→30秒）
- Gradio は「**P0で実機確認に合格した版をロック**」に方針変更（候補 6.22.0）
- 最終フレームのフォールバック抽出を**正確な index 指定**（select=eq(n,55/123)）へ変更
- ディスク閾値を警告20GB／受付停止5GBの2段階へ。ユーザー確認済み未決事項8件を確定（連結は全チェーン・自動削除なし・QUEUED取消あり・名称/ポート維持ほか）

---

## 2026-08-07 — 設計書 v1.0: 初期設計

### Added
- V1 全体設計 `docs/v1-design.md`（全21項目＋付録）
- UI／生成ワーカーの**2プロセス構成**（既存 DiffSynth venv 無変更・MPSクラッシュ隔離・モデル常駐再利用）
- 実機検証済み仕様の固定: 576×320・24fps・**56/124フレーム**・Turbo LoRA 4/8ステップ・直列1本
- 継続生成（最終フレーム→先頭キーフレーム・親seed継承）と音声付き連結
- ローカルJSON履歴・単一ジョブキュー・進捗表示・モックモード・localhost限定セキュリティ
- 根拠となる実機事実: 2.33秒/5.17秒正常、243f（10秒）は白黒3×3分割で破綻、samevoice 継続実証、PyAV 出力は H.264/yuv420p/AAC
