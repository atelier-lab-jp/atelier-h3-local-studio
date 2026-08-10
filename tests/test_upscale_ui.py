"""1080p高品質化の画面（P6・設計書 §26）。

`test_mobile_ui.py` と同じやり方で、実際に Gradio を localhost で起動して
`/config`（ブラウザへ配る画面の設計図）と HTTP 経由のコールバックを見る。

**確認できること／できないこと**（正直に書く）:
- ブラウザは起動しないので、実際の見た目（ボタンが押しやすいか等）は見ていない。
  ここで見るのは部品の構造・初期表示・文言・配線である。
- 実モデルも実ワーカーも動かさない（AppService の高品質化サービスを差し替える）。
- 書き込み先は `tmp_path` のみ。

Gradio 6 の HTTP 経路では `gr.State` / Group / Button の更新はクライアントへ
届かない（P5.2 で確認済み）ので、それらは戻り値の並びから落ちる。
"""

from __future__ import annotations

import dataclasses
import json
import socket
from pathlib import Path

import pytest

from app.core.app_service import AppService
from app.core.config import load_config
from app.core.upscale_service import (
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    UpscaleStatus,
)
from app.ui.minimal import (
    UPSCALE_ALREADY_NOTE,
    UPSCALE_BUTTON_LABEL,
    UPSCALE_CANCEL_LABEL,
    UPSCALE_NOTE,
    UPSCALE_TITLE,
    UPSCALED_PRODUCTS_FILTER,
    build_ui,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = PROJECT_ROOT / "app" / "assets" / "mock"


class FakeUpscale:
    """高品質化サービスの差し替え（プロセスも ffmpeg も起動しない）。"""

    def __init__(self):
        self.started: list = []
        self.status_value = UpscaleStatus()
        self.available = (True, "利用できます")
        self.cancel_message = "高品質化を中止しています…"

    def availability(self):
        return self.available

    def start_upscale(self, request):
        self.started.append(request)
        self.status_value = UpscaleStatus(
            state=STATE_RUNNING,
            key=f"upscale-{len(self.started)}",
            source_key=request.source_key,
            source_label=request.label,
            total=request.num_frames,
            frame=0,
        )
        return self.status_value.key

    def status(self):
        return self.status_value

    def cancel(self):
        return self.cancel_message

    def shutdown(self, timeout: float = 5.0):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def upscale_ui(tmp_path_factory):
    """1080p成果物が1つある状態で起動した UI。"""
    tmp = tmp_path_factory.mktemp("upscale_ui")
    cfg = dataclasses.replace(load_config(PROJECT_ROOT), data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir,
              cfg.upscaled_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()
    fake = FakeUpscale()
    service._upscale = fake

    # 元動画と、その1080p版を1つずつ置く
    # （1080p版は台帳を持たないので、ファイルを置くだけで一覧に出る）
    _add_success_clip(cfg, service, "v_20260810_150000_aaaa")
    artifact = cfg.upscaled_dir / "u_clip_v_20260810_150000_aaaa_1080p.mp4"
    artifact.write_bytes((MOCK_DIR / "mock_56.mp4").read_bytes())

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        allowed_paths=[str(cfg.outputs_dir), str(cfg.concat_dir), str(cfg.upscaled_dir)],
        prevent_thread_lock=True,
    )

    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield {
            "client": client, "service": service, "cfg": cfg,
            "port": port, "fake": fake, "artifact": artifact,
        }
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


@pytest.fixture(scope="module")
def ui_config(upscale_ui):
    import urllib.request

    url = f"http://127.0.0.1:{upscale_ui['port']}/config"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(autouse=True)
def idle_upscale(upscale_ui):
    """各テストは「何も実行していない」状態から始める（状態の持ち越しを防ぐ）。"""
    upscale_ui["fake"].status_value = UpscaleStatus()
    upscale_ui["fake"].available = (True, "利用できます")
    yield
    upscale_ui["fake"].status_value = UpscaleStatus()


def _components(ui_config, type_name: str) -> list[dict]:
    return [c for c in ui_config["components"] if c.get("type") == type_name]


def _button(ui_config, text: str) -> dict:
    matches = [
        b for b in _components(ui_config, "button")
        if str(b["props"].get("value") or "") == text
    ]
    assert len(matches) == 1, f"「{text}」が {len(matches)} 個あります"
    return matches[0]


def _first(result) -> str:
    """gradio_client の戻り値を文字列にする。

    出力が1つだけの経路では**タプルではなく値そのもの**が返るので、
    `result[0]` と書くと文字列の1文字目を取ってしまう。
    """
    if isinstance(result, (list, tuple)):
        return str(result[0])
    return str(result)


def _upscale_tick_text(upscale_ui) -> str:
    return _first(upscale_ui["client"].predict(api_name="/on_upscale_tick"))


def _markdown_values(ui_config) -> list[str]:
    return [str(m["props"].get("value") or "") for m in _components(ui_config, "markdown")]


# ============================================================ 画面の構造


def test_the_upscale_section_lives_in_the_completed_tab(ui_config):
    """③完成・編集タブに高品質化の見出しがある（④や①には作らない）。"""
    assert f"### {UPSCALE_TITLE}" in _markdown_values(ui_config)


def test_the_section_is_hidden_until_a_video_is_selected(ui_config):
    """動画を選ぶまでは出さない（誤操作を減らす）。"""
    parents = {}

    def walk(node):
        for child in node.get("children") or []:
            parents[child["id"]] = node["id"]
            walk(child)

    walk(ui_config["layout"])
    heading = next(
        m for m in _components(ui_config, "markdown")
        if str(m["props"].get("value") or "") == f"### {UPSCALE_TITLE}"
    )
    group = next(c for c in ui_config["components"] if c["id"] == parents[heading["id"]])
    assert group["props"].get("visible") is False


def test_the_start_button_starts_hidden_and_is_a_primary_action(ui_config):
    """開始ボタンは目立つ色（primary）。危険操作ではないので赤にしない。"""
    button = _button(ui_config, UPSCALE_BUTTON_LABEL)
    assert button["props"].get("variant") == "primary"
    assert button["props"].get("visible") is False
    assert "h3-tap" in (button["props"].get("elem_classes") or [])


def test_the_cancel_button_is_a_stop_variant(ui_config):
    """中止は赤系（stop）。押し間違えないよう開始ボタンと色を変える。"""
    button = _button(ui_config, UPSCALE_CANCEL_LABEL)
    assert button["props"].get("variant") == "stop"
    assert "h3-tap" in (button["props"].get("elem_classes") or [])


def test_the_explanation_says_the_source_is_kept(ui_config):
    """「元の動画はそのまま残る」ことを画面で明言する（不安を残さない）。"""
    assert "元の動画はそのまま残ります" in UPSCALE_NOTE
    assert UPSCALE_NOTE in _markdown_values(ui_config)


def test_the_explanation_mentions_the_output_folder_and_size(ui_config):
    """保存先と仕上がりサイズを書く（どこに何ができるか分かるように）。"""
    assert "data/upscaled/" in UPSCALE_NOTE
    assert "1920×1080" in UPSCALE_NOTE


def test_the_history_tab_has_a_1080p_filter(ui_config):
    """④履歴に「1080p成果物」フィルタがある。"""
    radios = [
        r for r in _components(ui_config, "radio")
        if "状態フィルタ" in str(r["props"].get("label") or "")
    ]
    choices = [
        c[0] if isinstance(c, (list, tuple)) else c
        for c in radios[0]["props"]["choices"]
    ]
    assert UPSCALED_PRODUCTS_FILTER in choices


# ============================================================ 表示の中身


def test_the_1080p_filter_lists_the_artifact(upscale_ui):
    """1080p成果物フィルタに、置いたファイルが出る（台帳は無い）。"""
    table = _first(
        upscale_ui["client"].predict(
            UPSCALED_PRODUCTS_FILTER, api_name="/on_history_filter"
        )
    )
    assert "1080p成果物" in table
    assert "v_20260810_150000_aaaa" in table
    assert "1920×1080" in table


def test_the_1080p_filter_shows_the_source_kind(upscale_ui):
    """何から作ったか（個別／連結）が分かる列がある。"""
    table = _first(
        upscale_ui["client"].predict(
            UPSCALED_PRODUCTS_FILTER, api_name="/on_history_filter"
        )
    )
    assert "個別動画" in table


def test_the_completed_summary_counts_the_1080p_artifact(upscale_ui):
    """③の件数に 1080p が数えられる（選択候補と食い違わない）。"""
    summary = str(upscale_ui["client"].predict(api_name="/on_videos_tick")[0])
    assert "1080p1件" in summary


def test_the_status_reports_progress_in_frames(upscale_ui):
    """進捗が `52 / 124フレーム（42%）` の形で出る。"""
    upscale_ui["fake"].status_value = UpscaleStatus(
        state=STATE_RUNNING,
        key="upscale-1",
        source_key="clip:v_20260810_150000_aaaa",
        source_label="🎬 v_20260810_150000_aaaa",
        frame=52,
        total=124,
    )
    assert "52 / 124フレーム（42%）" in _upscale_tick_text(upscale_ui)


def test_a_failure_is_shown_in_japanese(upscale_ui):
    """失敗は日本語で伝える（内部の例外文をそのまま出さない）。"""
    upscale_ui["fake"].status_value = UpscaleStatus(
        state=STATE_FAILED,
        message="高品質化に失敗しました（空き容量が不足しています）",
        error="高品質化に失敗しました（空き容量が不足しています）",
    )
    text = _upscale_tick_text(upscale_ui)
    assert "失敗" in text
    assert "Traceback" not in text


def test_the_finished_output_name_is_shown(upscale_ui):
    """完成したらファイル名を出す（Finder で探せるように）。"""
    upscale_ui["fake"].status_value = UpscaleStatus(
        state=STATE_SUCCEEDED,
        output_path=upscale_ui["artifact"],
        message="1080p高品質版を作成しました",
    )
    assert upscale_ui["artifact"].name in _upscale_tick_text(upscale_ui)


# ============================================================ 操作


def test_starting_without_a_selection_is_refused(upscale_ui):
    """何も選ばずに押しても、日本語で促すだけ（開始しない）。"""
    before = len(upscale_ui["fake"].started)
    message = _first(upscale_ui["client"].predict("", api_name="/on_start_upscale"))
    assert "選んで" in message
    assert len(upscale_ui["fake"].started) == before


def test_starting_sends_only_the_selection_key(upscale_ui):
    """UI が送るのは `種別:ID` だけ（パスを渡さない。§26.3）。"""
    cfg, service = upscale_ui["cfg"], upscale_ui["service"]
    job_id = "v_20260810_150001_bbbb"
    _add_success_clip(cfg, service, job_id)

    before = len(upscale_ui["fake"].started)
    message = _first(
        upscale_ui["client"].predict(f"clip:{job_id}", api_name="/on_start_upscale")
    )
    assert "開始" in message
    assert len(upscale_ui["fake"].started) == before + 1

    request = upscale_ui["fake"].started[-1]
    assert request.source_key == f"clip:{job_id}"
    assert request.source_path == cfg.outputs_dir / f"{job_id}.mp4"
    assert request.output_path.parent == cfg.upscaled_dir


def test_a_path_shaped_key_cannot_reach_a_file(upscale_ui):
    """パスのような値を送っても、その場所は読まれない（ID として解決される）。"""
    before = len(upscale_ui["fake"].started)
    message = _first(
        upscale_ui["client"].predict(
            "clip:../../../etc/passwd", api_name="/on_start_upscale"
        )
    )
    assert "見つかりません" in message or "形式" in message
    assert len(upscale_ui["fake"].started) == before


def test_an_existing_artifact_is_not_regenerated(upscale_ui):
    """すでにある1080p版は作り直さない（押しても知らせるだけ）。"""
    before = len(upscale_ui["fake"].started)
    message = _first(
        upscale_ui["client"].predict(
            "clip:v_20260810_150000_aaaa", api_name="/on_start_upscale"
        )
    )
    assert "すでにあります" in message
    assert len(upscale_ui["fake"].started) == before


def test_cancel_reports_in_japanese(upscale_ui):
    """中止ボタンの返答は日本語。"""
    message = _first(upscale_ui["client"].predict(api_name="/on_cancel_upscale"))
    assert "中止" in message


# ============================================================ 1080p成果物の扱い


def test_a_1080p_artifact_cannot_be_continued(upscale_ui):
    """1080p成果物から「続きを作る」はできない（理由を日本語で出す）。"""
    result = upscale_ui["client"].predict(
        "upscaled:u_clip_v_20260810_150000_aaaa_1080p",
        api_name="/on_start_continuation",
    )
    message = str(result[-1])
    assert "1080p" in message and "続き" in message


def test_a_1080p_artifact_cannot_be_concatenated(upscale_ui):
    """1080p成果物は連結の対象にしない。"""
    result = upscale_ui["client"].predict(
        "upscaled:u_clip_v_20260810_150000_aaaa_1080p",
        api_name="/on_start_concat",
    )
    assert "連結できません" in _first(result)


def test_the_already_note_explains_why_there_is_no_button():
    """1080p成果物を選んだときは、ボタンが無い理由を書く。"""
    assert "これ以上の高品質化はできません" in UPSCALE_ALREADY_NOTE


# ============================================================ 回帰防止


def test_videos_tick_still_returns_four_values(upscale_ui):
    """③の Timer の戻り値の数を変えない（既存 api_name の契約）。"""
    result = upscale_ui["client"].predict(api_name="/on_videos_tick")
    assert len(result) == 4


def test_the_upscale_tick_does_not_touch_the_player_or_selector(upscale_ui):
    """進捗の更新でプレビューや選択が壊れない（出力は状態表示だけ）。

    Group の更新は HTTP へ届かないので、クライアントから見える戻り値は1つ。
    """
    result = upscale_ui["client"].predict(api_name="/on_upscale_tick")
    if isinstance(result, (list, tuple)):
        assert len(result) == 1, "状態表示以外のものが返っている"
    assert "1080pの状態" in _first(result)


def _add_success_clip(cfg, service, job_id: str) -> Path:
    """SUCCESS の個別動画を1本作る（モック素材を使う）。"""
    import shutil
    from datetime import datetime

    from app.core.contracts import BackendIdentity, JobSpec
    from app.core.history import HistoryRecord

    now = datetime(2026, 8, 10, 15, 0, 0).astimezone()
    out = cfg.outputs_dir / f"{job_id}.mp4"
    last = cfg.outputs_dir / f"{job_id}_last.png"
    spec = JobSpec(
        job_id=job_id,
        prompt=f"テスト {job_id}",
        num_frames=56,
        steps=4,
        seed_requested=None,
        output_path=out,
        last_frame_path=last,
    )
    service.history.add(
        HistoryRecord.from_job_spec(
            spec,
            identity=BackendIdentity(
                backend_id="minimax_h3",
                display_name="MiniMax-H3-NF4",
                model_id="DiffSynth-Studio/MiniMax-H3-NF4",
                model_revision="nf4-turbo4step-ckpt500",
            ),
            execution_engine="mock",
            app_version=cfg.version,
            data_root=cfg.data_root,
            created_at=now,
        )
    )
    service.history.mark_running(job_id, now)
    shutil.copy(MOCK_DIR / "mock_56.mp4", out)
    shutil.copy(MOCK_DIR / "mock_56_last.png", last)
    service.history.mark_success(
        job_id, output_path=out, last_frame_path=last,
        seed_used=42, elapsed_sec=1.0, finished_at=now,
    )
    return out


# ============================================================ 配信境界


def test_the_1080p_preview_is_playable(upscale_ui):
    """1080p成果物のプレビューが**実際に再生できる**（P6 で一度壊した箇所）。

    画面に出るのに `_servable()` が弾く、という食い違いを繰り返さないための試験。
    """
    result = upscale_ui["client"].predict(
        "upscaled:u_clip_v_20260810_150000_aaaa_1080p",
        api_name="/on_select_video",
    )
    detail = str(result[1])
    assert "再生できません" not in detail, detail
    assert result[0] is not None, "プレビューに動画が渡っていない"


def test_the_ui_and_the_server_serve_the_same_directories():
    """`_servable()` の許可先と `main.py` の `allowed_paths` を一致させる。

    片方だけ増やすと「一覧には出るのに再生できない」動画ができる。
    """
    ui_source = (PROJECT_ROOT / "app" / "ui" / "minimal.py").read_text(encoding="utf-8")
    main_source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")

    for name in ("outputs_dir", "concat_dir", "upscaled_dir"):
        assert f"cfg.{name}" in ui_source, f"UI が {name} を配信対象にしていません"
        assert f"cfg.{name}" in main_source, f"main.py が {name} を配信対象にしていません"

    # ゴミ箱はどちらからも配信しない
    assert "trash_dir" not in main_source


def test_the_trash_is_never_servable(upscale_ui, tmp_path):
    """`data/trash` の動画はプレビューに渡らない（整理した動画を配らない）。"""
    from app.ui.minimal import build_ui  # noqa: F401  （UI 構築済みの確認用）

    cfg = upscale_ui["cfg"]
    cfg.trash_dir.mkdir(parents=True, exist_ok=True)
    hidden = cfg.trash_dir / "u_clip_v_20260810_150000_aaaa_1080p.mp4"
    hidden.write_bytes(b"\x00" * 1024)

    # 一覧から引けない＝プレビューにも出せない
    assert upscale_ui["service"].find_row(
        "u_clip_v_20260810_150000_aaaa_1080p", "upscaled"
    ).video_path.parent == cfg.upscaled_dir
