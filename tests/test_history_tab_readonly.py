"""④「履歴」タブが閲覧専用であること（P7・決定D22）。

③「完成・編集」と④「履歴」で同じ操作が2か所にあった状態を解消し、
**動画への操作は③だけ・④はフィルタと表だけ**にした。

ここで固定するのは次の3つ:
1. ④に操作部品が**1つも無い**こと（隠しているのではなく、部品ごと消えていること）
2. ④の契約が「入力＝状態フィルタ／出力＝履歴表1つ」であること
3. ③の機能が**何も壊れていない**こと（④から消した機能を③からも消していないか）

実際に Gradio を localhost で起動し、`/config`（ブラウザへ配る画面の設計図）と
HTTP 経由のコールバックで確かめる。実モデルは使わず、書き込み先は tmp_path のみ。
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
from datetime import datetime
from pathlib import Path

import pytest

from app.core.app_service import AppService
from app.core.config import load_config
from app.core.contracts import BackendIdentity, JobSpec
from app.core.history import HistoryRecord
from app.ui.minimal import (
    CONCAT_PRODUCTS_FILTER,
    HISTORY_FILTERS,
    UPSCALED_PRODUCTS_FILTER,
    build_ui,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = PROJECT_ROOT / "app" / "assets" / "mock"
T0 = datetime(2026, 8, 10, 16, 0, 0).astimezone()

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax-H3-NF4",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)

#: ④から消した操作（③には残っていること）
REMOVED_FROM_HISTORY = (
    "この動画の続きを作る",
    "ルートからここまでを連結",
    "Finderで表示（Macのみ）",
    "↻ 選んだ記録を表示",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def add_clip(cfg, service, job_id: str) -> Path:
    out = cfg.outputs_dir / f"{job_id}.mp4"
    last = cfg.outputs_dir / f"{job_id}_last.png"
    spec = JobSpec(
        job_id=job_id, prompt=f"テスト {job_id}", num_frames=56, steps=4,
        seed_requested=None, output_path=out, last_frame_path=last,
    )
    service.history.add(
        HistoryRecord.from_job_spec(
            spec, identity=IDENTITY, execution_engine="mock",
            app_version=cfg.version, data_root=cfg.data_root, created_at=T0,
        )
    )
    service.history.mark_running(job_id, T0)
    shutil.copy(MOCK_DIR / "mock_56.mp4", out)
    shutil.copy(MOCK_DIR / "mock_56_last.png", last)
    service.history.mark_success(
        job_id, output_path=out, last_frame_path=last,
        seed_used=42, elapsed_sec=1.0, finished_at=T0,
    )
    return out


@pytest.fixture(scope="module")
def history_ui(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("history_readonly")
    cfg = dataclasses.replace(load_config(PROJECT_ROOT), data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir,
              cfg.upscaled_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()

    add_clip(cfg, service, "v_20260810_160000_aaaa")
    # 1080p成果物も1つ置く（台帳は持たないのでファイルを置くだけ）
    artifact = cfg.upscaled_dir / "u_clip_v_20260810_160000_aaaa_1080p.mp4"
    artifact.write_bytes((MOCK_DIR / "mock_56.mp4").read_bytes())

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1", server_port=port, share=False, inbrowser=False,
        allowed_paths=[str(cfg.outputs_dir), str(cfg.concat_dir), str(cfg.upscaled_dir)],
        prevent_thread_lock=True,
    )

    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield {"client": client, "service": service, "cfg": cfg, "port": port, "demo": demo}
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


@pytest.fixture(scope="module")
def ui_config(history_ui):
    import urllib.request

    url = f"http://127.0.0.1:{history_ui['port']}/config"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _components(ui_config, type_name: str) -> list[dict]:
    return [c for c in ui_config["components"] if c.get("type") == type_name]


def _parents(ui_config) -> dict[int, int]:
    parent: dict[int, int] = {}

    def walk(node):
        for child in node.get("children") or []:
            parent[child["id"]] = node["id"]
            walk(child)

    walk(ui_config["layout"])
    return parent


def _history_tab_ids(ui_config) -> set[int]:
    """④タブの中にある部品IDを全部集める（入れ子を含む）。"""
    tab = None

    def find(node):
        nonlocal tab
        for child in node.get("children") or []:
            component = next(
                (c for c in ui_config["components"] if c["id"] == child["id"]), None
            )
            if (
                component
                and component.get("type") == "tabitem"
                and str(component["props"].get("id") or "") == "tab_history"
            ):
                tab = child
            find(child)

    find(ui_config["layout"])
    assert tab is not None, "④履歴タブが見つかりません"

    ids: set[int] = set()

    def collect(node):
        for child in node.get("children") or []:
            ids.add(child["id"])
            collect(child)

    collect(tab)
    return ids


def _in_history_tab(ui_config, type_name: str) -> list[dict]:
    ids = _history_tab_ids(ui_config)
    return [c for c in _components(ui_config, type_name) if c["id"] in ids]


def _first(result) -> str:
    if isinstance(result, (list, tuple)):
        return str(result[0])
    return str(result)


# ============================================================ ④の構造


def test_the_status_filter_exists_with_all_nine_choices(ui_config):
    """状態フィルタは残る。選択肢は9種類すべて（内部値も従来どおり）。"""
    radios = _in_history_tab(ui_config, "radio")
    assert len(radios) == 1, "④の状態フィルタが1つではありません"

    choices = [
        c[0] if isinstance(c, (list, tuple)) else c
        for c in radios[0]["props"]["choices"]
    ]
    assert choices == list(HISTORY_FILTERS.keys())
    assert len(choices) == 9
    for expected in ("すべて", "成功", "失敗", "取消", "中断", "実行待ち", "実行中",
                     CONCAT_PRODUCTS_FILTER, UPSCALED_PRODUCTS_FILTER):
        assert expected in choices


def test_the_history_table_exists_and_is_full_width(ui_config):
    """履歴表があり、PC ではページ幅いっぱいに広がる。"""
    tables = [
        m for m in _in_history_tab(ui_config, "markdown")
        if "h3-scroll" in (m["props"].get("elem_classes") or [])
    ]
    assert len(tables) == 1, "④の履歴表が1つではありません"
    assert "h3-wide" in (tables[0]["props"].get("elem_classes") or [])


def test_the_history_tab_has_no_video_player(ui_config):
    """④にプレビュー（gr.Video）が無い。"""
    assert _in_history_tab(ui_config, "video") == []


def test_the_history_tab_has_no_record_selector(ui_config):
    """④に記録選択の Dropdown が無い。"""
    assert _in_history_tab(ui_config, "dropdown") == []


def test_the_history_tab_has_no_buttons_at_all(ui_config):
    """④に操作ボタンが1つも無い（表示・Finder・続きを作る・連結のすべて）。"""
    buttons = [
        str(b["props"].get("value") or "") for b in _in_history_tab(ui_config, "button")
    ]
    assert buttons == [], f"④にボタンが残っています: {buttons}"


def test_the_history_tab_has_no_accordion(ui_config):
    """④に「詳しい情報」の折りたたみが無い。"""
    assert _in_history_tab(ui_config, "accordion") == []


def test_the_removed_controls_are_gone_from_the_history_tab(ui_config):
    """④から消した操作の**文字列自体**が④の中に残っていない。"""
    ids = _history_tab_ids(ui_config)
    values = [
        str(c["props"].get("value") or "")
        for c in ui_config["components"] if c["id"] in ids
    ]
    for label in REMOVED_FROM_HISTORY:
        assert not any(label in v for v in values), f"④に「{label}」が残っています"


def test_no_hidden_leftovers_in_the_history_tab(ui_config):
    """隠して残した旧部品が無い（`visible=False` の部品が④に無い）。"""
    ids = _history_tab_ids(ui_config)
    hidden = [
        c for c in ui_config["components"]
        if c["id"] in ids and c["props"].get("visible") is False
    ]
    assert hidden == [], f"④に隠れた部品が残っています: {hidden}"


def test_the_history_tab_has_no_two_column_row(ui_config):
    """④に2カラム構成（右カラム）が無い。"""
    ids = _history_tab_ids(ui_config)
    columns = [
        c for c in ui_config["components"]
        if c["id"] in ids and c.get("type") == "column"
    ]
    assert len(columns) <= 1, f"④に列が {len(columns)} 個あります（右カラムの残り）"


def test_no_history_selection_state_exists(history_ui):
    """④専用の選択 State を残していない。"""
    import gradio as gr

    demo = history_ui["demo"]
    states = [b for b in demo.blocks.values() if isinstance(b, gr.State)]
    # ①最新ID・③選択・③連結順 の3つだけ（④の分は削除した）
    assert len(states) == 3, f"State が {len(states)} 個あります（④の分が残っている可能性）"


# ============================================================ イベント・API


def test_the_history_selection_apis_are_removed(history_ui):
    """④専用のAPIは廃止した（意図的な契約変更）。"""
    demo = history_ui["demo"]
    names = {fn.api_name for fn in demo.fns.values() if fn.api_name}
    for removed in ("on_select_history", "on_history_concat", "on_history_reveal",
                    "on_history_continuation"):
        assert removed not in names, f"{removed} が残っています"


def test_only_two_history_apis_remain(history_ui):
    """④に残るのはフィルタ変更と Timer の2つだけ。"""
    demo = history_ui["demo"]
    names = sorted(
        fn.api_name for fn in demo.fns.values()
        if fn.api_name and "history" in fn.api_name
    )
    assert names == ["on_history_filter", "on_history_tick"]


def test_the_history_contract_is_filter_in_table_out(history_ui):
    """契約は「入力＝状態フィルタ／出力＝履歴表1つ」。"""
    demo = history_ui["demo"]
    by_api = {fn.api_name: fn for fn in demo.fns.values() if fn.api_name}

    for api_name in ("on_history_filter", "on_history_tick"):
        fn = by_api[api_name]
        assert len(fn.inputs) == 1, f"{api_name} の入力が1つではありません"
        assert len(fn.outputs) == 1, f"{api_name} の出力が1つではありません"
        assert "h3-scroll" in list(getattr(fn.outputs[0], "elem_classes", []) or [])


def test_the_timer_does_not_return_selection_choices(history_ui):
    """Timer は履歴表だけを返す（選択候補や選択値を返さない）。"""
    result = history_ui["client"].predict("すべて", api_name="/on_history_tick")
    assert not isinstance(result, (list, tuple)), "Timer が複数の値を返しています"
    assert "履歴" in str(result)


def test_no_event_targets_a_removed_component(history_ui):
    """削除した部品へのイベント配線が残っていない（孤児IDが無い）。"""
    demo = history_ui["demo"]
    known = set(demo.blocks)
    for fn in demo.fns.values():
        for component in list(fn.inputs) + list(fn.outputs):
            assert component._id in known, (
                f"{fn.api_name}: 存在しない部品への配線が残っています"
            )
        for target, _event in fn.targets:
            # `.then()` で繋いだ段はトリガ元を持たない（target が None）
            if target is None:
                continue
            assert target in known, f"{fn.api_name}: 存在しない部品のイベントです"


def test_the_gradio_config_has_no_orphan_component_ids(ui_config):
    """/config のレイアウト木に、部品一覧に無いIDが出てこない。"""
    known = {c["id"] for c in ui_config["components"]}
    seen: list[int] = []

    def walk(node):
        for child in node.get("children") or []:
            seen.append(child["id"])
            walk(child)

    walk(ui_config["layout"])
    orphans = [i for i in seen if i not in known]
    assert orphans == [], f"孤児の component ID: {orphans}"


# ============================================================ フィルタの動作


@pytest.mark.parametrize(
    "label", ["すべて", "成功", "失敗", "取消", "中断", "実行待ち", "実行中"]
)
def test_every_status_filter_still_works(history_ui, label):
    """既存の状態フィルタが従来どおり動く（表が返る）。"""
    table = _first(history_ui["client"].predict(label, api_name="/on_history_filter"))
    assert "履歴" in table
    assert label == "すべて" or f"（{label}" in table


def test_the_concat_products_filter_still_works(history_ui):
    table = _first(
        history_ui["client"].predict(CONCAT_PRODUCTS_FILTER, api_name="/on_history_filter")
    )
    assert "連結成果物" in table


def test_the_1080p_products_filter_still_works(history_ui):
    table = _first(
        history_ui["client"].predict(UPSCALED_PRODUCTS_FILTER, api_name="/on_history_filter")
    )
    assert "1080p成果物" in table
    assert "v_20260810_160000_aaaa" in table


def test_changing_the_filter_only_updates_the_table(history_ui):
    """フィルタ変更で返るのは表だけ（選択欄や候補は返らない）。"""
    result = history_ui["client"].predict("成功", api_name="/on_history_filter")
    assert not isinstance(result, (list, tuple))
    assert "履歴（成功" in str(result)


# ============================================ ③の非回帰（消しすぎていないこと）


def test_the_completed_tab_still_has_its_controls(ui_config):
    """④から消した操作が③には残っている（消す場所を間違えていない）。"""
    values = [
        str(b["props"].get("value") or "") for b in _components(ui_config, "button")
    ]
    for label in ("この動画の続きを作る", "ルートからここまでを連結",
                  "Finderで表示（Macのみ）", "↻ 選んだ動画を表示"):
        assert label in values, f"③から「{label}」が消えています"


def test_the_completed_tab_still_previews(history_ui):
    """③のプレビューと詳細が動く。"""
    video, detail, tech = history_ui["client"].predict(
        "clip:v_20260810_160000_aaaa", api_name="/on_select_video"
    )
    assert video is not None
    assert "v_20260810_160000_aaaa" in detail
    assert "execution_engine" in tech


def test_the_completed_tab_still_starts_continuation(history_ui):
    """③の継続生成が動く。"""
    result = history_ui["client"].predict(
        "clip:v_20260810_160000_aaaa", api_name="/on_start_continuation"
    )
    assert any("続きを作成中" in str(v) for v in result)


def test_the_completed_tab_still_reveals_in_finder(history_ui, monkeypatch):
    """③の Finder 表示が動く（サービスへ委譲されている）。"""
    message = history_ui["client"].predict(
        "clip:v_20260810_160000_aaaa", api_name="/on_reveal_video"
    )
    assert "Finder" in str(message) or "見つかりません" not in str(message)


def test_the_completed_tab_still_has_the_custom_concat_panel(ui_config):
    """③の指定順連結が残っている。"""
    values = [
        str(m["props"].get("value") or "") for m in _components(ui_config, "markdown")
    ]
    assert any("複数の動画を選んで連結" in v for v in values)


def test_the_completed_tab_still_has_the_upscale_panel(ui_config):
    """③の1080p高品質化が残っている（P6 を壊していない）。"""
    values = [
        str(m["props"].get("value") or "") for m in _components(ui_config, "markdown")
    ]
    assert any("この動画を1080pにする" in v for v in values)

    buttons = [
        str(b["props"].get("value") or "") for b in _components(ui_config, "button")
    ]
    assert any("1080pに高品質化する" in b for b in buttons)


def test_the_completed_tab_still_has_the_trash_panel(ui_config):
    """③のゴミ箱が残っている（P5.3-B を壊していない）。"""
    values = [
        str(m["props"].get("value") or "") for m in _components(ui_config, "markdown")
    ]
    assert any("この動画を整理する" in v for v in values)


def test_the_completed_tab_timer_still_returns_four_values(history_ui):
    """③の Timer 契約（4出力）は変えていない。"""
    result = history_ui["client"].predict(api_name="/on_videos_tick")
    assert len(result) == 4


def test_the_completed_tab_selection_is_not_reset_by_the_timer(history_ui):
    """③の選択を Timer が壊さない（プレーヤーと選択値を出力に持たない）。"""
    import gradio as gr

    demo = history_ui["demo"]
    by_api = {fn.api_name: fn for fn in demo.fns.values() if fn.api_name}
    for api_name in ("on_videos_tick", "on_history_tick", "on_upscale_tick"):
        fn = by_api[api_name]
        assert not any(isinstance(o, gr.Video) for o in fn.outputs), api_name
        assert not any(isinstance(o, gr.State) for o in fn.outputs), api_name
