"""iPhone（モバイル）対応・二重投入防止・日本語文言の検証（P5 §6.1〜§6.4）。

**この試験で確認できること／できないこと**（正直に書く）:

- ブラウザ（Safari）は起動しないので、**実際の描画結果**（本当に横スクロールが
  出ないか、指で押しやすいか）は検証できない。ここで検証するのは
  「ブラウザへ配られる CSS と、画面を構成する部品の構造」である。
  具体的には、実際に Gradio を localhost で起動して `/config` を取得し、
  (a) レスポンシブ CSS がページへ確かに配られていること
  (b) 幅の広い表が局所スクロールの器に入っていること
  (c) 1カラム化の対象クラスが実際の Row に付いていること
  (d) レイアウトを変える規則が `@media (max-width: 640px)` の**中だけ**にあること
      （＝ Mac の表示を回帰させない構造になっていること）
  を確認する。**実機の見た目の最終確認はメイン担当の実機試験で行うこと。**
- 二重投入防止・再接続・「閉じても処理が続く」ことは HTTP 経由で実際に検証する。
- Execution Engine は必ず MockEngine（**実モデルは絶対に起動しない**）。
- 書き込み先は `tmp_path` のみ（プロジェクトの `data/` には一切触れない）。
"""

from __future__ import annotations

import dataclasses
import json
import re
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.app_service import AppService, SubmitResult
from app.core.config import load_config
from app.ui.minimal import (
    CUSTOM_ADD_LABEL,
    CUSTOM_CLEAR_LABEL,
    CUSTOM_CONCAT_TITLE,
    CUSTOM_DOWN_LABEL,
    CUSTOM_REMOVE_LABEL,
    CUSTOM_UP_LABEL,
    FINDER_NOTE,
    MOBILE_BREAKPOINT_PX,
    MOBILE_CSS,
    STEP_CHOICES,
    SUBMIT_LABEL,
    SUBMIT_LABEL_BUSY,
    VIDEOS_EMPTY_NOTE,
    VIDEOS_TAB_LABEL,
    build_ui,
)

#: 選択欄を一覧の上へ移す前の案内文（P5.1 で置き換えた。復活させない）
OLD_VIDEOS_EMPTY_NOTE = "上の一覧から動画を選ぶと、ここに詳細とプレビューが出ます。"

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MEDIA_QUERY = f"@media (max-width: {MOBILE_BREAKPOINT_PX}px)"

LENGTH_56 = "約2.33秒（56フレーム）"
STEPS_4 = "4ステップ（高速）"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_service(tmp: Path):
    """モックの AppService を作る（`data_root` は tmp のみ。実モデルは使わない）。"""
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    return cfg, service


def _launch(cfg, service):
    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        allowed_paths=[str(cfg.outputs_dir), str(cfg.concat_dir), str(cfg.tmp_dir)],
        prevent_thread_lock=True,
    )
    return demo, port


@pytest.fixture(scope="module")
def mobile_app(tmp_path_factory):
    """モバイル検証用の UI。**ディスパッチャは起動しない**（投入は QUEUED のまま残る）。"""
    cfg, service = _make_service(tmp_path_factory.mktemp("mobile_ui_data"))
    service.history.load()  # 履歴だけ読む（queue.start() は呼ばない＝生成は始まらない）
    demo, port = _launch(cfg, service)

    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield client, service, demo, port
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


@pytest.fixture(scope="module")
def ui_config(mobile_app):
    """起動中のサーバが実際にブラウザへ配る `/config`（＝画面の設計図）。"""
    import urllib.request

    _client, _service, _demo, port = mobile_app
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(scope="module")
def p4_history_table(mobile_app):
    """記録が1件以上あるときの④履歴表（0件では見出し行が出ないため）。"""
    client, service, _demo, _port = mobile_app
    service.submit_generation_ex(
        prompt="履歴表の確認用", num_frames=56, steps=4, seed_requested=7
    )
    result = client.predict("すべて", api_name="/on_history_filter")
    # P7 で ④の出力は履歴表1つだけになった。出力が1つの経路では
    # gradio_client は**タプルではなく値そのもの**を返す
    assert not isinstance(result, (list, tuple)), "④の出力が1つに絞られていません"
    return str(result)


@pytest.fixture()
def offline_demo(tmp_path):
    """起動せずに構造だけ見たい試験用（Blocks を組み立てるだけ）。"""
    cfg, service = _make_service(tmp_path)
    service.history.load()
    return cfg, service, build_ui(cfg, "mock", service)


# ------------------------------------------------------------------ CSS の構造


def _split_css(css: str) -> tuple[str, str]:
    """CSS を「メディアクエリの外」と「中」に分ける（Mac 回帰防止の判定に使う）。

    ネストは使っていないので、`@media ... {` から対応する `}` までを中身とみなす。
    """
    start = css.index(MEDIA_QUERY)
    brace = css.index("{", start)
    depth = 0
    for i in range(brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                inside = css[brace + 1 : i]
                outside = css[:start] + css[i + 1 :]
                return outside, inside
    raise AssertionError("メディアクエリの括弧が閉じていません")


def test_mobile_css_uses_a_640px_media_query():
    assert MEDIA_QUERY in MOBILE_CSS
    outside, inside = _split_css(MOBILE_CSS)
    assert inside.strip()
    assert MEDIA_QUERY not in inside  # 分割が正しいことの確認


def test_layout_rules_are_confined_to_the_mobile_media_query():
    """Mac の表示を回帰させない: レイアウトを変える指定はメディアクエリの中だけ。"""
    outside, inside = _split_css(MOBILE_CSS)

    # 1カラム化・コンテナ幅・パディングの変更は「中」にだけある
    assert "flex-direction: column" in inside
    assert "flex-direction" not in outside
    assert ".gradio-container {" not in outside.replace("\n", " ")
    for banned in ("max-width: 100% !important", "padding: 8px !important"):
        assert banned in inside
        assert banned not in outside

    # 「外」で触ってよいのは新設クラスと video/img の最大幅だけ
    without_comments = re.sub(r"/\*.*?\*/", "", outside, flags=re.S)
    outside_selectors = re.findall(r"([^{}]+)\{", without_comments)
    for selector in outside_selectors:
        selector = selector.strip()
        assert selector.startswith(".h3-") or selector.startswith(
            ".gradio-container video"
        ), f"Mac にも効く選択子が増えています: {selector}"


def test_css_has_no_fixed_pixel_widths():
    """固定 px 幅を作らない（作ると狭い画面で横スクロールが出る。§6.1）。"""
    fixed = re.findall(r"(?<!min-)(?<!max-)width:\s*\d+px", MOBILE_CSS)
    assert fixed == [], f"固定幅が含まれています: {fixed}"


def test_css_keeps_horizontal_scrolling_inside_a_local_container():
    """横スクロールは `.h3-scroll` の内側だけ。ページ本体は横に伸ばさない。"""
    outside, _inside = _split_css(MOBILE_CSS)
    assert ".h3-scroll" in outside
    assert "overflow-x: auto" in outside
    # ページ全体を隠す（内容が読めなくなる）逃げ方はしていない
    assert "overflow-x: hidden" not in MOBILE_CSS


def test_css_makes_videos_and_images_fluid():
    """動画は全画面共通ではみ出さない。画像の調整は iPhone のときだけ。

    `img { height: auto }` を共通部に置くと、Gradio がアイコン等へ付ける固定高さを
    Mac 表示でも上書きしてしまう。Mac の見た目を変えないため、画像側の規則は
    メディアクエリの中だけに置く（相互レビュー L2 の指摘）。
    """
    outside, inside = _split_css(MOBILE_CSS)
    assert ".gradio-container video" in outside
    assert "max-width: 100%" in outside
    # 共通部に img のスタイル規則が無いこと（コメント文中の "img" は数えない）
    outside_rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", outside, flags=re.S))
    assert not any("img" in sel for sel, _body in outside_rules)
    # iPhone のときは画像も画面幅へ収める
    assert ".gradio-container img" in inside


def test_css_gives_tap_targets_at_least_44px():
    """タップ領域は 44×44px 以上（Apple HIG の最小値）。"""
    _outside, inside = _split_css(MOBILE_CSS)
    rules = dict(re.findall(r"([^{}]+)\{([^{}]*)\}", inside))
    tap_rules = [sel for sel, body in rules.items() if "min-height: 44px" in body]
    joined = " ".join(tap_rules)
    assert "button" in joined
    assert "select" in joined
    assert "label" in joined
    # チェックボックス・ラジオ自体も指で押せる大きさにする
    assert "min-width: 24px" in inside and "min-height: 24px" in inside


def test_css_is_actually_delivered_to_the_browser(ui_config):
    """組み立てた CSS が実際に `/config` へ載ってブラウザまで届く。"""
    css = ui_config.get("css") or ""
    assert MEDIA_QUERY in css
    assert ".h3-scroll" in css and ".h3-row" in css


# --------------------------------------------------------- 画面部品の構造（配信物）


def _components(ui_config, type_name: str) -> list[dict]:
    return [c for c in ui_config["components"] if c.get("type") == type_name]


def _classes(component: dict) -> list[str]:
    return list(component.get("props", {}).get("elem_classes") or [])


def _display_order(ui_config) -> list[int]:
    """レイアウト木を上から順にたどり、部品IDを**画面に出る順**で並べる。

    Mac は Row の左から右、iPhone は 1カラムに畳まれて上から下。どちらも
    この深さ優先の並びと一致するので、「一覧より上にあるか」をこれで判定できる。
    """
    order: list[int] = []

    def walk(node: dict) -> None:
        order.append(node["id"])
        for child in node.get("children") or []:
            walk(child)

    walk(ui_config["layout"])
    return order


def _parents(ui_config) -> dict[int, int]:
    """子IDから親IDを引く表（部品を包む Row のクラスを確かめるのに使う）。"""
    parent: dict[int, int] = {}

    def walk(node: dict) -> None:
        for child in node.get("children") or []:
            parent[child["id"]] = node["id"]
            walk(child)

    walk(ui_config["layout"])
    return parent


def _only(matches: list[dict], what: str) -> dict:
    """ちょうど1つだけ見つかることを確かめる（＝複製していない）。"""
    assert len(matches) == 1, f"{what} が {len(matches)} 個あります（1個であるべき）"
    return matches[0]


def _by_label(ui_config, type_name: str, text: str) -> list[dict]:
    return [
        c
        for c in _components(ui_config, type_name)
        if text in str(c.get("props", {}).get("label") or "")
    ]


def _by_value(ui_config, type_name: str, text: str) -> list[dict]:
    return [
        c
        for c in _components(ui_config, type_name)
        if str(c.get("props", {}).get("value") or "").startswith(text)
    ]


def test_every_row_stacks_into_one_column_on_narrow_screens(ui_config):
    """すべての横並び（Row）が 1カラム化の対象クラスを持つ（§6.1）。"""
    rows = _components(ui_config, "row")
    assert rows, "Row が1つも見つかりません"
    missing = [r["id"] for r in rows if "h3-row" not in _classes(r)]
    assert missing == [], f"h3-row が付いていない Row: {missing}"

    _outside, inside = _split_css(MOBILE_CSS)
    assert ".h3-row {" in inside and "flex-direction: column !important" in inside
    # 子要素の min-width を 0 にしないと、中身が親を押し広げて横スクロールが出る
    assert "min-width: 0 !important" in inside


def test_wide_tables_are_wrapped_in_a_local_scroll_container(offline_demo, ui_config):
    """幅の広い表（待機一覧・完成動画・履歴）は局所スクロールの器に入っている。

    「表を出す部品」は Timer コールバックの出力先そのものなので、
    `demo.fns` から出力先の部品を引き当ててクラスを確認する（取り違えない）。
    """
    _cfg, _service, demo = offline_demo
    by_api = {fn.api_name: fn for fn in demo.fns.values()}
    # P5.3-A: ③の完成動画表は廃止したので、表を出す Timer は②と④だけになった
    table_outputs = {
        "on_queue_tick": 3,   # 待機中のジョブ（表）
        "on_history_tick": 0,  # 履歴（13列の表・連結成果物の表）
    }
    for api_name, index in table_outputs.items():
        component = by_api[api_name].outputs[index]
        classes = list(getattr(component, "elem_classes", []) or [])
        assert "h3-scroll" in classes, f"{api_name} の表に h3-scroll がありません"

    # ③の先頭は表ではなく要約（横に広がらないので h3-scroll は不要）
    summary = by_api["on_videos_tick"].outputs[0]
    assert "h3-scroll" not in list(getattr(summary, "elem_classes", []) or [])

    scrollable = [
        c for c in _components(ui_config, "markdown") if "h3-scroll" in _classes(c)
    ]
    assert len(scrollable) >= 2, "表を出す Markdown に h3-scroll が足りません"

    # 起動直後に表がある部品（履歴の見出し行）も器の中に入っている
    for component in _components(ui_config, "markdown"):
        value = str(component.get("props", {}).get("value") or "")
        if "|---" in value or "| 状態 |" in value:
            assert "h3-scroll" in _classes(component), component["id"]


def test_video_components_have_no_fixed_pixel_width(ui_config):
    videos = _components(ui_config, "video")
    assert videos
    for video in videos:
        width = video.get("props", {}).get("width")
        assert width in (None, "100%"), f"動画に固定幅が入っています: {width}"


def test_all_four_tabs_are_reachable(ui_config):
    """①〜④のタブがすべて存在する（iPhone でもタブ操作で行き来できる）。"""
    tabs = _components(ui_config, "tabitem")
    ids = {t.get("props", {}).get("id") for t in tabs}
    labels = {t.get("props", {}).get("label") for t in tabs}
    assert {"tab_new", "tab_queue", "tab_videos", "tab_history"} <= ids
    # P5.3-A: ③は「完成動画」から「完成・編集」へ改称した（**内部IDは不変**）
    assert {"新規生成", "キュー", "完成・編集", "履歴"} <= labels
    assert "完成動画" not in labels
    videos_tab = _only(
        [t for t in tabs if t.get("props", {}).get("id") == "tab_videos"], "③タブ"
    )
    assert videos_tab["props"]["label"] == VIDEOS_TAB_LABEL


def test_detail_accordions_exist_on_every_tab_that_shows_technical_text(ui_config):
    """技術的な情報は「詳しい情報」（折りたたみ）に分離してある（§6.4）。"""
    accordions = _components(ui_config, "accordion")
    labels = [str(a.get("props", {}).get("label") or "") for a in accordions]
    detail = [label for label in labels if "詳しい情報" in label]
    # ①アプリの動作ログ・②サポート用・③完成・編集 の3か所。
    # ④履歴の分は P7 で廃止した（技術情報を見る操作も③へ一本化した）
    assert len(detail) >= 3, labels
    assert all(not a.get("props", {}).get("open") for a in accordions if "詳しい情報" in str(a.get("props", {}).get("label") or ""))


def test_finder_button_is_explained_as_mac_only(ui_config):
    """Finder 表示が Mac だけの機能であることを日本語で明示する（§6.1）。"""
    buttons = [
        str(b.get("props", {}).get("value") or "")
        for b in _components(ui_config, "button")
    ]
    finder_buttons = [b for b in buttons if "Finder" in b]
    assert finder_buttons, "Finder ボタンが見つかりません"
    assert all("Mac" in b for b in finder_buttons), finder_buttons

    notes = [
        str(m.get("props", {}).get("value") or "")
        for m in _components(ui_config, "markdown")
    ]
    assert any(FINDER_NOTE == n for n in notes)
    assert "iPhone" in FINDER_NOTE and "Mac" in FINDER_NOTE


# ------------------------------------------------------------ 二重投入防止（§6.2）


def _submit_button(demo):
    import gradio as gr

    buttons = [
        b
        for b in demo.blocks.values()
        if isinstance(b, gr.Button) and b.value == SUBMIT_LABEL
    ]
    assert len(buttons) == 1, [b.value for b in buttons]
    return buttons[0]


def test_submit_button_is_disabled_on_click_and_restored_afterwards(offline_demo):
    """押した瞬間に無効化 → 投入 → 応答後に必ず有効化、の3段になっている。"""
    _cfg, _service, demo = offline_demo
    submit = _submit_button(demo)

    order = list(demo.fns.items())
    touching = [(i, fn) for i, fn in order if submit in fn.outputs]
    assert len(touching) == 2, "生成ボタンを操作するイベントは2つ（無効化・再有効化）"

    (disable_index, disable_fn), (enable_index, enable_fn) = touching
    # 1段目はボタンの click そのもの（＝押した瞬間に走る）
    assert disable_fn.targets == [(submit._id, "click")]
    disabled = disable_fn.fn()
    assert disabled["interactive"] is False
    assert disabled["value"] == SUBMIT_LABEL_BUSY

    # 2段目が本体の投入。3段目が再有効化で、2段目の直後にぶら下がっている
    submit_fn_index = next(
        i for i, fn in order if fn.api_name == "on_submit_v2"
    )
    submit_fn = demo.fns[submit_fn_index]
    assert submit_fn.trigger_after == disable_index
    assert enable_fn.trigger_after == submit_fn_index
    enabled = enable_fn.fn()
    assert enabled["interactive"] is True
    assert enabled["value"] == SUBMIT_LABEL
    # 失敗しても押せないまま取り残されない（`.then` は成功時限定にしない）
    assert enable_fn.trigger_only_on_success is False


def test_timer_ticks_never_touch_the_submit_button(offline_demo):
    """Timer の出力に生成ボタンを入れない（毎秒の更新と無効化が競合するため）。"""
    import gradio as gr

    _cfg, _service, demo = offline_demo
    submit = _submit_button(demo)
    timer_ids = {b._id for b in demo.blocks.values() if isinstance(b, gr.Timer)}
    assert timer_ids, "Timer が見つかりません"

    ticking = [
        fn
        for fn in demo.fns.values()
        if any(target in timer_ids for target, _event in fn.targets)
    ]
    assert ticking
    for fn in ticking:
        assert submit not in fn.outputs, f"{fn.api_name} が生成ボタンを上書きします"
        assert not any(isinstance(o, gr.Button) for o in fn.outputs)


def test_double_submit_registers_only_one_job(mobile_app, monkeypatch):
    """同じ内容を続けて2回投入しても、登録されるのは1件だけ（§6.2）。"""
    client, service, _demo, _port = mobile_app
    monkeypatch.setattr(service, "submit_idempotency_sec", 5.0)
    before = len(service.queue.queued_jobs())
    prompt = "二重投入の確認 <d>[Japanese] にどうとうこう</d>"

    first = client.predict(prompt, LENGTH_56, STEPS_4, True, 42, "", api_name="/on_submit_v2")[0]
    second = client.predict(prompt, LENGTH_56, STEPS_4, True, 42, "", api_name="/on_submit_v2")[0]

    assert "キューに追加しました" in first
    assert "同じ内容がすでに登録されています" in second
    assert not second.startswith("❌")  # 失敗ではなく「1件だけ登録した」正常な結果
    job_id = [j.job_id for j in service.queue.queued_jobs()][-1]
    assert job_id in first and job_id in second
    assert len(service.queue.queued_jobs()) == before + 1


def test_duplicate_submission_leaves_no_trace_in_history(tmp_path):
    """冪等化の情報は履歴にも JobSpec にも残さない（メモリ上のキャッシュだけ）。"""
    cfg, service = _make_service(tmp_path)
    service.history.load()
    service.submit_idempotency_sec = 5.0
    try:
        first = service.submit_generation_ex(
            prompt="履歴を汚さない確認", num_frames=56, steps=4, seed_requested=7
        )
        second = service.submit_generation_ex(
            prompt="履歴を汚さない確認", num_frames=56, steps=4, seed_requested=7
        )
        assert isinstance(first, SubmitResult) and isinstance(second, SubmitResult)
        assert first.duplicate is False and second.duplicate is True
        assert second.view.job_id == first.view.job_id

        records = service.history.list_records()
        assert len(records) == 1
        raw = json.loads(cfg.history_path.read_text(encoding="utf-8"))
        text = json.dumps(raw, ensure_ascii=False)
        for banned in ("idempot", "duplicate", "submit_key"):
            assert banned not in text.lower()
    finally:
        service.shutdown(timeout=5.0)


def test_intentional_resubmission_is_allowed_after_the_window(tmp_path):
    """意図的な作り直しは妨げない（時間窓を過ぎれば同じ内容でも通る）。"""
    cfg, service = _make_service(tmp_path)
    service.history.load()
    service.submit_idempotency_sec = 0.05
    try:
        first = service.submit_generation_ex(
            prompt="作り直しの確認", num_frames=56, steps=4, seed_requested=1
        )
        time.sleep(0.2)
        second = service.submit_generation_ex(
            prompt="作り直しの確認", num_frames=56, steps=4, seed_requested=1
        )
        assert second.duplicate is False
        assert second.view.job_id != first.view.job_id
        assert len(service.queue.queued_jobs()) == 2
    finally:
        service.shutdown(timeout=5.0)


@pytest.mark.parametrize(
    "changed",
    [
        {"prompt": "べつの内容"},
        {"num_frames": 124},
        {"steps": 8},
        {"seed_requested": 99},
        {"parent_id": "v_parent"},
        {"keyframe_path": Path("/tmp/other_last.png")},
    ],
)
def test_idempotency_key_covers_every_input(changed):
    """キーは（プロンプト・長さ・ステップ・seed・親・キーフレーム）で決まる。"""
    base = dict(
        prompt="ベース",
        num_frames=56,
        steps=4,
        seed_requested=42,
        parent_id=None,
        keyframe_path=None,
    )
    key = AppService._idempotency_key(**base)
    assert key == AppService._idempotency_key(**base)  # 同じ入力なら同じキー
    assert key != AppService._idempotency_key(**{**base, **changed}), changed


# ---------------------------------------------------------------- 再接続（§6.3）


def test_queue_state_is_restored_for_a_new_browser_session(mobile_app, monkeypatch):
    """リロード（新しいセッション）でも現在のキュー状態を取得できる。"""
    from gradio_client import Client

    client, service, _demo, port = mobile_app
    monkeypatch.setattr(service, "submit_idempotency_sec", 0.0)
    client.predict(
        "再接続の確認", LENGTH_56, STEPS_4, True, 42, "", api_name="/on_submit_v2"
    )
    job_id = service.queue.queued_jobs()[-1].job_id

    # ブラウザを閉じて開き直した状況＝別セッションの新しいクライアント
    reloaded = Client(f"http://127.0.0.1:{port}", verbose=False)
    header, progress, *_ = reloaded.predict(api_name="/on_tick")
    waiting = reloaded.predict(api_name="/on_queue_tick")[3]

    assert "待機" in header
    assert job_id in waiting
    assert job_id in progress
    # 取消候補（Dropdown）も新しいセッションへ届く
    choices = reloaded.predict(api_name="/on_queue_tick")[5]
    values = [c[1] if isinstance(c, (list, tuple)) else c for c in choices["choices"]]
    assert job_id in values


def test_generation_continues_after_the_browser_disconnects(tmp_path):
    """Safari を閉じてもサーバ側の生成は続く（コールバックで待たない設計の確認）。"""
    cfg, service = _make_service(tmp_path)
    service.start()
    demo, port = _launch(cfg, service)
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        started = time.monotonic()
        message = client.predict(
            "切断後も続く確認", LENGTH_56, STEPS_4, True, 42, "", api_name="/on_submit_v2"
        )[0]
        submit_elapsed = time.monotonic() - started
        assert "キューに追加しました" in message
        assert submit_elapsed < 3.0, "投入が生成の完了を待っています"

        # ここでブラウザを閉じる（HTTP 接続を切る）
        try:
            client.close()
        except Exception:  # 実装差異があってもテストの本質は変わらない
            pass
        del client

        deadline = time.monotonic() + 30
        latest = None
        while time.monotonic() < deadline:
            latest = service.latest_completed()
            if latest is not None:
                break
            time.sleep(0.1)
        assert latest is not None, "クライアント切断後に生成が止まりました"
        assert latest.video_path.is_file()
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


# ---------------------------------------------------------------- 文言（§6.4）


def test_ui_never_claims_that_8_steps_takes_twice_as_long(ui_config):
    """8ステップは実測 約1.67倍。「約2倍」とは表示しない（P3 実測）。"""
    assert STEP_CHOICES["8ステップ（高品質・時間は約1.7倍）"] == 8
    text = json.dumps(ui_config, ensure_ascii=False)
    assert "約2倍" not in text
    assert "約1.7倍" in text


def test_estimates_keep_the_measured_values(mobile_app):
    """目安時間は実測どおり（56f/4step 約6〜7分・124f/4step 約13〜14分・56f/8step 約11〜12分）。"""
    client, _service, _demo, _port = mobile_app
    cases = {
        (LENGTH_56, STEPS_4): "約6〜7分",
        ("約5.17秒（124フレーム）", STEPS_4): "約13〜14分",
        (LENGTH_56, "8ステップ（高品質・時間は約1.7倍）"): "約11〜12分",
    }
    for (length, steps), expected in cases.items():
        text = client.predict(length, steps, api_name="/on_estimate_change")
        assert expected in text, (length, steps, text)
        assert "実機で計測した値です" in text

    estimated = client.predict(
        "約5.17秒（124フレーム）",
        "8ステップ（高品質・時間は約1.7倍）",
        api_name="/on_estimate_change",
    )
    assert "約22〜23分" in estimated and "推定値" in estimated


def test_technical_terms_are_not_shown_in_the_main_panels(mobile_app):
    """内部用語は主要部に出さない（分類とジョブIDだけは残す。§6.4）。"""
    client, _service, _demo, _port = mobile_app
    _banner, engine, current, waiting, error, _choices = client.predict(
        api_name="/on_queue_tick"
    )
    main_text = "\n".join([engine, current, waiting, error])
    for term in ("execution_engine", "backend_id", "Traceback", "Exception"):
        assert term not in main_text, term
    # 「詳しい情報」側には残す（サポート用）
    detail = client.predict(api_name="/on_queue_detail_tick")
    assert "execution_engine" in detail


# ------------------------------------------------------------ LANモード（§3・§6.1）


def _lan_html(demo_config) -> str:
    return "\n".join(
        str(c.get("props", {}).get("value") or "")
        for c in demo_config["components"]
        if c.get("type") == "html"
    )


def test_lan_banner_shows_url_and_username_but_never_a_pin(tmp_path):
    """LANモードでは接続先を常時表示する。**PIN は UI に出さない**（P5 §3）。"""
    cfg, service = _make_service(tmp_path)
    service.history.load()
    lan_info = SimpleNamespace(
        url="http://192.168.1.23:7860", host="192.168.1.23", port=7860
    )
    demo = build_ui(cfg, "mock", service, lan_info)
    config = demo.get_config_file()
    html_text = _lan_html(config)

    assert "iPhone接続モード" in html_text
    assert "http://192.168.1.23:7860" in html_text
    assert "h3" in html_text  # ログインのユーザー名
    assert "同じWi-Fi" in html_text
    assert "Control+C" in html_text
    # PIN は UI 層へ渡ってこない（LanInfo に持たせていない）。
    # 画面にも「PIN そのもの」を出さない＝連続した4〜12桁の数字が現れない
    assert "PIN" not in html_text
    assert not re.search(r"(?<![\d.:])\d{4,12}(?![\d.:])", html_text), html_text
    assert not hasattr(lan_info, "pin")
    # 画面全体（全部品の文字列）にも数字だけの並びを出していない
    all_text = "\n".join(
        str(c.get("props", {}).get("value") or "")
        for c in config["components"]
        if isinstance(c.get("props", {}).get("value"), str)
    )
    assert "PIN" not in all_text


def test_lan_banner_works_with_the_real_lan_info_dataclass(tmp_path):
    """本物の `app.core.network.LanInfo` を渡しても動く（結合の確認）。"""
    network = pytest.importorskip("app.core.network")
    lan_info_cls = getattr(network, "LanInfo", None)
    if lan_info_cls is None:  # pragma: no cover - 実装前でも UI 試験は落とさない
        pytest.skip("LanInfo がまだ実装されていません")

    fields = {f for f in getattr(lan_info_cls, "__dataclass_fields__", {})}
    assert "pin" not in fields, "LanInfo に PIN を持たせてはいけません（P5 §3）"

    cfg, service = _make_service(tmp_path)
    service.history.load()
    demo = build_ui(
        cfg,
        "mock",
        service,
        lan_info_cls(url="http://10.0.0.5:7860", host="10.0.0.5", port=7860),
    )
    html_text = _lan_html(demo.get_config_file())
    assert "http://10.0.0.5:7860" in html_text
    assert "iPhone接続モード" in html_text


def test_no_lan_banner_in_normal_mode(offline_demo):
    """通常モード（127.0.0.1）では LAN の案内を出さない。"""
    _cfg, _service, demo = offline_demo
    html_text = _lan_html(demo.get_config_file())
    assert "iPhone接続モード" not in html_text


def test_resubmit_after_cancel_is_not_swallowed(mobile_app):
    """取消の直後に同じ内容を投入したら、新しいジョブとして登録される。

    冪等化が「直前ジョブがまだ生きているか」を見ないと、取消のあとに同じ内容を
    出し直したとき『同じ内容がすでに登録されています』と表示されるのに1件も
    走らない状態になる（相互レビュー M1）。
    """
    from app.core.contracts import JobStatus

    _client, svc, _demo, _port = mobile_app
    first = svc.submit_generation_ex(
        prompt="取消してから作り直す", num_frames=56, steps=4, seed_requested=11
    )
    svc.queue.cancel_queued(first.view.job_id)
    assert svc.history.get(first.view.job_id).status is JobStatus.CANCELED

    second = svc.submit_generation_ex(
        prompt="取消してから作り直す", num_frames=56, steps=4, seed_requested=11
    )
    assert second.duplicate is False
    assert second.view.job_id != first.view.job_id


def test_double_tap_is_still_collapsed(mobile_app):
    """生きているジョブに対する二重タップは、これまでどおり1件に潰す。"""
    _client, svc, _demo, _port = mobile_app
    a = svc.submit_generation_ex(
        prompt="二重タップ防止", num_frames=56, steps=4, seed_requested=12
    )
    b = svc.submit_generation_ex(
        prompt="二重タップ防止", num_frames=56, steps=4, seed_requested=12
    )
    assert b.duplicate is True
    assert b.view.job_id == a.view.job_id


# ------------------------------------------------------------------ P5.1 UI操作性


def test_clear_prompt_button_sits_next_to_the_hint_button(ui_config):
    """［プロンプトを消去］は［＋日本語セリフ記法を挿入］と同じ行に1つだけある。"""
    clear = _only(_by_value(ui_config, "button", "プロンプトを消去"), "消去ボタン")
    hint = _only(
        _by_value(ui_config, "button", "＋日本語セリフ記法を挿入"), "セリフ記法ボタン"
    )
    parents = _parents(ui_config)
    assert parents[clear["id"]] == parents[hint["id"]], "2つのボタンが同じ行にありません"

    order = _display_order(ui_config)
    prompt = _only(_by_label(ui_config, "textbox", "プロンプト（英語推奨"), "プロンプト欄")
    assert order.index(prompt["id"]) < order.index(clear["id"]), "消去ボタンがプロンプト欄より上にあります"


def test_clear_prompt_button_is_less_prominent_than_the_submit_button(ui_config):
    """主操作（生成をキューに追加）だけが primary で、消去は副ボタンにとどめる。"""
    clear = _only(_by_value(ui_config, "button", "プロンプトを消去"), "消去ボタン")
    submit = _only(_by_value(ui_config, "button", SUBMIT_LABEL), "生成ボタン")
    assert clear["props"].get("variant") == "secondary"
    assert submit["props"].get("variant") == "primary"
    assert clear["props"].get("size") == "sm"


def test_clear_prompt_button_keeps_a_44px_tap_target(ui_config):
    """iPhone の 44px タップ領域を守る行（h3-tap）に入っている。"""
    clear = _only(_by_value(ui_config, "button", "プロンプトを消去"), "消去ボタン")
    row = {c["id"]: c for c in ui_config["components"]}[_parents(ui_config)[clear["id"]]]
    assert "h3-tap" in _classes(row) and "h3-row" in _classes(row)

    _outside, inside = _split_css(MOBILE_CSS)
    assert ".h3-tap button" in inside and "min-height: 44px !important" in inside


def test_clear_prompt_never_writes_to_anything_but_the_prompt(offline_demo):
    """配線の出力先がプロンプト欄1つだけ（他の入力・一覧・履歴に触れない）。"""
    _cfg, _service, demo = offline_demo
    fn = {f.api_name: f for f in demo.fns.values()}["on_clear_prompt"]
    assert fn.inputs == []
    assert len(fn.outputs) == 1
    assert "プロンプト（英語推奨" in str(fn.outputs[0].label)


def test_completed_tab_has_no_video_table(ui_config, offline_demo):
    """③完成・編集タブに**全動画の一覧表が無い**（P5.3-A で④へ一本化した）。

    P5.1 では「選択欄を一覧より上へ」を確かめていたが、その一覧自体を廃止した。
    ここでは「表が復活していないこと」を代わりに固定する。
    """
    # 表の見出しも列も③には無い
    assert _by_value(ui_config, "markdown", "### 完成した動画（") == []
    for component in _components(ui_config, "markdown"):
        value = str(component.get("props", {}).get("value") or "")
        if "| 種別 | ID | 作成日時 | 長さ | ステップ |" in value:
            raise AssertionError(f"③の完成動画表が残っています: {component['id']}")

    # ③の選択欄は今もタブの先頭側にある（要約より上）
    select = _only(
        _by_label(ui_config, "dropdown", "表示する動画を選ぶ（個別／連結）"), "選択欄"
    )
    reload_btn = _only(_by_value(ui_config, "button", "↻ 選んだ動画を表示"), "表示ボタン")
    summary = _only(_by_value(ui_config, "markdown", "**完成した動画:"), "完成動画の要約")
    order = _display_order(ui_config)
    assert order.index(select["id"]) < order.index(summary["id"])
    assert order.index(reload_btn["id"]) < order.index(summary["id"])

    # 要約は Timer の先頭出力そのもの（表示と更新先が食い違わない）
    _cfg, _service, demo = offline_demo
    by_api = {fn.api_name: fn for fn in demo.fns.values()}
    assert by_api["on_videos_tick"].outputs[0].value == summary["props"]["value"]


def test_history_tab_still_has_its_table(offline_demo, p4_history_table):
    """④履歴タブの表は残る（一覧はこちらへ一本化した）。

    起動直後は0件で見出し行が出ないため、(a) 表を出す部品が Timer の出力先として
    今もあること (b) 記録があるときに実際に表が描かれること、の両方を見る。
    P7 で Timer の出力は**この表だけ**になった。
    """
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    outputs = by_api["on_history_tick"].outputs
    assert len(outputs) == 1, "Timer が履歴表以外も更新しています"
    table = outputs[0]
    assert "h3-scroll" in list(getattr(table, "elem_classes", []) or [])
    assert "h3-wide" in list(getattr(table, "elem_classes", []) or [])
    assert "履歴" in str(table.value)

    assert "| 状態 | 種別 | 日時 | ID |" in p4_history_table


def test_video_selector_row_keeps_one_column_and_44px_on_iphone(ui_config):
    """移動後も 1カラム化（h3-row）と 44px タップ領域（h3-tap）を保つ。"""
    reload_btn = _only(_by_value(ui_config, "button", "↻ 選んだ動画を表示"), "表示ボタン")
    by_id = {c["id"]: c for c in ui_config["components"]}
    row = by_id[_parents(ui_config)[reload_btn["id"]]]
    assert {"h3-row", "h3-tap"} <= set(_classes(row))


def test_completed_tab_still_wires_select_reload_and_actions(offline_demo):
    """配置換えで既存の選択・表示・継続・連結・Finder の配線が外れていない。"""
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    select = by_api["on_videos_tick"].outputs[1]
    for api_name in (
        "on_select_video",
        "on_start_concat",
        "on_reveal_video",
        "on_start_continuation",
    ):
        assert by_api[api_name].inputs == [select], f"{api_name} の入力が選択欄ではありません"
    # Timer の出力（要約・選択欄・連結状態・候補）は配置換えの影響を受けない
    tick = by_api["on_videos_tick"].outputs
    assert len(tick) == 4
    assert type(tick[0]).__name__ == "Markdown"  # P5.3-A: 表ではなく要約
    assert type(tick[2]).__name__ == "Markdown"  # 連結の状態


def test_completed_tab_guidance_matches_the_new_selector_position(ui_config, mobile_app):
    """③の案内文が「上の選択欄から選び、表示ボタンを押す」に更新されている（P5.1）。

    起動直後の表示と、選択を空にしたときの案内の**両方**を見る。
    どちらか一方だけ直すと、選択を外した瞬間に古い文言が戻ってしまう。
    """
    client, _svc, _demo, _port = mobile_app

    assert VIDEOS_EMPTY_NOTE == (
        "上の選択欄から動画を選び、［選んだ動画を表示］を押すと、"
        "ここに詳細とプレビューが出ます。"
    )
    initial = [
        c
        for c in _components(ui_config, "markdown")
        if str(c.get("props", {}).get("value") or "") == VIDEOS_EMPTY_NOTE
    ]
    assert len(initial) == 1, "③の初期案内文が見つからない（または重複している）"

    _video, meta, _tech = client.predict("", api_name="/on_select_video")
    assert meta == VIDEOS_EMPTY_NOTE, "選択を空にすると案内文が古いものへ戻る"


def test_old_completed_tab_guidance_is_gone_everywhere(ui_config, mobile_app):
    """古い案内文が、画面にもコールバックの戻り値にも残っていない（P5.1）。"""
    client, _svc, _demo, _port = mobile_app

    source = (PROJECT_ROOT / "app" / "ui" / "minimal.py").read_text(encoding="utf-8")
    assert OLD_VIDEOS_EMPTY_NOTE not in source, "古い案内文がソースに残っています"

    values = [
        str(c.get("props", {}).get("value") or "") for c in ui_config["components"]
    ]
    assert OLD_VIDEOS_EMPTY_NOTE not in values

    # ④のプレビュー経路は P7 で廃止したので、残っている③だけを確認する
    _video, meta, _tech = client.predict("", api_name="/on_select_video")
    assert meta != OLD_VIDEOS_EMPTY_NOTE


def test_history_tab_has_no_record_selector_anymore(ui_config):
    """④から記録選択の欄と表示ボタンを**部品ごと**無くした（P7・決定D22）。

    以前は④にも選択欄・プレビュー・操作ボタンがあり、③と役割が重複していた。
    隠すだけでは同じ操作が2か所にある状態が続くので、部品自体を削除する。
    """
    labels = [str(d["props"].get("label") or "") for d in _components(ui_config, "dropdown")]
    assert not any("記録を選ぶ" in label for label in labels)

    values = [str(b["props"].get("value") or "") for b in _components(ui_config, "button")]
    assert "↻ 選んだ記録を表示" not in values

    # ③の選択欄と表示ボタンは残っている（④だけを消したことの確認）
    assert any("表示する動画を選ぶ" in label for label in labels)
    assert "↻ 選んだ動画を表示" in values


# ------------------------------------------------- P5.2 指定順連結（③タブ）


def test_custom_concat_is_always_visible_without_an_accordion(ui_config):
    """指定順連結は**常時表示**（P5.3-A で折りたたみを廃止した）。

    一覧表を④へ移してページが短くなったので、隠す理由が無くなった。
    折りたたみが復活すると「開かないと使えない」状態に戻るため、ここで固定する。
    """
    accordions = [
        a
        for a in _components(ui_config, "accordion")
        if "複数の動画を選んで連結" in str(a.get("props", {}).get("label") or "")
    ]
    assert accordions == [], "指定順連結が折りたたみに戻っています"

    # 見出しは通常の Markdown として常に見えている
    heading = _only(
        _by_value(ui_config, "markdown", f"### {CUSTOM_CONCAT_TITLE}"), "見出し"
    )
    assert heading["props"]["value"].startswith("### ")


def test_custom_concat_controls_are_present_once(ui_config):
    """追加・上下移動・候補から外す・全解除・実行のボタンが1組だけある。"""
    for text in (
        CUSTOM_ADD_LABEL,
        CUSTOM_UP_LABEL,
        CUSTOM_DOWN_LABEL,
        CUSTOM_REMOVE_LABEL,
        CUSTOM_CLEAR_LABEL,
    ):
        _only(_by_value(ui_config, "button", text), f"{text} ボタン")
    _only(
        [
            b
            for b in _components(ui_config, "button")
            if str(b.get("props", {}).get("value") or "").startswith("▶ この順番で連結")
        ],
        "実行ボタン",
    )
    _only(_by_label(ui_config, "dropdown", "追加する動画を選ぶ"), "候補ドロップダウン")
    _only(_by_label(ui_config, "dropdown", "対象を選ぶ"), "対象ドロップダウン")


def test_custom_concat_is_separate_from_the_view_selector(ui_config):
    """既存の「表示する動画を選ぶ」と別の操作領域になっている（取り違え防止）。

    P5.3-A で折りたたみを廃止したので、見出しから下の**下段**にあること
    （＝上段の表示用選択欄とは別のまとまり）で分離を確かめる。
    """
    view_selector = _only(
        _by_label(ui_config, "dropdown", "表示する動画を選ぶ（個別／連結）"), "表示用選択欄"
    )
    add_selector = _only(_by_label(ui_config, "dropdown", "追加する動画を選ぶ"), "候補選択欄")
    assert view_selector["id"] != add_selector["id"]
    assert view_selector["props"]["label"] != add_selector["props"]["label"]

    order = _display_order(ui_config)
    heading = _only(
        _by_value(ui_config, "markdown", f"### {CUSTOM_CONCAT_TITLE}"), "見出し"
    )
    # 表示用選択欄（上段） → 見出し → 候補選択欄（下段）の順に並ぶ
    assert order.index(view_selector["id"]) < order.index(heading["id"])
    assert order.index(heading["id"]) < order.index(add_selector["id"])

    # 親が違う＝別のまとまりに入っている
    parents = _parents(ui_config)
    assert parents[view_selector["id"]] != parents[add_selector["id"]]


def test_custom_concat_rows_stack_and_keep_44px_on_iphone(ui_config):
    """1カラム化（h3-row）と 44px タップ領域（h3-tap）を全操作行が持つ。"""
    by_id = {c["id"]: c for c in ui_config["components"]}
    parents = _parents(ui_config)
    for text in (
        CUSTOM_ADD_LABEL, CUSTOM_UP_LABEL, CUSTOM_DOWN_LABEL,
        CUSTOM_REMOVE_LABEL, CUSTOM_CLEAR_LABEL,
    ):
        button = _only(_by_value(ui_config, "button", text), f"{text} ボタン")
        row = by_id[parents[button["id"]]]
        assert {"h3-row", "h3-tap"} <= set(_classes(row)), text


def test_timer_never_touches_the_custom_concat_order(offline_demo):
    """Timer の outputs に State・現在順・対象欄・実行ボタンが入っていない。

    ここが崩れると、1秒ごとの更新でユーザーが編集中の並びが巻き戻る。
    """
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    tick_outputs = by_api["on_videos_tick"].outputs

    # 末尾に足したのは「候補ドロップダウン」だけ
    assert len(tick_outputs) == 4
    assert "追加する動画を選ぶ" in str(getattr(tick_outputs[3], "label", ""))

    labels = [str(getattr(o, "label", "") or "") for o in tick_outputs]
    assert not any("対象を選ぶ" in label for label in labels)
    types = {type(o).__name__ for o in tick_outputs}
    assert "State" not in types, "Timer が State を上書きしています"
    assert "Button" not in types, "Timer が実行ボタンを上書きしています"

    # 他の Timer ハンドラも同様に State を触らない
    for api_name in ("on_tick", "on_queue_tick", "on_history_tick"):
        assert "State" not in {
            type(o).__name__ for o in by_api[api_name].outputs
        } or api_name == "on_tick"


def test_custom_concat_order_state_is_a_session_state(offline_demo):
    """並びは `gr.State`＝**ブラウザセッションごと**（サーバ共有ではない）。"""
    import gradio as gr

    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    add = by_api["on_custom_add"]
    assert isinstance(add.inputs[0], gr.State)
    assert isinstance(add.outputs[0], gr.State)
    # 既定値は空リスト（セッション開始時は何も選ばれていない）
    assert add.inputs[0].value == []


def test_custom_concat_handlers_are_wired(offline_demo):
    """6つの操作がすべて配線されている。"""
    _cfg, _service, demo = offline_demo
    names = {f.api_name for f in demo.fns.values()}
    assert {
        "on_custom_add",
        "on_custom_up",
        "on_custom_down",
        "on_custom_remove",
        "on_custom_clear",
        "on_custom_start",
    } <= names


def test_existing_completed_tab_controls_are_not_regressed(ui_config):
    """P5.1 までの③タブの操作が、そのまま残っている。"""
    # ③にだけあるもの
    _only(_by_value(ui_config, "button", "↻ 選んだ動画を表示"), "表示ボタン")
    # ③④の両方にあるもの（P4 からの既存仕様）
    for text in ("この動画の続きを作る", "ルートからここまでを連結", "Finderで表示（Macのみ）"):
        assert _by_value(ui_config, "button", text), f"{text} ボタンが消えています"
    # 選択欄は今もタブの先頭側（P5.1 の「まず選べる」意図を維持）。
    # P5.3-A で一覧表が要約に変わったので、比較相手を要約にしている。
    order = _display_order(ui_config)
    select = _only(
        _by_label(ui_config, "dropdown", "表示する動画を選ぶ（個別／連結）"), "選択欄"
    )
    summary = _only(_by_value(ui_config, "markdown", "**完成した動画:"), "要約")
    assert order.index(select["id"]) < order.index(summary["id"])


# ------------------------------- P5.3-A 完成・編集ワークスペース（③の再設計）


def test_completed_tab_layout_order_matches_the_iphone_reading_order(ui_config):
    """DOM の並び＝iPhone の1カラム表示順（P5.3-A・設計書 §24.3）。

    希望順は「選ぶ → プレビュー → 選択動画への操作 → 順番指定連結 → 連結の状態」。
    CSS の order や独自 JavaScript を使わずに済むよう、**この順に組み立てている**。
    """
    order = _display_order(ui_config)
    select = _only(
        _by_label(ui_config, "dropdown", "表示する動画を選ぶ（個別／連結）"), "選択欄"
    )
    preview = _only(
        [v for v in _components(ui_config, "video")
         if v.get("props", {}).get("label") == "プレビュー"],
        "プレビュー",
    )
    continue_btn = [
        b for b in _by_value(ui_config, "button", "この動画の続きを作る")
        if order.index(b["id"]) > order.index(preview["id"])
    ][0]
    concat_heading = _only(
        _by_value(ui_config, "markdown", f"### {CUSTOM_CONCAT_TITLE}"), "連結の見出し"
    )
    status = _only(_by_value(ui_config, "markdown", "### 連結の状態"), "連結の状態")

    positions = [
        order.index(c["id"])
        for c in (select, preview, continue_btn, concat_heading, status)
    ]
    assert positions == sorted(positions), f"表示順が希望と違います: {positions}"


def test_completed_tab_has_two_columns_on_mac(ui_config):
    """Mac では上段が2カラム（選ぶ／プレビューと操作）。iPhone では h3-row で畳む。"""
    parents = _parents(ui_config)
    by_id = {c["id"]: c for c in ui_config["components"]}
    select = _only(
        _by_label(ui_config, "dropdown", "表示する動画を選ぶ（個別／連結）"), "選択欄"
    )
    preview = _only(
        [v for v in _components(ui_config, "video")
         if v.get("props", {}).get("label") == "プレビュー"],
        "プレビュー",
    )

    def column_of(component_id):
        while component_id in parents:
            component_id = parents[component_id]
            if by_id.get(component_id, {}).get("type") == "column":
                return component_id
        return None

    left, right = column_of(select["id"]), column_of(preview["id"])
    assert left and right and left != right, "上段が2カラムになっていません"
    row = parents[left]
    assert row == parents[right], "2つのカラムが同じ Row に入っていません"
    assert "h3-row" in _classes(by_id[row]), "iPhone で1カラムへ畳めません"


def test_concat_order_list_scrolls_locally(ui_config):
    """連結順の一覧だけが縦スクロール（20本でもページ全体は伸びない）。"""
    scrollers = [
        c for c in _components(ui_config, "markdown") if "h3-vscroll" in _classes(c)
    ]
    assert len(scrollers) == 1, "連結順の局所スクロールが1つだけあるはず"

    _outside, inside = _split_css(MOBILE_CSS)
    assert ".h3-vscroll" in MOBILE_CSS
    # レイアウト規則はメディアクエリの外でも .h3- 接頭辞なら許される（既存規約）
    assert "max-height" in MOBILE_CSS and "overflow-y: auto" in MOBILE_CSS
    # 横には広げない（横スクロールの原因を作らない）
    rules = dict(re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", MOBILE_CSS, flags=re.S)))
    vscroll = next(body for sel, body in rules.items() if ".h3-vscroll" in sel)
    assert "overflow-x" not in vscroll


def test_concat_total_is_outside_the_scroll_area(ui_config, offline_demo):
    """合計本数・合計時間はスクロール領域の**外**にある（20本でも常に読める）。"""
    order = _display_order(ui_config)
    total = _only(_by_value(ui_config, "markdown", "**現在の連結順:"), "合計表示")
    scroller = _only(
        [c for c in _components(ui_config, "markdown") if "h3-vscroll" in _classes(c)],
        "連結順の一覧",
    )
    assert "h3-vscroll" not in _classes(total)
    assert order.index(total["id"]) < order.index(scroller["id"])

    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    outputs = by_api["on_custom_add"].outputs
    assert len(outputs) == 6, "合計表示が出力に含まれていません"
    assert "h3-vscroll" in list(getattr(outputs[1], "elem_classes", []) or [])
    assert "h3-vscroll" not in list(getattr(outputs[5], "elem_classes", []) or [])


def test_history_tab_has_a_concat_products_filter(ui_config):
    """④に「連結成果物」フィルタがあり、既存7フィルタは非回帰（P5.3-A）。"""
    radios = [
        r for r in _components(ui_config, "radio")
        if "状態フィルタ" in str(r.get("props", {}).get("label") or "")
    ]
    assert len(radios) == 1
    choices = [
        c[0] if isinstance(c, (list, tuple)) else c
        for c in radios[0]["props"]["choices"]
    ]
    assert choices[:7] == ["すべて", "成功", "失敗", "取消", "中断", "実行待ち", "実行中"]
    assert choices[7] == "連結成果物"
    assert radios[0]["props"]["value"] == "すべて"  # 既定は従来どおり


def test_timer_still_never_touches_the_custom_concat_editing_state(offline_demo):
    """再設計後も Timer は編集中の状態（State・連結順・合計・対象欄・実行ボタン）に触れない。"""
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    tick = by_api["on_videos_tick"].outputs
    assert len(tick) == 4

    editing = {
        id(o)
        for o in by_api["on_custom_add"].outputs
        if type(o).__name__ in ("State", "Button")
        or "h3-vscroll" in list(getattr(o, "elem_classes", []) or [])
    }
    assert editing, "編集中の部品を特定できていません"
    assert not (editing & {id(o) for o in tick}), "Timer が編集中の部品を上書きします"

    labels = [str(getattr(o, "label", "") or "") for o in tick]
    assert not any("対象を選ぶ" in label for label in labels)
    assert "追加する動画を選ぶ" in labels[3]  # 候補の choices だけは更新する


# ------------------- P5.3-A 仕上げ: 順番指定連結の視認性（外観の契約）
#
# Gradio の Group 既定背景（薄い灰色）と secondary ボタンの灰色がほとんど同じで、
# 補助操作が「押せるもの」に見えなかった。パネルを白く、ボタンを白＋枠線にして
# 境界を作る。**塗りつぶしのオレンジは最終実行だけ**に残す。


def test_custom_concat_panel_has_its_own_class(ui_config):
    """連結セクションが専用クラスのパネルになっている（Gradio 既定背景に任せない）。

    P5.3-B で整理セクション、P6 で高品質化セクションにも同じパネル装飾を使うので、
    `.h3-panel` は3つある。ここでは**連結の操作を含むほう**が正しく
    パネルになっていることを見る。
    """
    panels = [c for c in ui_config["components"] if "h3-panel" in _classes(c)]
    assert len(panels) == 3, "連結パネル・整理パネル・高品質化パネルの3つがあるはず"
    assert all(p["type"] == "group" for p in panels)

    parents = _parents(ui_config)
    add = _only(_by_value(ui_config, "button", CUSTOM_ADD_LABEL), "追加ボタン")

    def ancestors(component_id):
        seen = set()
        while component_id in parents:
            component_id = parents[component_id]
            seen.add(component_id)
        return seen

    concat_panels = [p for p in panels if p["id"] in ancestors(add["id"])]
    assert len(concat_panels) == 1, "順番指定連結のパネルが特定できません"
    # 連結パネルは常時表示、整理パネルは選択するまで隠れている
    assert concat_panels[0]["props"].get("visible") is not False
    other = [p for p in panels if p["id"] != concat_panels[0]["id"]][0]
    assert other["props"].get("visible") is False


def test_helper_buttons_are_outlined_and_not_the_primary(ui_config):
    """補助操作は専用クラスの白ボタン。**塗りつぶしの主ボタンにはしない**。"""
    for text in (CUSTOM_UP_LABEL, CUSTOM_DOWN_LABEL, CUSTOM_REMOVE_LABEL, CUSTOM_CLEAR_LABEL):
        button = _only(_by_value(ui_config, "button", text), f"{text} ボタン")
        assert "h3-btn" in _classes(button), f"{text} に h3-btn がありません"
        assert "h3-btn-accent" not in _classes(button)
        assert button["props"].get("variant") != "primary"


def test_add_button_is_accented_but_weaker_than_the_run_button(ui_config):
    """追加はオレンジの**枠線だけ**。塗りつぶしのオレンジは最終実行の1つだけ。"""
    add = _only(_by_value(ui_config, "button", CUSTOM_ADD_LABEL), "追加ボタン")
    assert "h3-btn-accent" in _classes(add)
    assert add["props"].get("variant") != "primary"

    run = _only(
        [
            b for b in _components(ui_config, "button")
            if str(b.get("props", {}).get("value") or "").startswith("▶ この順番で連結")
        ],
        "実行ボタン",
    )
    assert run["props"].get("variant") == "primary"
    assert not _classes(run), "実行ボタンは既定の主ボタンのまま"

    # ③タブで塗りつぶしの主ボタンは「続きを作る」と「この順番で連結」だけ
    primaries = [
        str(b["props"].get("value") or "")
        for b in _components(ui_config, "button")
        if b["props"].get("variant") == "primary"
    ]
    assert any(p.startswith("▶ この順番で連結") for p in primaries)
    assert not any("候補" in p or "上へ" in p or "下へ" in p for p in primaries)


def test_button_styles_are_scoped_and_not_global(ui_config):
    """`.h3-` 付きの選択子だけで書く（グローバルな button 指定を足さない）。"""
    outside, inside = _split_css(MOBILE_CSS)
    without_comments = re.sub(r"/\*.*?\*/", "", outside, flags=re.S)
    for selector in re.findall(r"([^{}]+)\{", without_comments):
        for part in selector.split(","):
            part = part.strip()
            assert part.startswith(".h3-") or part.startswith(".gradio-container video"), (
                f"Mac にも効くグローバル指定が増えています: {part}"
            )
    # 追加した規則は button 単独ではなく必ずクラスの下にある
    assert ".h3-btn, .h3-btn button" in MOBILE_CSS
    assert ".h3-btn-accent, .h3-btn-accent button" in MOBILE_CSS
    # メディアクエリの中の 44px 指定（既存）はそのまま
    assert ".gradio-container button" in inside


def test_helper_buttons_define_hover_focus_and_disabled(ui_config):
    """通常・hover・focus・disabled が見分けられる指定になっている。

    ※ disabled の**色**は Gradio 6.22.0 のボタン CSS が優先するため実描画では
    変わらない（`cursor: not-allowed` は効く）。この画面のボタンは無効にならない
    ので実害はないが、将来 `interactive=False` を使うときのために定義は残す。
    """
    css = MOBILE_CSS
    for base in (".h3-btn", ".h3-btn-accent"):
        assert f"{base}:hover:not(:disabled)" in css, f"{base} の hover がありません"
        assert f"{base}:focus-visible" in css, f"{base} の focus がありません"
        assert f"{base}:disabled" in css, f"{base} の disabled がありません"
        assert "cursor: not-allowed" in css
    # 危険色（赤系）は使わない。P5.3-B の「ゴミ箱へ移動」のために残す
    rules = dict(re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S)))
    for selector, body in rules.items():
        if ".h3-btn" in selector:
            assert "#d92d20" not in body and "red" not in body.lower(), selector


def test_panel_and_buttons_use_different_backgrounds(ui_config):
    """パネル背景とボタン背景が同じ色にならない（同化の再発防止）。"""
    rules = dict(
        re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", MOBILE_CSS, flags=re.S))
    )

    def background_of(name):
        body = next(b for sel, b in rules.items() if sel.strip().startswith(name))
        return re.search(r"background:\s*([^;!]+)", body).group(1).strip()

    panel = background_of(".h3-panel")
    button = background_of(".h3-btn,")
    assert panel != button, "パネルとボタンの背景が同じ色です（同化の再発）"
    assert button == "#ffffff"
    # 見分けの主役は枠線（背景差はごく僅かなので、枠が無いと境界が消える）
    border = next(b for sel, b in rules.items() if sel.strip().startswith(".h3-btn,"))
    assert "border: 1px solid" in border
    panel_body = next(b for sel, b in rules.items() if sel.strip().startswith(".h3-panel"))
    assert "border: 1px solid" in panel_body


def test_new_labels_replace_the_ambiguous_delete_wording(ui_config):
    """「削除」を「候補から外す」へ。ファイル削除と混同させない（P5.3-A 仕上げ）。"""
    assert CUSTOM_UP_LABEL == "↑ 1つ上へ"
    assert CUSTOM_DOWN_LABEL == "↓ 1つ下へ"
    assert CUSTOM_REMOVE_LABEL == "－ 候補から外す"
    assert CUSTOM_CLEAR_LABEL == "連結候補をすべて解除"

    values = [str(b["props"].get("value") or "") for b in _components(ui_config, "button")]
    for label in (CUSTOM_UP_LABEL, CUSTOM_DOWN_LABEL, CUSTOM_REMOVE_LABEL, CUSTOM_CLEAR_LABEL):
        assert label in values
    for old in ("↑ 上へ", "↓ 下へ", "－ 削除", "選択をすべて解除"):
        assert old not in values, f"古い文言が残っています: {old}"

    # 対象選択欄の説明も揃える
    target = _only(_by_label(ui_config, "dropdown", "対象を選ぶ"), "対象選択欄")
    assert target["props"]["label"] == "対象を選ぶ（順番の入れ替え・候補から外す）"

    # 「動画は削除されない」ことがパネルの説明に書いてある
    notes = [
        str(c["props"].get("value") or "")
        for c in _components(ui_config, "markdown")
        if "h3-note" in _classes(c)
    ]
    assert any("動画は削除されません" in n for n in notes)


def test_appearance_change_does_not_touch_the_wiring(offline_demo):
    """外観だけの変更で、配線（inputs/outputs）は P5.2 のまま。"""
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}

    # 6ハンドラが健在で、State を受けて State を返す形も変わらない
    for api_name in (
        "on_custom_add", "on_custom_up", "on_custom_down",
        "on_custom_remove", "on_custom_clear", "on_custom_start",
    ):
        fn = by_api[api_name]
        assert type(fn.inputs[0]).__name__ == "State"
        assert type(fn.outputs[0]).__name__ == "State"
    assert len(by_api["on_custom_add"].inputs) == 2   # State ＋ 候補ドロップダウン
    assert len(by_api["on_custom_clear"].inputs) == 1  # State だけ
    assert len(by_api["on_videos_tick"].outputs) == 4  # 契約は不変


# ------------------------------- P5.3-B アプリ内ゴミ箱（③の整理セクション）


def test_trash_section_is_hidden_until_a_video_is_selected(ui_config):
    """整理セクションは**動画を選ぶまで隠れている**（誤操作を減らす）。

    P6 の高品質化セクションも同じ方針なので、初期非表示のパネルは2つある。
    ここでは**整理の見出しを含むほう**が隠れていることを確かめる。
    """
    panels = [c for c in ui_config["components"] if "h3-panel" in _classes(c)]
    hidden = [p for p in panels if p["props"].get("visible") is False]
    assert len(hidden) == 2, "初期非表示の整理パネルと高品質化パネルがあるはず"

    parents = _parents(ui_config)
    hidden_ids = {p["id"] for p in hidden}
    for heading_text in ("### この動画を整理する", "### この動画を1080pにする"):
        heading = _only(_by_value(ui_config, "markdown", heading_text), "見出し")
        assert parents[heading["id"]] in hidden_ids


def test_trash_button_is_a_stop_variant_and_starts_hidden(ui_config):
    """危険操作なので赤系（stop）。P5.3-A で温存しておいた色をここで使う。"""
    button = _only(
        [
            b for b in _components(ui_config, "button")
            if str(b["props"].get("value") or "").startswith("🗑 ゴミ箱へ移動")
        ],
        "ゴミ箱ボタン",
    )
    assert button["props"].get("variant") == "stop"
    assert button["props"].get("visible") is False
    assert "h3-tap" in _classes(button)  # iPhone の 44px タップ領域

    # 連結の補助ボタンは赤系にしない（P5.3-A の約束を守れているか）
    for text in (CUSTOM_UP_LABEL, CUSTOM_REMOVE_LABEL, CUSTOM_CLEAR_LABEL):
        helper = _only(_by_value(ui_config, "button", text), text)
        assert helper["props"].get("variant") != "stop"


def test_trash_requires_an_explicit_confirmation_checkbox(ui_config):
    """確認チェックが無いと押せない（ワーカー再起動と同じ形）。"""
    checks = [
        c for c in _components(ui_config, "checkbox")
        if "ゴミ箱へ移動することを確認" in str(c["props"].get("label") or "")
    ]
    assert len(checks) == 1
    assert checks[0]["props"].get("value") is False


def test_trash_note_explains_where_files_go_and_no_restore(ui_config):
    """移動先と「復元機能が無いこと」を先に伝える。"""
    notes = [
        str(c["props"].get("value") or "")
        for c in _components(ui_config, "markdown")
        if "h3-note" in _classes(c)
    ]
    note = next(n for n in notes if "ゴミ箱" in n or "trash" in n)
    assert "data/trash/" in note
    assert "復元機能はありません" in note
    assert "連結動画は消えません" in note  # 連動削除しないことも伝える


def test_trash_wiring_sends_only_the_selection_key(offline_demo):
    """UI から渡すのは選択キーと確認チェックだけ（パスは渡さない）。"""
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    fn = by_api["on_move_to_trash"]

    assert len(fn.inputs) == 2
    assert "表示する動画を選ぶ" in str(getattr(fn.inputs[0], "label", ""))
    assert type(fn.inputs[1]).__name__ == "Checkbox"
    # 成功時にまとめて片づける先（選択・プレビュー・要約・候補・確認チェック）
    assert len(fn.outputs) == 10


def test_timer_does_not_touch_the_trash_controls(offline_demo):
    """Timer が整理セクションを触らない（1秒ごとに確認チェックが外れない）。"""
    _cfg, _service, demo = offline_demo
    by_api = {f.api_name: f for f in demo.fns.values()}
    tick = by_api["on_videos_tick"].outputs
    assert len(tick) == 4  # P5.3-A の契約は維持

    trash_controls = {id(o) for o in by_api["on_move_to_trash"].outputs
                      if type(o).__name__ in ("Checkbox", "Group")}
    assert not (trash_controls & {id(o) for o in tick})
    # 編集中の連結順も従来どおり触らない
    assert "State" not in {type(o).__name__ for o in tick}


def test_no_restore_or_hidden_filter_ui_exists(ui_config):
    """復元UI・非表示フィルタは作らない（簡易方式の確認）。"""
    labels = [str(b["props"].get("value") or "") for b in _components(ui_config, "button")]
    assert not any("復元" in v or "元に戻す" in v for v in labels)

    radios = [
        r for r in _components(ui_config, "radio")
        if "状態フィルタ" in str(r["props"].get("label") or "")
    ]
    choices = [
        c[0] if isinstance(c, (list, tuple)) else c for c in radios[0]["props"]["choices"]
    ]
    assert not any("ゴミ箱" in c or "非表示" in c for c in choices)
    # 種類で絞る2つ（P5.3-A の連結成果物・P6 の1080p成果物）が末尾に並ぶ
    assert choices[-2:] == ["連結成果物", "1080p成果物"]
