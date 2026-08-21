# changelog.md — ATELIER H3 Local Studio 変更履歴

時系列の変更履歴（新しいものを上に追記）。現在状態の正本は `ai_summary.md`。
開発中はフェーズ表記で管理してきた。`[Unreleased]` 見出しの整理（`v1.0.0` への統合）は、
OSS 公開時の正式な v1.0.0 確定と同時に行う。

---

## [Unreleased] 2026-08-21 — OSS 公開準備（ライセンス・帰属・文書整備）

公開内容を完成させる工程。生成機能のコードは無変更（挙動の変更は Analytics 環境変数の固定化のみ）。

### Added
- `LICENSE` — Apache License 2.0 の公式本文（無改変。本プロジェクトの copyright 表記は
  Copyright 2026 ATELIER LAB。README §12 に記載）
- `THIRD-PARTY-NOTICES.md` — DiffSynth-Studio（Apache-2.0）・Real-ESRGAN（BSD-3-Clause）の
  帰属と、モデル weight（MiniMax H3 / MiniMax-H3-NF4 / Turbo LoRA / realesr-animevideov3）の
  取得元・ライセンス情報。**weight は本リポジトリに同梱せず、本リポジトリの Apache-2.0 は
  weight に適用されない**ことを明示
- README — uv 必須の明記、clone 直後にモックモードで動作確認する導線、
  「実モデルの準備」章（preflight が検査する実パスを記載）、
  §12「ライセンスと第三者ソフトウェア」（モデルライセンスの中立的な注意書きを含む）
- 由来コードへの帰属コメント — `reference_scripts/*.py`・
  `app/engine/backends/minimax_h3/h3_worker.py`（DiffSynth-Studio 由来の用法）・
  `app/postprocess/upscale_worker.py`（Real-ESRGAN の SRVGGNetCompact 互換実装）

### Changed
- `GRADIO_ANALYTICS_ENABLED` を `setdefault` から**強制代入**へ（`app/main.py`・
  `scripts/real_stage_test.py`）。外部環境変数で Analytics を有効化できる経路を塞いだ
- 公開境界の文書整合 — `_servable(allow_tmp=True)` は「継続サムネイル1件をサーバ側が
  値として渡す」例外であり `allowed_paths` を広げないことを、docstring・`app/main.py`・
  開発規約に明文化（コードの挙動は無変更）
- README の stale な「Git 管理未開始・コミット0件」付録を削除し §12 へ置き換え
- `ai_summary.md` を現在状態（全フェーズ完了・実 iPhone 確認済み・OSS 公開準備中）へ更新

### 実機確認（2026-08-20）
- **実 iPhone からの LANモード最終確認を完了**。接続・新規生成投入・実モデル生成完走・
  完成動画の iPhone 上での再生・「完成・編集」/「履歴」タブへの反映・レスポンシブ表示を確認
  （ジョブ `v_20260820_235040_brtj`: 56フレーム・24fps・4ステップ・約2.33秒・
  seed 262872212・処理時間 約6分38秒・SUCCESS）
- S9（Wi-Fi 断での完走）は未実施のまま（OSS 公開の BLOCKER とは扱わず、追加確認項目として残す）

---

## [Unreleased] 2026-08-10 — P8: 開始画像から動画を作る

手元の写真やイラストを **動画の第1フレーム**に固定して生成できるようにした。
**生成側の固定仕様（576×320・24fps・56/124フレーム・4/8ステップ・直列1本）は無変更**で、
**新規モデルのダウンロードも新規 Python パッケージの追加も無い**。
設計は `docs/v1-design.md` §28（決定D23〜D26）。

使うのは **FL2VA（First-Last frame to Video-Audio）の第1フレーム条件**で、
継続生成（P4）が親の最終フレームを渡している経路と同じもの。
ワーカーへは `keyframes=[画像]` / `keyframe_indices=[0]` を渡し、
**`references` は渡さない**＝ **Ref2VA ではない**。

- **PoC 実測（M4・56フレーム・4ステップ）: 7分57秒**（通常生成 403秒の **1.18倍**）
- **Ref2VA は 2,925秒（48.8分）**かかったため V1 では不採用（§2.2）
- 既存の FL2VA DiT・FL2VA processor・text encoder・Video/Audio VAE・
  Turbo LoRA を**そのまま再利用**。DiffSynth-Studio は無変更

### 不採用にした案（実装しない）
インタラクティブなクロップ・回転エディタ／モード選択ラジオ／素材ライブラリ／
複数画像／最終フレーム指定／任意フレーム位置への画像挿入／参照動画・参照音楽・Ref2VA／
顔検出などの自動クロップ位置決め／開始画像専用の台帳／履歴スキーマの変更／
開始画像の一覧・再利用UI。

### Added
- **`app/core/start_image.py`** — 検証・正規化・staging保存・確定・掃除を行う純粋層
  （UI にもエンジンにも依存せず、`gradio` を import しない）。
  例外 `StartImageError` は**利用者向け日本語だけ**を持ち、内部パス・例外文・
  スタックを含めない
- **①新規生成タブの「開始画像（任意）」**（`app/ui/minimal.py`）—
  プロンプト補助ボタンの直下・長さ選択の直前。未選択時は説明だけ、選択後は
  **変換後の 576×320 をプレビュー**し、切り取り量・透過の告知を出す。
  ［開始画像を外す］は**引数を取らず固定値だけ返す**（`on_clear_prompt` と同型）
- **`/on_submit_v3`（7引数）** — 開始画像IDを受け取る投入口。
  `/on_submit`（5引数）・`/on_submit_v2`（6引数）は API 専用の非表示ボタンで温存
- `AppService.prepare_start_image()` と `submit_generation(_ex)(start_image_id=...)`、
  `config` の `start_images_dir` / `start_images_staging_dir`、
  preflight のディレクトリ作成・孤児 `.partial` 列挙・起動時の staging 掃除
- `app/main.py` に `max_file_size="40mb"`（`allowed_paths` は**無変更**）。
  Gradio のキャッシュ掃除 `delete_cache=(3600, 3600)` は Blocks 側へ指定

### 受け入れる画像／断る画像
- **PNG・JPEG・非アニメーション WebP** のみ。32MB／5000万画素／辺12000px 以下、
  **最小 576×320**（拡大させない）、縦横比 0.5〜3.0
- **HEIC/HEIF・AVIF・GIF・SVG・TIFF・アニメーション・16bit(HDR)・壊れた画像・
  巨大画像・symlink** は日本語で拒否。HEIC は
  「［設定］→［カメラ］→［フォーマット］を『互換性優先』に」まで案内する
- **クロップ先は 1.8:1（9:5）＝出力そのものの形**。576×320 は 16:9（1.7778）ではなく、
  16:9 で切ると横に **1.25% 引き伸びる**ため（決定D24）。中央クロップのみで、
  引き伸ばしは一切しない
- **576×320 ちょうどの RGB は画素を1ビットも変えずに通す**（メタデータだけ除去）
- **透過は黒で塗りつぶし、その旨を必ず告知する**

### 保存先と配信境界
- `data/start_images/`（一時領域は `data/start_images/staging/`）。
  **`allowed_paths` にも `_servable()` にも入れない**（§26.9 の規律のまま、
  どちらにも足さないのが正解）
- UI が持つのは**サーバ採番の ID（`si_` ＋ 正規化PNG の SHA-256 先頭12桁）だけ**。
  パスはブラウザへ出さないし受け取らない。**プレビューは PIL 値で返す**ので
  配信経路を1本も増やしていない
- 投入時に**内容ハッシュを再照合**してから正式パスへ確定するので、
  プレビューした画像とジョブへ渡る画像の**バイト列が一致する**。
  投入後に別の画像を選び直しても登録済みジョブの画像は変わらない

### Changed（既存の契約は壊していない）
- `JobSpec.job_type` に `"start_image"` を追加。
  **`single` が `keyframe_path` を拒否する条件は一切緩めていない**ので、
  既存の継続生成・エンジン試験が無修正で通る
- 履歴は **`type="single"`（個別動画）**として記録（決定D26）。
  `_JOB_TYPES`・`SCHEMA_VERSION`・`resolve_chain()` は**無変更**。
  判別は `parent_id is None and keyframe_path is not None`
- プロンプトは投入時にサーバ側で
  `Continue directly from the supplied first frame.` を**冪等付与**。
  二重投入の冪等化キーにも開始画像IDを含める
- `tests/test_mobile_ui.py` の投入経路の名前を1行だけ更新（`on_submit_v2` → `on_submit_v3`）

### 変更していないもの
生成の解像度・fps・フレーム数・ステップ数・直列1本／ワーカープロトコル／
`history.json`・`concat_manifest.json` のスキーマ／`allowed_paths` と `_servable()`／
既存7つの固定 API（`/on_submit` `/on_tick` `/on_estimate_change` `/on_insert_hint`
`/on_queue_tick` `/on_cancel_queued` `/on_restart_worker`）と `/on_submit_v2`／
6つの Timer（tick）の出力の数と順序／`gr.State` の数（3個）／`h3-panel` の数（3個）／
継続生成・チェーン連結・任意順連結・1080p高品質化・ゴミ箱・Finder表示。
**開始画像から作った動画は、これらすべてで従来どおりの個別動画として扱える。**

### Added（試験）
- `tests/test_start_image.py` — 正規化層（形式・寸法・アニメ・16bit・巨大画像・
  symlink・境界・決定性・メタデータ除去・staging と確定・掃除）
- `tests/test_start_image_job.py` — ジョブ契約と履歴（`start_image` 種別・
  排他・冪等化・`type="single"` での記録・領域外パスの拒否）
- `tests/test_start_image_ui.py`（36件）— 画面の構造・初期表示・文言・配線を
  実際に `/config` と HTTP で確認する。**Timer が開始画像に触れないこと**、
  **`gr.State`／`h3-panel` の数が変わっていないこと**、
  **ブラウザへ返るのが ID と画像だけであること**、
  **`data/start_images` が HTTP で配信されないこと**を機械的に固定する

### Verified
- 全試験 **1480 passed / 1 skipped / 1 xpassed**（P7 時点は 1350。P8 で 130件追加）
- 既存の UI 試験（`test_mobile_ui` / `test_ui_flow` / `test_history_tab_readonly` /
  `test_upscale_ui` / `test_lan_security`）は**1件も落ちていない**

---

## [Unreleased] 2026-08-10 — P7: ④「履歴」タブを閲覧専用へ

③「完成・編集」と④「履歴」に同じ操作が2か所ずつ並んでいた状態を解消した。
**P6（1080p高品質化）の機能は何も変えていない**（UI の役割整理だけ）。

### 役割の固定（決定D22）
- **③「完成・編集」＝動画を操作する唯一の場所** — 選ぶ・プレビュー・詳細・続きを作る・
  ルート連結・Finder表示・詳しい情報・指定順連結・1080p高品質化・ゴミ箱へ移動
- **④「履歴」＝記録を見るだけ** — 状態フィルタと履歴表だけ

### Removed（④から部品ごと削除。隠して残していない）
- プレビュー動画とその見出し／メタ情報・詳細・プロンプト概要
- ［この動画の続きを作る］／［ルートからここまでを連結］／［Finderで表示（Macのみ）］と
  その結果メッセージ・説明文
- 「詳しい情報（サポート用）」の折りたたみ
- 右カラム全体（2カラム構成そのもの）
- ［詳細を見る記録を選ぶ］の Dropdown と ［↻ 選んだ記録を表示］
- ④専用の選択 State と、それに紐づく change / click 配線
- 未使用になった `HISTORY_EMPTY_NOTE` と、`_row_detail` / `_preview_of` の
  `with_status` 引数（④専用の分岐だった）

### Changed（意図的な契約変更）
- **廃止した API**: `/on_select_history`・`/on_history_concat`・`/on_history_reveal`・
  `/on_history_continuation`（外部互換のためだけに残すことはしない）
- **残した API**: `/on_history_filter`・`/on_history_tick`。戻り値は 2→**1**（履歴表だけ）。
  契約は「入力＝状態フィルタ／出力＝履歴表1つ」
- 履歴表は `.h3-wide` で**ページ幅いっぱい**に。セルは折り返さないが
  **最後の列（ジョブ履歴では「エラー」）だけは折り返す**
  （長文で表が何倍にも広がり、Mac でも常に横スクロールが要る状態になるため）

### 変更していないもの
履歴レコードの内容／`history.json`・`concat_manifest.json` のスキーマ／
状態フィルタ9種の選択肢と内部値（番兵含む）／通常ジョブ・連結成果物・1080p成果物の
絞り込み処理と表の内容／ファイル実在による表示判定／P5.3-B のゴミ箱／
P6 のワーカー・UpscaleService・出力検証・音声 stream copy・排他・配信境界／
③のすべての機能。

### Added
- `tests/test_history_tab_readonly.py`（35件）— ④に操作部品が1つも無いこと、
  契約が「フィルタ→表1つ」であること、**③が何も壊れていないこと**を固定する。
  隠し部品・孤児 component ID・削除済み部品への配線が無いことも機械的に確認する

### Verified
- 全試験 **1350 passed / 1 skipped / 1 xpassed**（skip は P5.3-B から続く既知の1件）
- 実描画（実dataは複製して使用・原本は無変更）:
  - Mac 1440×900 — ④は全5フィルタで動画0・ボタン0・選択欄0。表は器1216px／
    中身1447px（「すべて」）で器の中だけ横スクロール。ページ横あふれ 0px
  - iPhone 390×844（DPR2）— 同じく操作部品0。器326px／中身1285px、
    表の中だけ横スクロール。ページ横あふれ 0px。フィルタは44pxのタップ領域を維持
  - ③は Mac / iPhone とも従来どおり（続きを作る・ルート連結・Finder表示・
    選んだ動画を表示・指定順連結がすべて表示され、横あふれ 0px）

---

## [Unreleased] 2026-08-10 — P6: 選んだ動画の1080p高品質化

576×320 で作った動画を、AI で細部を描き足しながら **1920×1080 の別ファイル**にする。
**生成そのものは何も変えていない**（解像度・fps・フレーム数・ステップ数の V1 固定仕様は不変）。

実測（124フレーム・音声あり・Apple M4 / MPS）: **15.6秒**、1920×1080、
フレーム数と再生時間は元と同じ、音声は MD5 一致、細部の量は Lanczos だけの場合の
45.0 に対し **164.8**。元動画のバイト列は変化なし。

### 不採用にした案（実装しない・決定D21）
専用台帳 `upscale_manifest.json` ／ 履歴・台帳のスキーマ変更 ／ モデル選択UI ／
x4plus ／ 余白を足す(pad)方式 ／ ネイティブ1080p生成 ／ フレーム補間 ／
時間方向の超解像 ／ CoreML ／ ncnn ／ クラウドAPI ／ 画像・音楽入力 ／ 復元UI ／
依存関係グラフ ／ カスケード削除。§25 の「資産管理システムを作らない」方針を引き継ぐ。

### Added
- **③「完成・編集」タブに「この動画を1080pにする」**（`app/ui/minimal.py`）—
  動画を選んだときだけ現れる。進捗は `52 / 124フレーム（42%）` の形で毎秒更新し、
  実行中だけ中止ボタンが出る。1080p成果物を選んだ場合はパネルだけ出して
  **開始ボタンを出さず**、理由（これ以上の高品質化はできない）を書く
- `app/postprocess/upscale_worker.py`（新規）— DiffSynth の既存 venv で**一発起動**する
  自己完結ワーカー。`app.*` に依存せず、**どちらの venv にもパッケージを追加しない**
  （basicsr を入れず、必要なネットワーク定義だけ自己実装）。x4 → 高さ1080へ Lanczos 縮小
  → **左右を均等にcrop** で 1920×1080。進捗は `@@PROGRESS` の1行で報告
- `app/core/upscale_service.py`（新規）— 1件ずつ実行・進捗・中止・検証・原子的昇格。
  引数配列・`shell=False`・`PYTHONDONTWRITEBYTECODE=1` でワーカーを起動する
- `AppService.start_upscale / upscale_status / cancel_upscale / upscaled_rows`
- `JobQueue` に **`dispatch_guard`** — ディスパッチ直前だけ呼ばれ、**submit は妨げない**。
  高品質化中に追加した生成は **QUEUED のまま保持**され、終わると自動的に始まる
  （既存の `intake_guard`（空き容量）の挙動は変えていない）
- ④履歴タブに **「1080p成果物」フィルタ**（既存8フィルタは不変）
- `./scripts/setup.sh --with-upscale` — モデル（約2.4MB）を取得し、
  **SHA-256 を照合してから**正式名に置く（照合前は `.partial`）。
  Git には含めない。無くてもアプリは起動する
- 起動前チェックにモデルの有無と SHA-256 照合を追加（**エラーではなく警告**）
- 試験 4ファイル・81件を追加（`test_upscale_service.py` / `test_upscale_worker.py` /
  `test_upscale_integration.py` / `test_upscale_ui.py`）。
  ワーカーは `tests/fixtures/fake_upscale_worker.py` に差し替えるので、
  **実モデルも MPS も使わず 4秒**で回る

### Changed
- 成果物の名前は `u_{clip|chain|manual}_{ID}_1080p.mp4`。**種類を省略しない**
  （個別動画とチェーン連結は同じ `job_id` を使うため、種類が無いと衝突する）。
  48文字を超える安全なIDは切り詰め＋SHA-256 先頭8文字で一意にする
- どの動画に1080p版があるかは**決まった名前のファイルが在るかだけ**で決まる
  （台帳を持たない。§25 と同じ方式）。元動画を整理しても1080p版は残り、
  1080p版を整理しても元動画は残る
- 音声は**再エンコードせず** stream copy。**`-shortest` は使わない**（末尾が切れるため）
- `data/upscaled` を HTTP 配信対象へ追加（`data/trash` は従来どおり**加えない**）

### Fixed
- **1080p成果物のプレビューが再生できなかった** — `allowed_paths` にだけ
  `data/upscaled` を足し、UI 側の `_servable()` を更新し忘れていた。
  実描画確認（Chrome 1440×900）で発見。両方の並びが一致することを試験で固定した
- `app/core/naming.py` に `hashlib` の import が無く、長いIDの短縮が
  `NameError` になる状態だった

### Verified
- 実動画（124フレーム・音声あり）を**本物のモデル・MPS・UpscaleService 経由**で通し、
  解像度・フレーム数・再生時間・音声MD5・元動画の無変更・再実行の拒否・
  中間ファイルが残らないことを確認（作業領域は scratchpad のみ。`data/` は未変更）
- HTTP: 1080p成果物は **206 Partial Content** で取得でき、
  `data/trash` と `history.json` は **403**
- 実描画: Mac 1440×900 / iPhone 390×844 の両方で横スクロールなし
  （`scrollWidth == clientWidth`）。未選択・個別選択・1080p選択の3状態を目視確認
- 全試験 1310件 合格（skip 1件は P5.3-B から続く既知のもの）

---

## [Unreleased] 2026-08-10 — P5.3-B: 動画の整理（簡易方式・アプリ内ゴミ箱）

**設計変更**: 当初検討していた厳密な資産管理方式を**採用しない**と決めた（決定D20）。
このアプリは動画資産管理システムではなく、「気軽に作って確かめ、気に入った成果物だけ
残す」ための道具なので、削除済み動画の完全な追跡・復元・依存関係の維持は行わない。

### 不採用にした旧案（実装しない）
`trash_manifest.json` ／ 汎用 `jsonstore.py` ／ tombstone ／ 復元情報の永続化 ／ 復元UI ／
削除履歴の監査表示 ／ 親子・チェーン・sources を遡る依存検査 ／ 参照先の一覧表示 ／
カスケード削除 ／ 非表示フィルタ ／ 種別ごとの可視性ストア ／ 削除済みIDの永続管理 ／
`history.json`・`concat_manifest.json` からの物理削除。

### Changed
- **表示の正本を「ファイルの実在」にした**（§25.1）— 動画ファイルが無い記録は
  ③の選択候補・件数・連結候補、④の成功履歴・連結成果物から**外す**。
  Finder で消せば次の更新で消え、**正式パスへ戻せば次の更新でまた出る**
  （除外リストを持たないので、追加の操作なしでそうなる）。
  失敗・取消・中断・待機・実行中は**従来どおり必ず残す**
- **内部の記録は削除しない** — `history.json` / `concat_manifest.json` はジョブ管理と
  親子情報のためにそのまま残し、ユーザー向けの一覧にだけ出さない
- **P5.3-A の「ファイル欠損 N件」表示と「整理機能は次の工程で追加予定」の案内を廃止**
  （実在しないものは黙って一覧から外れるだけになった）

### Added
- **アプリ内ゴミ箱**（③「完成・編集」）— 実在する動画を選んだときだけ
  「この動画を整理する」が現れる。確認チェックを入れない限り実行を拒否し、
  ボタンは赤系（`variant="stop"`。P5.3-A で温存しておいた色をここで使う）。
  移動先は `data_root/trash`（**実際に移動するときだけ作る**・`allowed_paths` に入れない）
- `app/core/trash_service.py`（新規・約160行）— **ファイル移動と境界検証だけ**を担当。
  `data_root` 境界／symlink 拒否／通常ファイル／ゴミ箱の中は対象外／実在確認。
  同名があれば `{stem}_{日時}_{乱数4}{suffix}` にして**上書きしない**。
  2ファイル目で失敗したら**1ファイル目を元へ戻す**。戻せなければ元パスと移動先を ERROR ログへ
- `AppService.move_to_trash(job_id, kind)` — 受け取るのは**種別とIDだけ**。
  パスはサーバ側で正式ストアから解決する。生成・連結の実行中は一律拒否
  （依存管理ではなく単純なファイル競合の回避）。二重操作は
  「動画はすでに移動されたか、見つかりません。」を返す
- 成功時は同じ応答で選択解除・プレビュー消去・メタ初期化・確認チェック解除・
  件数と候補の更新までを行う
- テスト53件追加（`tests/test_trash_service.py` 新規41件＋UI・構造12件）

### 依存関係を管理しないことの明示（§25.5）
子がいる／親である／チェーンの途中／`concat_sources` に含まれる、といった理由での
**削除拒否は実装しない**。個別動画を移動しても**子動画・履歴・既存の連結成果物は残る**。
できあがった連結 MP4 は自立して再生できるので、素材が消えても影響を受けない。
素材が要る操作（再連結・継続生成）は既存の検証境界が「動画ファイルが見つかりません」と断る。

### Verified
- 全テスト **1223 passed + 1 skipped + 1 xpassed**（P5.3-A の 1170+1 から53件増）
- **`on_videos_tick` の4出力契約は維持**。Timer は整理セクション・確認チェック・
  編集中の連結順に一切触れない（テストで固定）
- **実 `data/` のコピーで12段階の通し確認**: 欠損4件が③④・連結候補から消える →
  実在動画を選んで移動（MP4＋PNG が `data/trash/` へ）→ ③の件数23→22・④の成功履歴からも消える →
  **再起動後も非表示** → 正式パスへ手で戻す → **再読み込みで自然に再表示（23件へ復帰）** →
  既存の連結動画は無傷で再生可能 → `trash` は配信対象外。
  `history.json` と `concat_manifest.json` は**バイト単位で不変**
- **実描画**（Mac 1440×900 / iPhone 390×844 DPR2）: 選択前は整理セクション非表示、
  選択後に出現。ボタンは赤 `rgb(239,68,68)`、iPhone では**高さ64px**（44px以上）。
  横 overflow は**両幅とも0px**。P5.3-A の連結パネルは非回帰

### Known limitations
- **アプリ内に復元機能は無い**（Finder で `data/trash/` から元のフォルダへ戻す）
- ゴミ箱の中身は自動削除しない（容量の整理は Finder で行う）
- 生成・連結の実行中は整理できない（完了を待つ必要がある）

---

## [Unreleased] 2026-08-10 — P5.3-A: 完成・編集ワークスペースへのUI再設計

③タブを「一覧を見る画面」から「**選ぶ・見る・つなげる作業画面**」へ作り替えた。
**機能の追加はなく、画面構成と表示の役割分担だけ**の変更（連結処理・履歴・台帳は無変更）。
**動画の整理（一覧から除外・ゴミ箱）は P5.3-B で未実装。**

### Changed
- **③の全動画一覧表を廃止**（決定D19）— 実データ27件で3,000px超になり、主機能である
  順番指定連結まで長くスクロールする必要があった。同じ表は④にもあり役割が重複していた。
  一覧は**④履歴タブへ一本化**し、③には**件数と欠損件数の要約1〜2行**だけを出す
- **タブ名を「完成動画」→「完成・編集」へ**。**内部ID `tab_videos` は変更していない**
  （既存の配線・タブ到達テスト・`allowed_paths` に影響を出さないため）
- **上段2カラム（左＝選ぶ／右＝プレビューと操作）＋下段（順番指定連結）＋フル幅の連結状態**。
  **部品を並べた順がそのまま iPhone の1カラム表示順**になるので、CSS の `order` も
  独自 JavaScript も使っていない
- **指定順連結の Accordion を廃止して常時表示**（ページが短くなり隠す理由が消えた）。
  P5.2 の6操作・純粋関数・`gr.State`・上限20本・相互排他はすべて無変更
- **`on_videos_tick` は4出力・同じ順序のまま、先頭の意味だけを「一覧表」→「要約」へ**変更。
  **外部クライアントへの意図的な契約変更**（数・順序・残り3出力の意味は不変）
- 任意連結の行は seed・ステップを「—」で表示（P5.2 からの継続）

### Added
- **順番指定連結の視認性を改善**（P5.3-A 仕上げ）— Gradio の Group 既定背景と
  secondary ボタンの灰色がほとんど同じで、補助操作がボタンに見えなかった。
  パネルを `.h3-panel`（オフホワイト＋枠線＋角丸＋薄い影・内側の器は透過）にし、
  補助操作を `.h3-btn`（**白背景＋枠線のアウトラインボタン**・hover／focus つき）、
  追加を `.h3-btn-accent`（白背景＋**オレンジの枠線と文字**）にした。
  **塗りつぶしのオレンジは最終実行の1つだけ**。赤系は使わない
  （P5.3-B の「ゴミ箱へ移動」のために残す）。CSS は**すべて `.h3-` 付きの選択子**で、
  グローバルな `button` 指定も独自 JavaScript も使っていない
- **文言を「削除」から「候補から外す」へ** — `↑ 1つ上へ` / `↓ 1つ下へ` /
  `－ 候補から外す` / `連結候補をすべて解除`、対象欄は
  `対象を選ぶ（順番の入れ替え・候補から外す）`。パネルの説明にも
  「**動画は削除されません**」と明記し、**ファイル削除と区別**した
- `.h3-vscroll`（`max-height: 14em; overflow-y: auto`）— **連結順の一覧だけ**を局所縦スクロール。
  20本選んでもページ全体は伸びない。**合計本数・合計時間は器の外**に出したので常に読める
  （実測: 2本49px／5本132px／20本は内容549pxを196pxの枠でスクロール・最終項目まで到達可能）
- **④履歴タブに「連結成果物」フィルタ**（既存7フィルタの意味・動作は不変）。
  チェーン連結（`c_*`）と指定順連結（`cm_*`）を新しい順に、**連結成果物にとって意味のある列**
  （種類／ID／作成日時／長さ／本数／元の動画＝順番どおり／ファイル）で表示する。
  step・seed・状態遷移は使わない。元動画が5本以上なら `先頭 → … → 末尾` に省略
- `AppService.completed_summary()` / `concat_product_rows()` — 要約と連結成果物の一覧。
  どちらも `completed_videos()` と同じ行から作るので、画面と選択候補が食い違わない
- テスト17件追加（③の表が無いこと・要約の件数・欠損件数・DOM順・2カラム・局所スクロール・
  合計の位置・④の新フィルタ・既存7フィルタ非回帰・Timer が編集中の状態に触れないこと）

### Verified
- 全テスト **1170 passed + 1 xpassed**（P5.2 の 1145+1 から25件増）
- **外観の実測**（computed style）: パネル `rgb(252,252,253)` に対しボタンは
  `rgb(255,255,255)`＋枠線 `rgb(152,162,179)`（追加は枠線 `rgb(255,124,0)`）で、
  **背景も枠線もパネルと同一でない**ことを機械的に確認。最終実行だけが塗りつぶし
  `rgb(249,115,22)`。パネル内に残る灰色ブロックは **0個**
- **実描画確認**（mock・**実 `data/` のコピー**・ヘッドレス Chrome 151・CDP）:
  - Mac 1440×900: ページ全長 **3,000px超 → 1110px**。順番指定連結の見出しは y=662 で
    **初期表示内**、実行ボタン y=938。表は0個、横 overflow **0px**
  - iPhone 390×844(DPR2): 作業する動画 y=176 → 続きを作る y=883 → 順番指定連結 y=1291
    → 連結の状態 y=1965 の**希望どおりの順**。横 overflow **0px**、操作ボタンは**すべて44px**
  - 実データのコピーで**欠損4件**が要約・選択候補・④の連結成果物フィルタに正しく出ることを確認
  - 実際に20本を積んで局所スクロール（内容549px／枠196px／最終項目到達可）を実測
- 既存のチェーン連結・継続生成・Finder・プレビュー・任意連結6操作は非回帰
- プロジェクトの実 `data/`・`history.json`・`concat_manifest.json`・`.bak` は**無変更**

### Known limitations
- **動画の整理は未実装**（P5.3-B）。欠損記録は候補に残り、要約が件数を知らせるだけ
- ③の左カラム下部には余白が出る（右カラムのほうが背が高いため）。右を広くとって
  縮めたが、2カラムの構造上ゼロにはならない
- 1080p高品質化（P6）のボタンは③の右カラムへ足せるよう場所だけ空けてある（未実装）
- 無効時（`:disabled`）の**色**は Gradio 6.22.0 のボタン CSS が優先するため変わらない
  （`cursor: not-allowed` は効く）。この画面のボタンは無効にならないので実害はない

---

## [Unreleased] 2026-08-09 — P5.2: 任意順序連結（複数動画の順番指定連結）

好きな動画を好きな順番でつないだ動画を作れるようにした。既存の「ルートからここまでを連結」は
そのまま残し、**置き換えない**。生成エンジン・キュー契約・**履歴スキーマは無変更**。

### Added
- **③完成動画タブに「複数の動画を選んで連結（順番指定）」**（初期 closed の折りたたみ）—
  候補選択 →［＋連結候補へ追加］→ 番号付きの「現在の連結順」→ 対象を選んで［↑上へ］［↓下へ］
  ［－削除］→［選択をすべて解除］／［▶ この順番で連結（N本・約XX秒）］。
  **ドラッグ＆ドロップは使わない**（iPhone 対応）
- `app/core/concat_manifest.py`（新規）— 任意連結の成果物台帳 `data/concat_manifest.json`。
  一意tmp→`flush`→`fsync`→`os.replace()`、保存失敗時のメモリロールバック、
  **検証済み primary のみ `.bak` 更新**、破損時の隔離退避、**`.bak` も検証してから復旧**、
  両方破損なら空で開始（**MP4 は削除しない**）、ID・重複・パス境界・型・必須フィールドの検証
- `HistoryStore.resolve_custom_concat()` — 任意順IDの9項目検証。**指定順をそのまま返す**
  （作成日時順・ID順へ並べ替えない）。SUCCESS 検証・互換性検証・成果物検証は
  `resolve_concat_chain()` と**共通の内部ヘルパ**にまとめ、判定と文言を二重に書かない
- `ConcatService.start_custom_concat()` — チェーン連結と**同じレーン**（同じ排他・スレッド・
  失敗時清掃・shutdown）。`ConcatStatus` に `mode`（`chain`/`custom`）と `concat_id` を
  後方互換に追加。状態欄に「チェーン連結／指定順連結」と本数を表示
- `AppService.start_custom_concat()` / `concat_candidates()` / `completed_videos()` の合流 —
  個別・チェーン連結・任意連結を1つの一覧へ。`VideoRow.concat_kind` で内部識別（表示は共通）
- `naming.new_manual_concat_id()` / `manual_concat_filename()` — `cm_YYYYMMDD_HHMMSS_xxxx_{n}clips.mp4`
- 並び操作は `app/ui/minimal.py` の**モジュールレベル純粋関数**（`custom_order_add` /
  `custom_order_move` / `custom_order_remove` / `custom_order_clear`）。入力を破壊しない
- テスト137件追加（台帳46／入力解決25／サービス32／順序の機械的証明7／UI・構造27）

### Changed
- `docs/v1-design.md` を **v1.6** へ（§23 新設・決定D18）
- **`/on_videos_tick` の戻り値を 3→4 へ**（末尾に「指定順連結の候補」を追加）。
  **既存3つの順番と意味は不変**。意図的な契約変更としてテストにも明記した
- 任意連結の行は seed・ステップを「—」で表示（複数動画の結果なので値が定まらない。
  「ランダム」と出すと誤解を招く）
- `README.md` に指定順連結の操作を追記

### 設計判断（決定D18: 成果物を履歴に書かない）
チェーン連結は終端レコードの `concat_path` に記録するが、この形は**1レコード1連結**しか
持てない。任意連結では A→B→C と A→D→C と B→A が同時に存在しうるため、終端へ記録すると
先の成果物を上書きする（本数が同じならファイル名も一致し **MP4 まで上書き**）。
履歴スキーマを変える案は、旧版アプリで `type` 検証に落ちて**全履歴がパース不能**になり
隔離＋`.bak` 復旧で直近の履歴を失うため採らなかった。**history.json は1バイトも変更しない。**

### 失敗時の契約（追加安全要件）
正式 MP4 へ昇格した後に台帳の保存が失敗した場合、**今回昇格した MP4 をロールバック削除**する。
削除できなければ `.orphan_*.mp4` へ隔離（一覧には出ない）、それも無理なら**正確なパスと
対処方法**を詳細ログへ残す。「一覧に出るのに台帳に無い MP4」も「台帳にあるのにファイルが
無い記録」も作らない。3経路とも障害注入で自動テスト済み。

### Verified
- 全テスト **1144 passed + 1 xpassed**（P5.1 の 1007+1 から137件増）
- **順序の機械的証明**: 赤+440Hz / 緑+880Hz / 青+1320Hz の素材を実 ffmpeg で作り、
  B→C→A・C→A→B・A→C→B・A→B・B→A を連結。各区間の**中央フレームの色**と
  **同区間の音声の主要周波数**の両方が指定順と一致することを確認（A/V の入れ違いも検出できる）。
  「別の順番として解釈すると必ず失敗する」ことも試験に含め、素通りしないことを担保
- **実描画確認**（mock・ヘッドレス Chrome 151・CDP）: Mac 1440px と iPhone 390px（DPR2）とも
  **横スクロール 0px**。iPhone では操作ボタン5種すべて**高さ実測 44px**の1カラム
- **実機相当の通し確認**: data/ のコピーを data_root にして3本を任意順に連結 → 15.50秒・
  台帳へ記録 → 再起動後も③一覧・プレビューに表示 → **HTTP 206 `video/mp4`** で再生 →
  Finder 解決成功。**元動画の size/mtime は不変**、`history.json` も**バイト単位で不変**
- **配信境界**: `concat_manifest.json` は **403**（`history.json` と同じく配信対象外）
- 既存チェーン連結の非回帰（実行・出力 `c_*`・履歴記録・HTTP 206）を確認
- 目視・通し確認はすべて `data/` のコピーで実施。**プロジェクトの `data/` は無変更**

### Known limitations
- 実行中の強制中断は無い（チェーン連結と同じ。［選択をすべて解除］は実行前の候補操作）
- 連結成果物を素材にした再連結、同じ動画の重複利用、20本超はいずれも仕様として拒否する
- 任意連結成果物の削除UIは無い（既存方針どおり、整理は Finder で行う）
- 台帳はプロセス起動時にメモリへ読む（`history.json` と同じ）。同じ data_root を
  複数プロセスで同時に使う運用は想定しない

---

## [Unreleased] 2026-08-09 — P5.1: UI操作性改善（プロンプト消去・選択欄の移動）

小規模な UI 改修のみ。**生成エンジン・キュー契約・履歴スキーマ・設計契約は無変更**
（`docs/v1-design.md` と開発規約は更新不要）。

### Added
- **［プロンプトを消去］ボタン**（①新規生成タブ）— プロンプト入力欄の直下、
  ［＋日本語セリフ記法を挿入］と同じ行に置いた副ボタン（`secondary` / `size="sm"`）。
  ハンドラ `on_clear_prompt()` は**引数を取らず空文字1つだけを返し**、配線の
  `outputs` もプロンプト欄1つに限定してある。長さ・ステップ・シード・継続モード・
  キュー・履歴・完成動画には**構造的に**触れられない。確認ダイアログなし。
  空欄で押しても例外にならない。API 名は `/on_clear_prompt`
- テスト15件追加（機能6件 `test_ui_flow.py` ／ 構造9件 `test_mobile_ui.py`。
  うち3件は案内文の整合を守る回帰テスト）。
  レイアウト木（`/config` の `layout`）を上からたどって「一覧より上か」を判定する
  ヘルパ `_display_order()` / `_parents()` を追加

### Changed
- **③完成動画タブの選択欄を一覧の上へ移動** — ［表示する動画を選ぶ（個別／連結）］と
  ［↻ 選んだ動画を表示］を、`完成した動画（N件・新しい順）` の見出しと表より**上**へ。
  **複製ではなく移動**（最下部には何も残らない）。選択・表示・継続・連結・Finder の
  配線と Timer の出力順（一覧・選択欄・連結状態）は**一切変更していない**ため、
  既存動作と選択状態の保持はそのまま
- `README.md` — ①の消去ボタンと、③の選択欄が上にあることを追記
- **案内文の整合修正**（選択欄の移動に合わせた軽微な修正）— ③の右側の案内を
  「上の一覧から動画を選ぶと…」→**「上の選択欄から動画を選び、［選んだ動画を表示］を
  押すと、ここに詳細とプレビューが出ます。」**へ。起動直後の表示と、選択を空にした
  ときの案内の**両方**を直した（片方だけだと選択を外した瞬間に古い文言へ戻る）。
  この案内文は③④で共有されていたため `VIDEOS_EMPTY_NOTE` / `HISTORY_EMPTY_NOTE` に
  分け、`_preview_of()` が呼び出し側から `empty_message` を受け取る形にした。
  ④は選択欄が一覧の下・ボタン名も「選んだ記録を表示」なので、③の文言を流用しない
  （④の案内は既存の「上の一覧から記録を選ぶと、ここに詳細が出ます。」に統一）。
  **配置・イベント配線・選択動作は無変更**

### Verified
- 全テスト **1007 passed + 1 xpassed**（P5 の 992+1 から15件増）
- **回帰検知の確認**: 選択欄を元の位置（一覧の下）へ戻すと
  `test_video_selector_is_shown_above_the_list_on_the_completed_tab` が失敗することを
  実際に確認した（試験が素通りしない証明）。確認後にファイルは復元済み
- **実描画での目視確認**（mock モード・ヘッドレス Chrome 151・CDP でタブ切替）:
  Mac 幅 1440px と iPhone 幅 390px（DPR2）の両方で、③タブの選択欄が一覧より上に出る。
  横スクロールは**両幅とも 0px**（`scrollWidth == clientWidth`）。
  iPhone では ［↻ 選んだ動画を表示］・［プロンプトを消去］とも**高さ実測 44px**・
  幅 326px の1カラム
- 実行中の UI に対して個別動画（`clip:`）・連結動画（`concat:`）の選択がどちらも
  従来どおりプレビューを返すことを HTTP 経由で確認
- 目視確認は `data/` のコピーを別の data_root にして行い、**プロジェクトの `data/` は無変更**

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
  → **2026-08-20 に実機確認を完了**（上の「OSS 公開準備」エントリを参照）

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
- 開発継続文書: `ai_summary.md`（現在状態の正本）／`changelog.md`（本書）。あわせて AI エージェント向けの開発規約（絶対制約・主要コマンド・文書更新規約）を整備（リポジトリ外で管理）
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
- README に継続文書への案内を追加

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
