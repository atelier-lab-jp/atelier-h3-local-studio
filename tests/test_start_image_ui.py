"""開始画像から動画を作る画面（P8・設計書 §28）。

`test_upscale_ui.py` と同じやり方で、実際に Gradio を localhost で起動して
`/config`（ブラウザへ配る画面の設計図）と HTTP 経由のコールバックを見る。

**確認できること／できないこと**（正直に書く）:

- ブラウザ（Safari）は起動しないので、実際の描画結果は検証できない。ここで見るのは
  部品の構造・初期表示・文言・配線・HTTP で実際にやり取りされる値である。
- Execution Engine は必ず MockEngine（**実モデルは絶対に起動しない**）。
- 書き込み先は `tmp_path` と Gradio の受信用一時フォルダのみ
  （プロジェクトの `data/` には一切触れない）。
- 試験用の画像は **Gradio が受信ファイルを置く場所**へ作る。UI はそこを信頼境界として
  下位層へ渡すので、そこに置いてこそ「本物のアップロード」と同じ経路を通せる
  （置き場所がずれていると、正しい画像まで断られる不具合を見逃す）。

この試験が固定する契約（P8 で壊してはいけないもの）:
- `gr.State` は3個のまま／`h3-panel` は3個のまま（初期非表示は2個のまま）
- Timer（6つの tick）は開始画像の部品に**一切触れない**。出力の個数も不変
- 既存7つの固定 API と `/on_submit_v2` の形は不変。投入は新しい `/on_submit_v3`
- ブラウザへ返すのは**サーバ採番の ID と画像そのものだけ**（パスを返さない）
- `allowed_paths` と `_servable()` は無変更（`data/start_images` は配信しない）
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import shutil
import socket
from pathlib import Path

import pytest

from app.core.app_service import AppService
from app.core.config import load_config
from app.ui.minimal import (
    START_IMAGE_CLEAR_LABEL,
    START_IMAGE_ID_PATTERN,
    START_IMAGE_LABEL,
    START_IMAGE_NOTE,
    START_IMAGE_PREVIEW_LABEL,
    START_IMAGE_SELECTED,
    START_IMAGE_SUBMIT_HINT,
    SUBMIT_LABEL,
    StartImageError,
    build_ui,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LENGTH_56 = "約2.33秒（56フレーム）"
STEPS_4 = "4ステップ（高速）"

#: 開始画像ID の形（UI が返してよい唯一の識別子）
ID_RE = re.compile(r"^si_[0-9a-f]{12}$")


def _service_accepts_start_image(service) -> bool:
    """投入口が `start_image_id` を受けるか（UI の `_has_start_image` と同じ判定）。"""
    target = getattr(service, "submit_generation_ex", None)
    if target is None:  # pragma: no cover - AppService には必ずある
        return False
    params = inspect.signature(target).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "start_image_id" in params


class ServiceWithoutStartImage:
    """P8 API を持たない古い AppService に見せかける薄い包み（防御の確認用）。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name == "prepare_start_image":
            raise AttributeError(name)
        return getattr(self._inner, name)


# ------------------------------------------------------------------ 素材と起動


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_png(path: Path, size, color=(120, 160, 200)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


@pytest.fixture(scope="module")
def images():
    """試験用の画像を **Gradio の受信用フォルダ**へ置く。

    UI は「Gradio が受信ファイルを置く場所の配下か」を下位層に検証させるので、
    コールバックを直接呼ぶ試験でも同じ場所から渡さないと本番と経路が変わる。
    使い終わったら自分が作ったフォルダごと片づける。
    """
    from gradio.utils import get_upload_folder

    root = Path(get_upload_folder()).resolve() / "atelier_h3_start_image_tests"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield {
            "root": root,
            "wide": _write_png(root / "wide.png", (1920, 1080)),
            "square": _write_png(root / "square.png", (1200, 1200)),
            "exact": _write_png(root / "exact.png", (576, 320)),
            "tiny": _write_png(root / "tiny.png", (100, 60)),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_service(tmp: Path):
    cfg = dataclasses.replace(load_config(PROJECT_ROOT), data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir,
              cfg.upscaled_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()  # 履歴だけ読む（queue.start() は呼ばない＝生成は始まらない）
    return cfg, service


@pytest.fixture(scope="module")
def start_image_ui(tmp_path_factory):
    """開始画像に対応した AppService で起動した UI（mock モード）。"""
    cfg, service = _make_service(tmp_path_factory.mktemp("start_image_ui"))
    assert hasattr(service, "prepare_start_image") and _service_accepts_start_image(
        service
    ), "AppService に P8 API（prepare_start_image / start_image_id）がありません"

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        # main.py と同じ3つだけ（`data/start_images` は**入れない**）
        allowed_paths=[str(cfg.outputs_dir), str(cfg.concat_dir), str(cfg.upscaled_dir)],
        max_file_size="40mb",
        prevent_thread_lock=True,
    )

    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield {
            "client": client, "service": service, "cfg": cfg,
            "demo": demo, "port": port,
        }
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


@pytest.fixture(scope="module")
def ui_config(start_image_ui):
    import urllib.request

    url = f"http://127.0.0.1:{start_image_ui['port']}/config"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------------ 小道具


def _components(ui_config, type_name: str) -> list[dict]:
    return [c for c in ui_config["components"] if c.get("type") == type_name]


def _props(component: dict) -> dict:
    return component.get("props", {}) or {}


def _classes(component: dict) -> list[str]:
    return list(_props(component).get("elem_classes") or [])


def _by_label(ui_config, type_name: str, text: str) -> list[dict]:
    return [
        c for c in _components(ui_config, type_name)
        if text == str(_props(c).get("label") or "")
    ]


def _only(matches: list[dict], what: str) -> dict:
    assert len(matches) == 1, f"{what} が {len(matches)} 個あります（1個であるべき）"
    return matches[0]


def _parents(ui_config) -> dict[int, int]:
    parent: dict[int, int] = {}

    def walk(node: dict) -> None:
        for child in node.get("children") or []:
            parent[child["id"]] = node["id"]
            walk(child)

    walk(ui_config["layout"])
    return parent


def _subtree_ids(ui_config, root_id: int) -> set[int]:
    """あるレイアウト節点の配下にある部品IDを全部集める。"""

    def find(node: dict) -> dict | None:
        if node["id"] == root_id:
            return node
        for child in node.get("children") or []:
            found = find(child)
            if found is not None:
                return found
        return None

    root = find(ui_config["layout"])
    assert root is not None, f"レイアウトに {root_id} が見つかりません"
    ids: set[int] = set()

    def walk(node: dict) -> None:
        ids.add(node["id"])
        for child in node.get("children") or []:
            walk(child)

    walk(root)
    return ids


def _tab_ids(ui_config, tab_id: str) -> set[int]:
    tab = _only(
        [t for t in _components(ui_config, "tabitem") if _props(t).get("id") == tab_id],
        f"{tab_id} タブ",
    )
    return _subtree_ids(ui_config, tab["id"])


def _ancestors(ui_config, component_id: int) -> set[int]:
    parents = _parents(ui_config)
    seen: set[int] = set()
    while component_id in parents:
        component_id = parents[component_id]
        seen.add(component_id)
    return seen


def _start_image_components(ui_config) -> dict[str, dict]:
    """開始画像の部品を名前で引けるようにする（すべて1個だけであることも確認）。

    メッセージ欄と補助表示は初期値が空なので、**構造**で見分ける
    （初期状態で「空・非表示」の Markdown はこの2つだけ。
    片方は開始画像の器の中、もう片方は器の外＝生成ボタンの近く）。
    """
    heading = _only(
        [
            m for m in _components(ui_config, "markdown")
            if str(_props(m).get("value") or "").strip() == "### 開始画像（任意）"
        ],
        "見出し",
    )
    group_id = _parents(ui_config)[heading["id"]]
    blanks = [
        m for m in _components(ui_config, "markdown")
        if not str(_props(m).get("value") or "") and _props(m).get("visible") is False
    ]
    assert len(blanks) == 2, f"空・非表示の Markdown が {len(blanks)} 個あります"
    inside = [m for m in blanks if group_id in _ancestors(ui_config, m["id"])]
    outside = [m for m in blanks if m not in inside]
    assert len(inside) == 1 and len(outside) == 1

    return {
        "input": _only(_by_label(ui_config, "image", START_IMAGE_LABEL), "開始画像入力"),
        "preview": _only(
            _by_label(ui_config, "image", START_IMAGE_PREVIEW_LABEL), "プレビュー"
        ),
        "id": _only(_by_label(ui_config, "textbox", "開始画像ID"), "開始画像ID"),
        "clear": _only(
            [
                b for b in _components(ui_config, "button")
                if str(_props(b).get("value") or "") == START_IMAGE_CLEAR_LABEL
            ],
            "開始画像を外すボタン",
        ),
        "note": _only(
            [
                m for m in _components(ui_config, "markdown")
                if str(_props(m).get("value") or "").startswith(
                    "画像を指定すると、その画像を動画の第1フレームとして使用します。"
                )
            ],
            "説明文",
        ),
        "heading": heading,
        "msg": inside[0],
        "hint": outside[0],
    }


def _by_api(demo) -> dict:
    return {fn.api_name: fn for fn in demo.fns.values() if fn.api_name}


def _visible_texts(ui_config) -> list[str]:
    """利用者の目に触れる文字列だけを集める（内部の型名などは対象外）。"""
    texts: list[str] = []
    for component in ui_config["components"]:
        props = _props(component)
        for key in ("label", "value", "info", "placeholder"):
            value = props.get(key)
            if isinstance(value, str):
                texts.append(value)
        for choice in props.get("choices") or []:
            if isinstance(choice, (list, tuple)):
                texts.extend(str(c) for c in choice)
            else:
                texts.append(str(choice))
    return texts


# ============================================================ 画面の構造


def test_the_start_image_section_lives_once_inside_the_new_tab(ui_config):
    """開始画像の部品は①新規生成タブの中に**1組だけ**ある。"""
    parts = _start_image_components(ui_config)  # 1個ずつであることは _only が担保
    new_tab = _tab_ids(ui_config, "tab_new")
    for name, component in parts.items():
        assert component["id"] in new_tab, f"{name} が①新規生成タブの外にあります"

    # ②③④には1つも無い
    for tab in ("tab_queue", "tab_videos", "tab_history"):
        ids = _tab_ids(ui_config, tab)
        for name, component in parts.items():
            assert component["id"] not in ids, f"{name} が {tab} にもあります"


def test_the_section_sits_between_the_prompt_helpers_and_the_length_radio(ui_config):
    """置き場所は「プロンプト補助ボタンの直下・長さ選択の直前」。"""
    order: list[int] = []

    def walk(node: dict) -> None:
        order.append(node["id"])
        for child in node.get("children") or []:
            walk(child)

    walk(ui_config["layout"])
    parts = _start_image_components(ui_config)
    hint_btn = _only(
        [
            b for b in _components(ui_config, "button")
            if str(_props(b).get("value") or "") == "＋日本語セリフ記法を挿入"
        ],
        "セリフ記法ボタン",
    )
    length = _only(_by_label(ui_config, "radio", "動画の長さ"), "長さ選択")
    assert order.index(hint_btn["id"]) < order.index(parts["heading"]["id"])
    assert order.index(parts["clear"]["id"]) < order.index(length["id"])


def test_nothing_is_selected_at_first(ui_config):
    """未選択のときは説明だけが出て、プレビューとクリアボタンは隠れている。"""
    parts = _start_image_components(ui_config)
    assert _props(parts["note"]).get("visible") is not False
    assert START_IMAGE_NOTE.startswith(str(_props(parts["note"])["value"])[:20])
    assert _props(parts["preview"]).get("visible") is False
    assert _props(parts["clear"]).get("visible") is False
    assert _props(parts["id"]).get("visible") is False
    assert str(_props(parts["id"]).get("value") or "") == ""
    # 生成ボタンの近くの補助表示も、開始画像が無いうちは出ない
    hint = [
        m for m in _components(ui_config, "markdown")
        if str(_props(m).get("value") or "") == START_IMAGE_SUBMIT_HINT
    ]
    assert hint == [], "開始画像が無いのに『開始画像つきで生成します』が出ています"


def test_the_explanation_says_what_the_app_does_to_the_image(ui_config):
    """引き伸ばさないこと・中央付近に置くこと・対応形式を先に伝える。"""
    note = str(_props(_start_image_components(ui_config)["note"])["value"])
    assert "第1フレーム" in note
    assert "576×320" in note
    assert "PNG・JPEG・WebP" in note
    assert "引き伸ばさず" in note
    assert "中央付近" in note
    # 「16:9」という数値は書かない（出力は 9:5 なので誤解を生む。決定D24）
    assert "16:9" not in note
    assert "HEIC" in note  # iPhone 利用者がいちばん先にぶつかる形式


def test_the_upload_row_stacks_and_keeps_a_44px_tap_target(ui_config):
    """新しい Row には `h3-row`（1カラム化）と `h3-tap`（44px）が付いている。"""
    parents = _parents(ui_config)
    parts = _start_image_components(ui_config)
    row_id = parents[parts["input"]["id"]]
    row = _only([c for c in ui_config["components"] if c["id"] == row_id], "画像の行")
    assert row["type"] == "row"
    assert "h3-row" in _classes(row)
    assert "h3-tap" in _classes(row)
    # 画面全体の約束（すべての Row が1カラム化の対象）も守れている
    missing = [r["id"] for r in _components(ui_config, "row") if "h3-row" not in _classes(r)]
    assert missing == [], f"h3-row が付いていない Row: {missing}"


def test_the_state_count_is_unchanged(start_image_ui):
    """`gr.State` を1つも増やしていない（開始画像IDは隠しテキストボックス）。"""
    import gradio as gr

    states = [b for b in start_image_ui["demo"].blocks.values() if isinstance(b, gr.State)]
    assert len(states) == 3, f"State が {len(states)} 個あります（P8 で増やしてはいけない）"


def test_the_panel_counts_are_unchanged(ui_config):
    """`h3-panel` は3個のまま・初期非表示は2個のまま（開始画像には付けない）。"""
    panels = [c for c in ui_config["components"] if "h3-panel" in _classes(c)]
    assert len(panels) == 3, "連結・整理・高品質化の3つ以外にパネルを増やさない"
    hidden = [p for p in panels if _props(p).get("visible") is False]
    assert len(hidden) == 2

    parts = _start_image_components(ui_config)
    parents = _parents(ui_config)
    group_id = parents[parts["heading"]["id"]]
    group = _only([c for c in ui_config["components"] if c["id"] == group_id], "開始画像の器")
    assert group["type"] == "group"
    assert "h3-panel" not in _classes(group)


def test_the_clear_button_is_not_a_primary_or_stop_button(ui_config):
    """「外す」は補助操作。赤（stop）にも塗りつぶし（primary）にもしない。"""
    clear = _start_image_components(ui_config)["clear"]
    assert _props(clear).get("variant") not in ("primary", "stop")
    assert "h3-btn" in _classes(clear)


def test_the_submit_button_label_is_unchanged(ui_config):
    """生成ボタンの文言は P8 でも変えない。"""
    buttons = [
        b for b in _components(ui_config, "button")
        if str(_props(b).get("value") or "") == SUBMIT_LABEL
    ]
    assert len(buttons) == 1


def test_no_crop_editor_no_mode_radio_no_asset_library(ui_config):
    """作らないと決めたUI（禁止事項）が入り込んでいない。"""
    types = {c.get("type") for c in ui_config["components"]}
    assert "imageeditor" not in types, "インタラクティブなクロップエディタは作らない"
    assert "gallery" not in types, "素材ライブラリは作らない"
    assert "file" not in types and "fileexplorer" not in types

    for radio in _components(ui_config, "radio"):
        label = str(_props(radio).get("label") or "")
        assert "開始画像" not in label, "モード選択ラジオは作らない"
    # 利用者が入れられる画像は1枚だけ（複数画像・参照動画・参照音楽を足さない）。
    # 表示専用（`interactive=False`）の継続元サムネイル・プレビューは数えない。
    uploads = [
        c for c in _components(ui_config, "image")
        if _props(c).get("interactive") is not False
        and "upload" in (_props(c).get("sources") or [])
    ]
    assert len(uploads) == 1, "アップロードできる画像欄は開始画像の1つだけ"
    assert uploads[0]["id"] == _start_image_components(ui_config)["input"]["id"]
    assert not _components(ui_config, "audio")


def test_the_ui_never_mentions_ref2va(ui_config):
    """旧 Ref2VA の語彙を画面に出さない（P8 は FL2VA の第1フレーム条件）。"""
    texts = " ".join(_visible_texts(ui_config)).lower()
    for banned in ("ref2va", "reference", "参照動画", "参照音楽", "参照画像"):
        assert banned.lower() not in texts, f"UI に「{banned}」が出ています"


# ============================================================ Timer との分離


def test_no_timer_output_touches_the_start_image(start_image_ui, ui_config):
    """6つの tick はどれも開始画像の部品を出力に持たない（毎秒消えない）。"""
    demo = start_image_ui["demo"]
    by_api = _by_api(demo)
    parts = _start_image_components(ui_config)
    protected = {c["id"] for c in parts.values()}

    tick_names = (
        "on_tick", "on_queue_tick", "on_videos_tick",
        "on_upscale_tick", "on_history_tick", "on_queue_detail_tick",
    )
    for name in tick_names:
        fn = by_api[name]
        touched = {o._id for o in fn.outputs} & protected
        assert not touched, f"{name} が開始画像の部品を上書きします: {touched}"


def test_timer_output_counts_are_unchanged(start_image_ui):
    """既存6つの tick の出力の個数は P8 でも増減しない。"""
    by_api = _by_api(start_image_ui["demo"])
    expected = {
        "on_tick": 4,
        "on_queue_tick": 6,
        "on_videos_tick": 4,   # P5.3-A からの契約
        "on_upscale_tick": 2,
        "on_history_tick": 1,  # P7 からの契約
        "on_queue_detail_tick": 1,
    }
    actual = {name: len(by_api[name].outputs) for name in expected}
    assert actual == expected


def test_only_two_events_touch_the_submit_button(start_image_ui):
    """生成ボタンを出力に持つのは無効化と再有効化の2つだけ（P5 §6.2 の契約）。"""
    import gradio as gr

    demo = start_image_ui["demo"]
    submit = _only_block(demo, lambda b: isinstance(b, gr.Button) and b.value == SUBMIT_LABEL)
    touching = [fn for fn in demo.fns.values() if submit in fn.outputs]
    assert len(touching) == 2


def _only_block(demo, predicate):
    blocks = [b for b in demo.blocks.values() if predicate(b)]
    assert len(blocks) == 1, f"部品が {len(blocks)} 個見つかりました"
    return blocks[0]


# ============================================================ 配線と API


def test_the_clear_callback_takes_no_arguments(start_image_ui, ui_config):
    """［開始画像を外す］は引数を取らず、開始画像の部品だけを更新する。"""
    fn = _by_api(start_image_ui["demo"])["on_clear_start_image"]
    assert fn.inputs == [], "外す操作に入力を渡さない（他の欄を読めないようにする）"
    assert inspect.signature(fn.fn).parameters == {}

    parts = _start_image_components(ui_config)
    allowed = {c["id"] for c in parts.values()}
    outside = {o._id for o in fn.outputs} - allowed
    assert not outside, f"開始画像以外の部品を書き換えます: {outside}"
    assert len(fn.outputs) == 7  # 入力欄・ID・プレビュー・説明・メッセージ・ボタン・補助表示

    # 戻り値は固定（サービス層を呼ばない）
    cleared = fn.fn()
    assert cleared[0] is None       # 画像入力を空にする
    assert cleared[1] == ""         # 開始画像ID


def test_the_select_callback_is_wired_to_the_image_input(start_image_ui, ui_config):
    """選択の配線は画像入力1つを受け、開始画像の部品だけを更新する。"""
    fn = _by_api(start_image_ui["demo"])["on_start_image"]
    parts = _start_image_components(ui_config)
    assert len(fn.inputs) == 1
    assert fn.inputs[0]._id == parts["input"]["id"]
    allowed = {c["id"] for c in parts.values()}
    assert {o._id for o in fn.outputs} <= allowed
    assert len(fn.outputs) == 6


def test_on_start_continuation_still_returns_nine_values(start_image_ui):
    """継続開始の戻り値は9個のまま（開始画像の出し入れは別の配線で行う）。"""
    demo = start_image_ui["demo"]
    fn = _by_api(demo)["on_start_continuation"]
    # Tabs と Group は HTTP へ載らないので、配線上は11・API 上は9
    assert len(fn.outputs) == 11
    assert len(fn.fn("clip:v_does_not_exist")) == 11

    result = start_image_ui["client"].predict(
        "clip:v_does_not_exist", api_name="/on_start_continuation"
    )
    assert len(result) == 9, "継続開始の戻り値の数が変わっています"


def test_continuation_mode_and_start_image_are_exclusive(start_image_ui, ui_config):
    """継続モードに入ったら開始画像欄を隠して ID を外し、解除で戻す。"""
    demo = start_image_ui["demo"]
    parts = _start_image_components(ui_config)
    parent_box = _only_block(
        demo, lambda b: getattr(b, "label", None) == "継続元の親ID"
    )
    wired = [
        fn for fn in demo.fns.values()
        if fn.targets == [(parent_box._id, "change")]
    ]
    assert len(wired) == 1, "継続モードの変化を見る配線が1つではありません"
    fn = wired[0]
    # 出力は開始画像の部品と、それを包む Group だけ
    assert len(fn.outputs) == 8

    entered = fn.fn("v_20260810_120000_aaaa")
    assert entered[0]["visible"] is False, "継続モードで開始画像欄が隠れていない"
    assert entered[1] is None                     # 画像入力を空にする
    assert entered[2] == ""                       # 開始画像IDを外す

    left = fn.fn("")
    assert left[0]["visible"] is True, "継続モードを解除しても開始画像欄が戻らない"
    assert left[2] == ""

    # 継続開始そのものは開始画像の部品を1つも出力に持たない（9個契約を守るため）
    start_fn = _by_api(demo)["on_start_continuation"]
    protected = {c["id"] for c in parts.values()}
    assert not ({o._id for o in start_fn.outputs} & protected)


def test_submit_v3_takes_seven_inputs_and_v2_still_takes_six(start_image_ui, ui_config):
    """投入は `/on_submit_v3`（7入力）。`/on_submit_v2`（6入力）も残す。"""
    by_api = _by_api(start_image_ui["demo"])
    v3 = by_api["on_submit_v3"]
    v2 = by_api["on_submit_v2"]
    assert len(v3.inputs) == 7 and len(v3.outputs) == 3
    assert len(v2.inputs) == 6 and len(v2.outputs) == 3
    # 7つ目が開始画像ID（プロンプトや設定の順番は変えていない）
    assert v3.inputs[6]._id == _start_image_components(ui_config)["id"]["id"]
    assert [type(i).__name__ for i in v3.inputs[:6]] == [
        type(i).__name__ for i in v2.inputs
    ]
    # 画面の生成ボタンが呼ぶのは v3
    assert v2.targets != v3.targets


def test_the_seven_frozen_apis_keep_their_shape(start_image_ui):
    """P1〜P3 から固定している7つの API の入出力の数が変わっていない。"""
    by_api = _by_api(start_image_ui["demo"])
    expected = {
        "on_submit": (5, 3),
        "on_tick": (0, 4),
        "on_estimate_change": (2, 1),
        "on_insert_hint": (1, 1),
        "on_queue_tick": (0, 6),
        "on_cancel_queued": (1, 7),
        "on_restart_worker": (1, 7),
    }
    actual = {
        name: (len(by_api[name].inputs), len(by_api[name].outputs)) for name in expected
    }
    assert actual == expected


def test_the_ui_and_the_server_serve_the_same_three_directories():
    """`_servable()` と `main.py` の `allowed_paths` は P8 でも無変更。"""
    ui_source = (PROJECT_ROOT / "app" / "ui" / "minimal.py").read_text(encoding="utf-8")
    main_source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    for name in ("outputs_dir", "concat_dir", "upscaled_dir"):
        assert f"cfg.{name}" in ui_source
        assert f"cfg.{name}" in main_source
    # 開始画像とゴミ箱はどちらからも配信しない
    assert "start_images_dir" not in main_source
    assert "start_images_dir" not in ui_source
    assert "trash_dir" not in main_source

    allowed = re.search(r'"allowed_paths": \[(.*?)\]', main_source, re.S)
    assert allowed is not None
    listed = re.findall(r"cfg\.(\w+)", allowed.group(1))
    assert listed == ["outputs_dir", "concat_dir", "upscaled_dir"]

    bases = re.search(r"bases = \[(.*?)\]", ui_source)
    assert bases is not None
    assert re.findall(r"cfg\.(\w+)", bases.group(1)) == [
        "outputs_dir", "concat_dir", "upscaled_dir"
    ]


# ============================================================ 実際のやり取り


def _select(start_image_ui, path: Path):
    from gradio_client import handle_file

    return start_image_ui["client"].predict(
        handle_file(str(path)), api_name="/on_start_image"
    )


def test_choosing_a_photo_returns_only_an_id(start_image_ui, images):
    """選んだ画像の ID だけが返る（**保存先のパスは返らない**）。"""
    result = _select(start_image_ui, images["wide"])
    image_id = str(result[0])
    assert ID_RE.match(image_id), f"開始画像IDの形が違います: {image_id!r}"
    assert "/" not in image_id and "\\" not in image_id and ".." not in image_id

    joined = " ".join(str(v) for v in result if isinstance(v, (str, int, float)))
    data_root = str(start_image_ui["cfg"].data_root)
    assert data_root not in joined, "保存先のパスが画面へ漏れています"
    assert "start_images" not in joined


def test_choosing_a_photo_shows_the_preview_and_the_hint(start_image_ui, images):
    """選んだあとは確認メッセージが出て、生成ボタンの近くに印が出る。"""
    fn = _by_api(start_image_ui["demo"])["on_start_image"]
    image_id, preview, message, note, clear, hint = fn.fn(str(images["wide"]))

    assert ID_RE.match(image_id)
    # プレビューは**画像そのもの**（パスではない）
    from PIL import Image as PILImage

    assert isinstance(preview["value"], PILImage.Image)
    assert preview["value"].size == (576, 320)
    assert preview["visible"] is True

    assert message["value"].startswith(START_IMAGE_SELECTED)
    assert message["visible"] is True
    assert note["visible"] is False       # 長い説明は引っ込める
    assert clear["visible"] is True
    assert hint["value"] == START_IMAGE_SUBMIT_HINT and hint["visible"] is True


def test_a_square_photo_warns_about_the_crop(start_image_ui, images):
    """正方形の画像は切り取り量が多いので、プレビュー確認をうながす。"""
    fn = _by_api(start_image_ui["demo"])["on_start_image"]
    _id, _preview, message, *_rest = fn.fn(str(images["square"]))
    assert "切り取" in message["value"]


def test_an_unusable_image_is_refused_in_japanese(start_image_ui, images):
    """小さすぎる画像は日本語で断られ、IDは空のまま。"""
    fn = _by_api(start_image_ui["demo"])["on_start_image"]
    image_id, preview, message, note, clear, hint = fn.fn(str(images["tiny"]))

    assert image_id == "", "断ったのにIDが入っています"
    assert message["value"].startswith("❌")
    assert message["visible"] is True
    assert "小さすぎます" in message["value"]
    assert preview["visible"] is False and clear["visible"] is False
    assert note["visible"] is True        # やり直せるよう説明を出したままにする
    assert hint["visible"] is False
    # 例外文・内部パスを画面へ出さない
    assert "Traceback" not in message["value"]
    assert str(start_image_ui["cfg"].data_root) not in message["value"]


def test_a_file_that_is_not_an_image_is_refused(start_image_ui, images):
    """画像でないファイルを渡しても、日本語のメッセージだけを返して落ちない。"""
    broken = images["root"] / "not_an_image.png"
    broken.write_bytes(b"this is not a png")
    fn = _by_api(start_image_ui["demo"])["on_start_image"]
    image_id, _preview, message, *_rest = fn.fn(str(broken))
    assert image_id == ""
    assert message["value"].startswith("❌")
    assert message["visible"] is True


def test_clearing_the_start_image_over_http(start_image_ui, images):
    """［開始画像を外す］で ID が空に戻る（HTTP 経由でも同じ）。"""
    assert ID_RE.match(str(_select(start_image_ui, images["wide"])[0]))
    cleared = start_image_ui["client"].predict(api_name="/on_clear_start_image")
    values = [v for v in cleared if isinstance(v, str)]
    assert "" in values
    assert not any(ID_RE.match(v) for v in values)


def test_the_uploaded_file_stays_inside_the_folder_we_declare(
    start_image_ui, images, monkeypatch
):
    """UI が渡す `upload_root` は、Gradio が実際に置く場所と一致している。

    ここがずれていると、下位層の境界検証が正しい画像まで断ってしまう。
    実際に HTTP でアップロードして、受け取ったパスが宣言した配下かを見る。
    """
    service = start_image_ui["service"]
    original = service.prepare_start_image
    calls: list[tuple[Path, object]] = []

    def spy(src_path, **kwargs):
        calls.append((Path(src_path), kwargs.get("upload_root")))
        return original(src_path, **kwargs)

    monkeypatch.setattr(service, "prepare_start_image", spy)
    image_id = str(_select(start_image_ui, images["wide"])[0])

    assert calls, "prepare_start_image が呼ばれていません"
    assert ID_RE.match(image_id), "宣言した置き場所からの画像が断られています"
    src, upload_root = calls[-1]
    assert upload_root is not None, "受信ファイルの置き場所を渡していません"
    assert src.resolve().is_relative_to(Path(upload_root)), (
        f"受信ファイル {src} が {upload_root} の外にあります"
    )


def test_submitting_with_a_start_image_says_so(start_image_ui, images, monkeypatch):
    """開始画像つきで投入すると、ID がそのままサービスへ渡り、結果にその旨が出る。"""
    service = start_image_ui["service"]
    original = service.submit_generation_ex
    seen: list[object] = []

    def spy(**kwargs):
        seen.append(kwargs.get("start_image_id"))
        return original(**kwargs)

    monkeypatch.setattr(service, "submit_generation_ex", spy)
    image_id = str(_select(start_image_ui, images["wide"])[0])
    message = start_image_ui["client"].predict(
        "開始画像つきの生成 <d>[Japanese] こんにちは</d>",
        LENGTH_56, STEPS_4, True, 42, "", image_id,
        api_name="/on_submit_v3",
    )[0]
    assert message.startswith("✅") or "すでに登録されています" in message
    assert "開始画像つき" in message
    assert seen == [image_id], "開始画像IDがそのまま渡っていません"


def test_submitting_without_a_start_image_is_unchanged(start_image_ui):
    """開始画像を使わない投入は、これまでどおりの文言のまま。"""
    message = start_image_ui["client"].predict(
        "開始画像なしの生成", LENGTH_56, STEPS_4, True, 4242, "", "",
        api_name="/on_submit_v3",
    )[0]
    assert "開始画像" not in message
    assert message.startswith("✅") or "すでに登録されています" in message


def test_a_forged_start_image_id_is_refused(start_image_ui):
    """ブラウザ側で ID を作り替えても通らない（形の検証は UI 側でも行う）。"""
    service = start_image_ui["service"]
    before = len(service.queue.queued_jobs())
    for forged in ("../../etc/passwd", "si_zzzzzzzzzzzz", "/tmp/evil.png", "si_abc"):
        message = start_image_ui["client"].predict(
            "偽のID", LENGTH_56, STEPS_4, True, 7, "", forged,
            api_name="/on_submit_v3",
        )[0]
        assert message.startswith("❌"), f"{forged} が通ってしまいました: {message}"
    assert len(service.queue.queued_jobs()) == before


def test_a_parent_and_a_start_image_cannot_be_combined(start_image_ui, images):
    """継続元と開始画像の同時指定は API を直接叩いても断る。"""
    image_id = str(_select(start_image_ui, images["wide"])[0])
    message = start_image_ui["client"].predict(
        "両方指定", LENGTH_56, STEPS_4, True, 7, "v_20260810_120000_aaaa", image_id,
        api_name="/on_submit_v3",
    )[0]
    assert message.startswith("❌")
    assert "同時に指定できません" in message


def test_the_staged_image_is_not_served_over_http(start_image_ui, images):
    """`data/start_images/` の画像は HTTP で取り出せない（配信対象外）。"""
    import urllib.error
    import urllib.request

    cfg = start_image_ui["cfg"]
    _select(start_image_ui, images["wide"])
    staging = getattr(
        cfg, "start_images_staging_dir", cfg.data_root / "start_images" / "staging"
    )
    staged = sorted(Path(staging).glob("si_*.png"))
    assert staged, "正規化した画像が staging に見当たりません"

    for target in (staged[0], Path(staging).parent):
        url = f"http://127.0.0.1:{start_image_ui['port']}/file={target}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status != 200, f"{target} が HTTP で配信されています"


# ============================================================ 起動と後方互換


def test_the_ui_starts_in_mock_mode(start_image_ui):
    """mock モードで UI が起動して `/config` を返す（実モデルは使わない）。"""
    import urllib.request

    url = f"http://127.0.0.1:{start_image_ui['port']}/"
    with urllib.request.urlopen(url, timeout=10) as resp:
        assert resp.status == 200
        assert "<html" in resp.read(2048).decode("utf-8", "replace").lower()


def test_the_section_is_hidden_when_the_service_cannot_do_it(tmp_path):
    """AppService に P8 API が無い版では、開始画像欄を**出さない**（起動はする）。"""
    import gradio as gr

    cfg, service = _make_service(tmp_path)
    demo = build_ui(cfg, "mock", ServiceWithoutStartImage(service))
    try:
        inputs = [
            b for b in demo.blocks.values()
            if isinstance(b, gr.Image) and b.label == START_IMAGE_LABEL
        ]
        assert len(inputs) == 1
        group = inputs[0].parent.parent  # Row → Group
        assert isinstance(group, gr.Group)
        assert group.visible is False, "P8 API が無いのに開始画像欄が出ています"
        # それでも API の形は変わらない（クライアントの互換のため）
        names = {fn.api_name for fn in demo.fns.values() if fn.api_name}
        assert {"on_submit", "on_submit_v2", "on_submit_v3"} <= names
    finally:
        service.shutdown(timeout=5.0)


def test_the_service_side_api_matches_the_contract(start_image_ui):
    """AppService 側の P8 API が契約どおりの引数を持っている。"""
    service = start_image_ui["service"]
    params = inspect.signature(service.prepare_start_image).parameters
    assert "upload_root" in params
    assert _service_accepts_start_image(service)
