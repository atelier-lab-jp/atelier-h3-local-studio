"""UI 経路の結合テスト（設計書 §17.2・指示 §6.7）。

実際に Gradio を localhost で起動し、gradio_client（HTTP）から
コールバックを叩いて確認する:
- ①新規生成タブ: 「生成をキューに追加」が**生成完了を待たずに**すぐ戻ること /
  日本語の成功・失敗メッセージ / Timer が読む状態表示（待機件数・進捗・完成）
- ②キュータブ（P3）: 現在の処理・待機一覧・エンジン状態・取消・再起動・
  赤色バナー・「最終処理中」推定・Timer の例外復帰
- ③完成動画 / ④履歴タブ・継続生成（P4）: 一覧（個別＋連結）・プレビュー・欠損の安全表示・
  状態フィルタ・エラー分類・異常データ（QUEUED/RUNNING 残存）・継続バナーとプリフィル・
  連結／Finder のメッセージ・Timer の例外復帰・プレビューが毎 tick でリセットされないこと

Execution Engine は必ず MockEngine（実モデルは絶対に起動しない）。
実データ領域は使わず tmp_path を data_root にする。ポートは空きポートを毎回取る。
Finder 表示（`open -R`）と連結（ffmpeg）は AppService 側をモックして実行しない。
"""

from __future__ import annotations

import dataclasses
import shutil
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.app_service import AppService
from app.core.config import load_config
from app.core.contracts import (
    EngineState,
    JobStage,
    JobStatus,
    JobView,
    QueueSnapshot,
    RestartState,
    ValidationError,
)
from app.core.history import HistoryRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def ui_app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui_data")
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine
    from app.ui.minimal import build_ui

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.start()

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        allowed_paths=[str(cfg.data_root), str(cfg.assets_mock_dir)],
        prevent_thread_lock=True,
    )
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield client, service
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


@pytest.fixture(scope="module")
def queue_ui_app(tmp_path_factory):
    """②キュータブ用の UI。**ディスパッチャを起動しない**ので投入ジョブは QUEUED のまま残る。

    こうすることで「待機一覧」「取消」を時間に依存せず決定的に検証できる
    （MockEngine は生成が一瞬で終わるため、稼働中のキューでは QUEUED を掴めない）。
    実モデルは使わない（MockEngine 固定）。
    """
    tmp = tmp_path_factory.mktemp("queue_ui_data")
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine
    from app.ui.minimal import build_ui

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()  # 履歴だけ読む（queue.start() は呼ばない＝生成は始まらない）

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        allowed_paths=[str(cfg.data_root), str(cfg.assets_mock_dir)],
        prevent_thread_lock=True,
    )
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield client, service
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


@pytest.fixture(scope="module")
def restart_ui_app(tmp_path_factory):
    """`service.restart_worker()` が配線済みの場合の UI（メインの統合後を先取り検証）。

    AppService にまだ restart_worker が無い間は、同名のスタブを**インスタンスに**
    生やして UI 側の配線だけを検証する（配線後は本物がそのまま使われる）。
    """
    tmp = tmp_path_factory.mktemp("restart_ui_data")
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine
    from app.ui.minimal import build_ui

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()

    calls: list[str] = []
    if not hasattr(service, "restart_worker"):
        service.restart_worker = lambda: (calls.append("stub"), True)[1]

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        allowed_paths=[str(cfg.data_root), str(cfg.assets_mock_dir)],
        prevent_thread_lock=True,
    )
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield client, service, calls
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


def _submit(client, prompt, length="約2.33秒（56フレーム）", steps="4ステップ（高速）"):
    return client.predict(prompt, length, steps, True, 42, api_name="/on_submit")


def _fake_snapshot(**kwargs) -> QueueSnapshot:
    base = dict(
        engine_state=EngineState.READY,
        current=None,
        queued=(),
        queue_size=0,
        last_finished=None,
        running=True,
    )
    base.update(kwargs)
    return QueueSnapshot(**base)


def _running_job(**kwargs) -> JobView:
    now = datetime.now()
    base = dict(
        job_id="v_20260807_120000_test",
        status=JobStatus.RUNNING,
        prompt_head="A cute small green dinosaur wizard",
        num_frames=56,
        steps=4,
        duration_label="2.33秒",
        seed_requested=42,
        seed_used=42,
        stage=JobStage.GENERATING,
        step=4,
        total_steps=4,
        queued_at=now - timedelta(seconds=400),
        started_at=now - timedelta(seconds=390),
        last_event_at=now,
    )
    base.update(kwargs)
    return JobView(**base)


def test_submit_returns_immediately_with_japanese_message(ui_app):
    """投入コールバックは生成完了を待たずに戻る（設計書 §5 / 指示 §6.7）。"""
    client, service = ui_app
    started = time.monotonic()
    message, header, progress = client.predict(
        "UI経由のテスト <d>[Japanese] こんにちは</d>",
        "約2.33秒（56フレーム）",
        "4ステップ（高速）",
        True,  # シードはランダム
        42,
        api_name="/on_submit",
    )
    elapsed = time.monotonic() - started

    assert "キューに追加しました" in message
    assert "目安時間" in message
    # 生成完了（モックでも複数イベント）を待っていたらこの時間では戻らない
    assert elapsed < 3.0, f"投入コールバックが戻るまで {elapsed:.2f}s かかりました"
    assert "状態" in header
    assert "待機" in progress or "現在の処理" in progress


def test_tick_reports_progress_and_completion(ui_app):
    """Timer が読む表示が、待機 → 生成中 → 完成 と更新される。"""
    client, service = ui_app
    client.predict(
        "進捗表示の確認", "約2.33秒（56フレーム）", "4ステップ（高速）", True, 42,
        api_name="/on_submit",
    )

    # gr.State 出力は API 経由では返らないため、表示テキストで完成を判定する
    deadline = time.monotonic() + 30
    saw_completion = False
    last_progress = ""
    last_header = ""
    while time.monotonic() < deadline:
        last_header, last_progress, _logs = client.predict(api_name="/on_tick")
        if "完成 ✅" in last_progress:
            saw_completion = True
            break
        time.sleep(0.1)

    assert saw_completion, f"完成が UI に反映されませんでした: {last_progress}"
    assert "空き容量" in last_header  # ヘッダに状態と空き容量が出ている
    assert "待機中" in last_progress
    assert "処理時間" in last_progress
    # UI が拾える完成動画が実在する
    latest = service.latest_completed()
    assert latest is not None and latest.video_path.is_file()


def test_empty_prompt_shows_japanese_error(ui_app):
    client, _ = ui_app
    message, _, _ = client.predict(
        "   ", "約2.33秒（56フレーム）", "4ステップ（高速）", True, 42,
        api_name="/on_submit",
    )
    assert message.startswith("❌")
    assert "プロンプトを入力してください" in message


def test_seed_required_when_not_random(ui_app):
    client, _ = ui_app
    message, _, _ = client.predict(
        "シード未入力の確認", "約2.33秒（56フレーム）", "4ステップ（高速）",
        False,  # ランダムにしない
        None,   # シード未入力
        api_name="/on_submit",
    )
    assert message.startswith("❌")
    assert "シード値" in message


def test_estimate_text_changes_with_settings(ui_app):
    """長さ・ステップを変えると目安時間表示が変わる（解像度は固定表示）。"""
    client, _ = ui_app
    short4 = client.predict(
        "約2.33秒（56フレーム）", "4ステップ（高速）", api_name="/on_estimate_change"
    )
    long8 = client.predict(
        "約5.17秒（124フレーム）", "8ステップ（高品質・時間は約1.7倍）",
        api_name="/on_estimate_change",
    )
    assert "576×320" in short4 and "24fps 固定" in short4
    assert short4 != long8
    assert "約6〜7分" in short4  # P3実測 403 秒
    assert "実機で計測した値です" in short4
    assert "約22〜23分" in long8  # P3実測 819 秒 × 1.67
    # 124フレーム×8ステップだけは実測がない。推定であることを明示する（P5 §6.4）
    assert "推定値" in long8


def test_dialogue_hint_button_inserts_notation(ui_app):
    client, _ = ui_app
    result = client.predict("A wizard speaks.", api_name="/on_insert_hint")
    assert "<d>[Japanese]" in result
    assert result.startswith("A wizard speaks.")


# ------------------------------------------------------------------ ②キュータブ（P3）


def test_queue_tab_reports_engine_current_and_waiting(queue_ui_app):
    """②タブの Timer コールバックがエンジン状態・現在の処理・待機一覧を返す。"""
    client, service = queue_ui_app
    before = len(service.queue.queued_jobs())

    _submit(client, "キュータブの確認A <d>[Japanese] こんにちは</d>")
    _submit(
        client,
        "キュータブの確認B",
        length="約5.17秒（124フレーム）",
        steps="8ステップ（高品質・時間は約1.7倍）",
    )

    banner, engine, current, waiting, error = client.predict(
        api_name="/on_queue_tick"
    )[:5]

    ids = [j.job_id for j in service.queue.queued_jobs()]
    assert len(ids) == before + 2
    assert banner == ""  # 正常時は赤色バナーを出さない
    assert "エンジンの状態" in engine
    # 実行方式は初心者向けの言い方にし、内部値（mock）は「詳しい情報」だけに出す
    assert "お試しモード" in engine
    assert "実行方式: mock" not in engine
    detail = client.predict(api_name="/on_queue_detail_tick")
    assert "実行方式（execution_engine）: mock" in detail
    assert "現在の処理" in current and "進行中の生成はありません" in current
    assert f"待機中のジョブ（{before + 2}件）" in waiting
    assert ids[-1] in waiting and ids[-2] in waiting
    assert "2.33秒" in waiting and "5.17秒" in waiting
    assert "直前の失敗" in error


def test_cancel_queued_job_from_ui(queue_ui_app):
    """待機中ジョブの取消が UI 経由で成功し、日本語メッセージが返る。"""
    client, service = queue_ui_app
    _submit(client, "取消の確認")
    target = service.queue.queued_jobs()[-1].job_id

    out = client.predict(target, api_name="/on_cancel_queued")
    message, waiting = out[0], out[4]

    assert "取り消しました" in message and target in message
    assert target not in waiting
    assert all(j.job_id != target for j in service.queue.queued_jobs())


def test_cancel_shows_japanese_message_when_not_cancelable(queue_ui_app):
    """取消できない場合（実行中・終了済み・存在しないID）は日本語で理由を返す。

    JobQueue.cancel_queued は待機列だけを探すため、実行中ジョブも
    「存在しないID」と同じ False 経路になる（メッセージも同一）。
    """
    client, service = queue_ui_app
    _submit(client, "二重取消の確認")
    target = service.queue.queued_jobs()[-1].job_id
    assert "取り消しました" in client.predict(target, api_name="/on_cancel_queued")[0]

    again = client.predict(target, api_name="/on_cancel_queued")[0]
    assert "取り消せませんでした" in again
    assert "実行中" in again  # 実行中ジョブの止め方も案内している

    unknown = client.predict("v_no_such_job_id", api_name="/on_cancel_queued")[0]
    assert "取り消せませんでした" in unknown

    empty = client.predict("", api_name="/on_cancel_queued")[0]
    assert "選んでください" in empty


def test_halted_and_intake_blocked_show_red_banner(queue_ui_app, monkeypatch):
    """HALTED と空き容量による受付停止が赤色バナーに出る（設計書 §13.2・§13.3）。"""
    client, service = queue_ui_app
    snap = _fake_snapshot(
        engine_state=EngineState.HALTED,
        running=True,
        restart_state=RestartState.HALTED,
        consecutive_failures=3,
        halted_reason="連続3回失敗したため生成を停止しました",
        intake_blocked_reason="空き容量が不足しています（残り 3.0GB / 必要 5GB 以上）",
    )
    monkeypatch.setattr(service, "snapshot", lambda: snap)

    banner, engine, _current, _waiting, _error = client.predict(
        api_name="/on_queue_tick"
    )[:5]

    assert "連続3回失敗したため生成を停止しました" in banner
    assert "空き容量が不足しています" in banner
    assert "ワーカーを再起動" in banner
    assert "d32f2f" in banner  # 赤枠のスタイルが付いている
    assert "停止中" in engine
    # 連続失敗回数のような内部の数値は「詳しい情報」に置く（P5 §6.4）
    assert "連続失敗 3回" in client.predict(api_name="/on_queue_detail_tick")


def test_backoff_and_restarting_engine_labels(queue_ui_app, monkeypatch):
    """再起動待ち（バックオフ残り秒）と再初期化中を日本語で表示する。"""
    client, service = queue_ui_app

    monkeypatch.setattr(
        service,
        "snapshot",
        lambda: _fake_snapshot(
            engine_state=EngineState.DEAD,
            restart_state=RestartState.BACKOFF,
            backoff_remaining_sec=4.2,
            consecutive_failures=1,
        ),
    )
    banner, engine = client.predict(api_name="/on_queue_tick")[:2]
    assert "再起動待ち" in engine and "あと約5秒" in engine
    # 自動復旧中は engine が一時的に DEAD でも停止バナーを出さない（誤警報を避ける）
    assert banner == ""

    monkeypatch.setattr(
        service,
        "snapshot",
        lambda: _fake_snapshot(
            engine_state=EngineState.STARTING,
            restart_state=RestartState.RESTARTING,
            restart_total=2,
        ),
    )
    engine = client.predict(api_name="/on_queue_tick")[1]
    assert "再初期化中" in engine
    assert "自動再起動 2回" in client.predict(api_name="/on_queue_detail_tick")


def test_finalizing_stage_is_shown_after_last_step(queue_ui_app, monkeypatch):
    """step==total かつ GENERATING かつ最後のイベントが古い → 「最終処理中」。"""
    client, service = queue_ui_app
    now = datetime.now()

    # 直近にイベントが来ている間は通常の「生成中 ステップ 4/4」
    monkeypatch.setattr(
        service,
        "snapshot",
        lambda: _fake_snapshot(
            engine_state=EngineState.BUSY,
            current=_running_job(last_event_at=now),
        ),
    )
    current = client.predict(api_name="/on_queue_tick")[2]
    assert "生成中 ステップ 4/4" in current
    assert "最終処理中" not in current

    # 無音が続いたら「最終処理中（映像・音声の変換）」。経過時間は出し続ける
    monkeypatch.setattr(
        service,
        "snapshot",
        lambda: _fake_snapshot(
            engine_state=EngineState.BUSY,
            current=_running_job(last_event_at=now - timedelta(seconds=60)),
        ),
    )
    current = client.predict(api_name="/on_queue_tick")[2]
    assert "最終処理中" in current
    assert "映像・音声の変換" in current
    assert "2〜3分" in current  # ハングではないと明示している
    assert "経過 06:3" in current  # 経過時間を出し続けている
    assert "目安" in current


def test_stalled_job_shows_warning_but_no_stop(queue_ui_app, monkeypatch):
    """stalled=True のときだけ「通常より時間がかかっています」を追加表示する。"""
    client, service = queue_ui_app
    monkeypatch.setattr(
        service,
        "snapshot",
        lambda: _fake_snapshot(
            engine_state=EngineState.BUSY,
            current=_running_job(step=2, stalled=True, last_event_at=datetime.now()),
        ),
    )
    current = client.predict(api_name="/on_queue_tick")[2]
    assert "通常より時間がかかっています" in current
    assert "自動では止めません" in current


def test_last_failure_shows_japanese_category(queue_ui_app, monkeypatch):
    """直前の失敗のエラー分類を日本語で表示する（設計書 §13.3）。"""
    client, service = queue_ui_app
    failed = _running_job(
        job_id="v_20260807_121500_bad",
        status=JobStatus.FAILED,
        stage=None,
        step=None,
        total_steps=None,
        error="ワーカープロセスが異常終了しました",
        error_category="worker_dead",
    )
    monkeypatch.setattr(
        service, "snapshot", lambda: _fake_snapshot(last_finished=failed)
    )
    error = client.predict(api_name="/on_queue_tick")[4]
    # エラーの種類とジョブIDは主要部に残す（調査できなくなるため。P5 §6.4）
    assert "生成エンジンの異常終了" in error
    assert "v_20260807_121500_bad" in error
    # エラー本文（内部のメッセージ）は「詳しい情報」へ分ける
    assert "ワーカープロセスが異常終了しました" not in error
    detail = client.predict(api_name="/on_queue_detail_tick")
    assert "ワーカープロセスが異常終了しました" in detail
    assert "v_20260807_121500_bad" in detail
    assert "worker_dead" in detail


def test_queue_tick_recovers_after_exception(queue_ui_app, monkeypatch):
    """Timer コールバックが例外を投げても UI 更新は永久停止しない（P1 の回帰防止）。"""
    client, service = queue_ui_app

    def boom():
        raise RuntimeError("スナップショット取得の擬似障害")

    monkeypatch.setattr(service, "snapshot", boom)
    broken = client.predict(api_name="/on_queue_tick")
    assert "状態を取得できません" in broken[2]
    assert broken[0] == ""  # 赤色バナーは誤って出さない

    monkeypatch.undo()
    recovered = client.predict(api_name="/on_queue_tick")
    assert "現在の処理" in recovered[2]
    assert "状態を取得できません" not in recovered[2]
    assert "エンジンの状態" in recovered[1]


def test_restart_worker_button(queue_ui_app, monkeypatch):
    """[ワーカーを再起動]。service.restart_worker() 未配線なら無効化＋説明を返す。"""
    client, service = queue_ui_app
    available = hasattr(service, "restart_worker")

    first = client.predict(False, api_name="/on_restart_worker")[0]
    if not available:
        assert "対応していません" in first
        return

    assert "チェック" in first  # 確認チェック無しでは再起動しない

    calls: list[int] = []
    monkeypatch.setattr(
        service, "restart_worker", lambda: (calls.append(1), True)[1]
    )
    done = client.predict(True, api_name="/on_restart_worker")[0]
    assert calls == [1]
    assert "再起動を開始しました" in done
    assert "失敗として記録されます" in done  # 実行中ジョブが失敗になることを明示


def test_restart_worker_calls_service_when_wired(restart_ui_app, monkeypatch):
    """restart_worker が配線されていれば、確認チェック後に呼ばれ日本語で結果を返す。"""
    client, service, calls = restart_ui_app

    not_acked = client.predict(False, api_name="/on_restart_worker")[0]
    assert "チェック" in not_acked
    assert not calls

    seen: list[int] = []
    monkeypatch.setattr(service, "restart_worker", lambda: (seen.append(1), True)[1])
    done = client.predict(True, api_name="/on_restart_worker")[0]
    assert seen == [1]
    assert "再起動を開始しました" in done
    assert "失敗として記録されます" in done

    # False を返した場合（停止処理中など）も日本語で理由を伝え、UI は落ちない
    monkeypatch.setattr(service, "restart_worker", lambda: False)
    refused = client.predict(True, api_name="/on_restart_worker")[0]
    assert "再起動できませんでした" in refused

    # 例外を投げても UI は落とさず日本語メッセージにする（設計書 §13.2）
    def boom():
        raise RuntimeError("擬似障害")

    monkeypatch.setattr(service, "restart_worker", boom)
    failed = client.predict(True, api_name="/on_restart_worker")[0]
    assert "再起動できませんでした" in failed
    # 例外文（内部の文言）はそのまま画面へ出さない。原因はログへ記録する（P5 §6.4）
    assert "擬似障害" not in failed
    assert "詳しい情報" in failed


# ============================================================ ③④・継続生成（P4）
#
# AppService の P4 API（completed_videos / history_rows / continuation_context /
# start_concat / concat_status / reveal_in_finder）をそのまま使って UI を検証する。
# 実モデル・実 ffmpeg・実 Finder はいずれも起動しない:
#   - Finder 表示は `service.reveal_in_finder` をモックし、subprocess も封じる
#   - 連結は「解決に失敗して即終了するケース」だけ本物を通し、成功経路は
#     `service.start_concat` をモックして ffmpeg を動かさない
# 履歴は固定データを直接 HistoryStore へ入れる（生成は行わない）。

MOCK_ASSET_MP4 = PROJECT_ROOT / "app" / "assets" / "mock" / "mock_56.mp4"
MOCK_ASSET_PNG = PROJECT_ROOT / "app" / "assets" / "mock" / "mock_56_last.png"


def _p4_record(cfg, **kwargs) -> HistoryRecord:
    """テスト用の履歴レコード（既定は 56フレーム・4ステップの成功記録）。"""
    base = dict(
        id="v_p4",
        type="single",
        status=JobStatus.SUCCESS,
        created_at=datetime(2026, 8, 7, 10, 0, 0).astimezone(),
        started_at=datetime(2026, 8, 7, 10, 0, 5).astimezone(),
        finished_at=datetime(2026, 8, 7, 10, 6, 45).astimezone(),
        prompt="A cute small green dinosaur wizard stands inside a magical atelier.",
        duration_label="2.33秒",
        num_frames=56,
        fps=24,
        width=576,
        height=320,
        steps=4,
        seed_requested=42,
        seed_used=42,
        parent_id=None,
        keyframe_path=None,
        output_path=None,
        last_frame_path=None,
        concat_path=None,
        concat_sources=None,
        elapsed_sec=401.0,
        error=None,
        error_category=None,
        execution_engine="mock",
        backend_id="minimax_h3",
        model_id="MiniMax-H3-NF4",
        model_revision="nf4-2026-07",
        backend_params=None,
        app_version=cfg.version,
    )
    base.update(kwargs)
    return HistoryRecord(**base)


def _seed_p4_history(cfg, history) -> None:
    """③④が扱うべき全パターンを履歴へ用意する（実ファイルもコピーする）。"""

    def _artifacts(job_id: str, *, mp4: bool = True, png: bool = True) -> None:
        if mp4:
            shutil.copyfile(MOCK_ASSET_MP4, cfg.outputs_dir / f"{job_id}.mp4")
        if png:
            shutil.copyfile(MOCK_ASSET_PNG, cfg.outputs_dir / f"{job_id}_last.png")

    # ルート（成功・成果物あり）
    _artifacts("v_p4_root")
    history.add(
        _p4_record(
            cfg,
            id="v_p4_root",
            output_path="outputs/v_p4_root.mp4",
            last_frame_path="outputs/v_p4_root_last.png",
        )
    )
    # 継続生成の子（成功）＋ この子までを連結した成果物
    _artifacts("v_p4_child")
    shutil.copyfile(MOCK_ASSET_MP4, cfg.concat_dir / "c_v_p4_child_2clips.mp4")
    history.add(
        _p4_record(
            cfg,
            id="v_p4_child",
            type="continuation",
            parent_id="v_p4_root",
            created_at=datetime(2026, 8, 7, 10, 10, 0).astimezone(),
            finished_at=datetime(2026, 8, 7, 10, 16, 40).astimezone(),
            keyframe_path="outputs/v_p4_root_last.png",
            output_path="outputs/v_p4_child.mp4",
            last_frame_path="outputs/v_p4_child_last.png",
            concat_path="concat/c_v_p4_child_2clips.mp4",
            concat_sources=["v_p4_root", "v_p4_child"],
            elapsed_sec=412.5,
        )
    )
    # 成功だが成果物が消えている（欠損の安全表示）
    history.add(
        _p4_record(
            cfg,
            id="v_p4_missing",
            created_at=datetime(2026, 8, 7, 10, 5, 0).astimezone(),
            output_path="outputs/v_p4_missing.mp4",
            last_frame_path="outputs/v_p4_missing_last.png",
        )
    )
    # 成功だが最終フレームPNGが無い（継続元にできない）
    _artifacts("v_p4_nopng", png=False)
    history.add(
        _p4_record(
            cfg,
            id="v_p4_nopng",
            created_at=datetime(2026, 8, 7, 10, 7, 0).astimezone(),
            output_path="outputs/v_p4_nopng.mp4",
            last_frame_path="outputs/v_p4_nopng_last.png",
        )
    )
    # 失敗（エラー分類つき）
    history.add(
        _p4_record(
            cfg,
            id="v_p4_failed",
            status=JobStatus.FAILED,
            created_at=datetime(2026, 8, 7, 9, 50, 0).astimezone(),
            steps=8,
            seed_requested=991,
            seed_used=None,
            elapsed_sec=63.0,
            error="MPS backend out of memory",
            error_category="oom",
        )
    )
    # 取消・中断
    history.add(
        _p4_record(
            cfg,
            id="v_p4_canceled",
            status=JobStatus.CANCELED,
            created_at=datetime(2026, 8, 7, 9, 40, 0).astimezone(),
            seed_used=None,
            elapsed_sec=None,
        )
    )
    history.add(
        _p4_record(
            cfg,
            id="v_p4_interrupted",
            status=JobStatus.INTERRUPTED,
            created_at=datetime(2026, 8, 7, 9, 30, 0).astimezone(),
            seed_used=None,
            elapsed_sec=None,
            error="アプリ終了により中断",
        )
    )
    # 異常データ（前回終了が正常に完了しなかった場合に残りうる）
    history.add(
        _p4_record(
            cfg,
            id="v_p4_queued",
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 8, 7, 9, 20, 0).astimezone(),
            started_at=None,
            finished_at=None,
            seed_used=None,
            elapsed_sec=None,
        )
    )
    history.add(
        _p4_record(
            cfg,
            id="v_p4_running",
            status=JobStatus.RUNNING,
            created_at=datetime(2026, 8, 7, 9, 25, 0).astimezone(),
            finished_at=None,
            seed_used=None,
            elapsed_sec=None,
        )
    )


@pytest.fixture(scope="module")
def p4_app(tmp_path_factory):
    """③④・継続生成の検証用 UI。ディスパッチャは起動しない（履歴は固定データ）。"""
    tmp = tmp_path_factory.mktemp("p4_ui_data")
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine
    from app.ui.minimal import build_ui

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()  # queue.start() は呼ばない（生成は始まらない）
    _seed_p4_history(cfg, service.history)

    demo = build_ui(cfg, "mock", service)
    port = _free_port()
    demo.launch(
        server_name="127.0.0.1",
        server_port=port,
        share=False,
        inbrowser=False,
        # 本番（app/main.py）と同じく成果物ディレクトリだけを配信対象にする。
        # 履歴JSON・ログは data_root 直下なので配信されない。
        allowed_paths=[str(cfg.outputs_dir), str(cfg.concat_dir), str(cfg.tmp_dir)],
        prevent_thread_lock=True,
    )
    from gradio_client import Client

    client = Client(f"http://127.0.0.1:{port}", verbose=False)
    try:
        yield client, service, cfg
    finally:
        demo.close()
        service.shutdown(timeout=5.0)


def _value_of(result):
    """gradio_client が返す値（`gr.update()` の dict の場合は value を取り出す）。"""
    if isinstance(result, dict):
        return result.get("value")
    return result


def _video_path(result) -> str | None:
    """gr.Video 出力からファイルパス文字列を取り出す（None / タプルにも耐える）。"""
    value = _value_of(result)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("video") or value.get("path")
        if isinstance(value, dict):
            value = value.get("path")
    return str(value) if value else None


# ------------------------------------------------------------ ③完成動画タブ


def test_completed_tab_lists_clips_and_concat(p4_app):
    """③の一覧に成功した個別動画と連結動画が新しい順で並ぶ（失敗記録は出ない）。"""
    client, _service, _cfg = p4_app
    listing, choices, concat_status = client.predict(api_name="/on_videos_tick")

    assert "完成した動画" in listing
    assert "v_p4_root" in listing and "v_p4_child" in listing
    assert "個別" in listing and "連結" in listing  # 種別が分かる
    assert "v_p4_failed" not in listing  # 失敗は③に出さない
    assert "v_p4_canceled" not in listing
    # 新しい順（child 10:10 が root 10:00 より前に出る）
    assert listing.index("v_p4_child") < listing.index("v_p4_root")
    # 選択候補には個別・連結の両方が入る
    values = [c[1] if isinstance(c, (list, tuple)) else c for c in choices["choices"]]
    assert "clip:v_p4_root" in values
    assert "concat:v_p4_child" in values
    assert "連結の状態" in concat_status


def test_completed_tab_preview_and_metadata(p4_app):
    """選択でプレビューが出て、設計書 §5.4 のメタ情報が揃う。"""
    client, _service, _cfg = p4_app
    video, meta, tech = client.predict("clip:v_p4_child", api_name="/on_select_video")

    path = _video_path(video)
    assert path is not None and path.endswith(".mp4")
    assert Path(path).is_file()

    assert "v_p4_child" in meta
    assert "2026-08-07 10:10" in meta       # 作成日時
    assert "2.33秒" in meta                  # 長さ
    assert "ステップ: 4" in meta              # ステップ
    assert "42" in meta                      # seed
    assert "v_p4_root" in meta               # 親ID
    assert "チェーン長: 2" in meta
    assert "処理時間: 06:52" in meta
    assert "dinosaur wizard" in meta         # プロンプト概要
    # backend / model は内部の言い方なので「詳しい情報」へ分ける（P5 §6.4）
    assert "minimax_h3" not in meta and "nf4-2026-07" not in meta
    assert "minimax_h3" in tech and "nf4-2026-07" in tech
    assert "v_p4_child" in tech              # ジョブIDは技術情報にも残す


def test_completed_tab_concat_row_shows_sources(p4_app):
    """連結動画を選ぶと連結元の並びが出て、連結ファイルがプレビューできる。"""
    client, _service, cfg = p4_app
    video, meta, _tech = client.predict("concat:v_p4_child", api_name="/on_select_video")

    assert _video_path(video) is not None
    assert "連結動画" in meta
    assert "v_p4_root" in meta and "v_p4_child" in meta
    assert "連結元（2本）" in meta
    assert (cfg.concat_dir / "c_v_p4_child_2clips.mp4").is_file()


def test_completed_tab_missing_artifact_is_safe(p4_app):
    """成果物が消えていてもプレビューは None にして、日本語で欠損を伝える。"""
    client, _service, _cfg = p4_app
    listing = client.predict(api_name="/on_videos_tick")[0]
    assert "ファイル欠損" in listing

    video, meta, _tech = client.predict("clip:v_p4_missing", api_name="/on_select_video")
    assert _video_path(video) is None
    assert "ファイルが見つかりません" in meta

    # 存在しないIDを選んでも落ちない
    video, meta, _tech = client.predict("clip:v_no_such_id", api_name="/on_select_video")
    assert _video_path(video) is None
    assert "見つかりません" in meta


def test_completed_tab_tick_recovers_after_exception(p4_app, monkeypatch):
    """③の Timer が例外を投げても更新は永久停止しない（次の tick で回復する）。"""
    client, service, _cfg = p4_app

    def boom(*a, **kw):
        raise RuntimeError("完成動画一覧の擬似障害")

    monkeypatch.setattr(service.history, "list_records", boom)
    if hasattr(service, "completed_videos"):
        monkeypatch.setattr(service, "completed_videos", boom)
    broken = client.predict(api_name="/on_videos_tick")
    assert "一覧を取得できません" in broken[0]

    monkeypatch.undo()
    recovered = client.predict(api_name="/on_videos_tick")
    assert "v_p4_root" in recovered[0]
    assert "一覧を取得できません" not in recovered[0]


def test_preview_is_not_reset_by_timer_ticks(p4_app):
    """Timer は一覧と連結状態だけを更新し、プレーヤーには触れない。"""
    client, _service, _cfg = p4_app
    before = _video_path(client.predict("clip:v_p4_root", api_name="/on_select_video")[0])
    assert before is not None

    # tick を何度回してもプレーヤーは出力に含まれない（3値＝一覧・選択肢・連結状態）
    for _ in range(3):
        tick = client.predict(api_name="/on_videos_tick")
        assert len(tick) == 3
        assert isinstance(tick[0], str) and isinstance(tick[2], str)
        assert not any(
            isinstance(v, dict) and ("path" in v or "video" in v) for v in tick
        )

    after = _video_path(client.predict("clip:v_p4_root", api_name="/on_select_video")[0])
    assert after is not None


def test_timer_outputs_exclude_video_players(tmp_path):
    """③④の Timer コールバックの outputs にプレーヤーが入っていないことを構造で確認する。

    「選択IDが変わったときだけ差し替える」方式が守られている限り、Timer の出力に
    gr.Video が含まれることはない（含まれると毎秒再生が中断する）。
    """
    import gradio as gr

    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp_path)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine
    from app.ui.minimal import build_ui

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()
    demo = build_ui(cfg, "mock", service)

    tick_names = {"on_tick", "on_queue_tick", "on_videos_tick", "on_history_tick"}
    seen = set()
    for fn in demo.fns.values():
        if fn.api_name not in tick_names:
            continue
        seen.add(fn.api_name)
        assert not any(isinstance(o, gr.Video) for o in fn.outputs), (
            f"{fn.api_name} の outputs に gr.Video が含まれています"
        )
        assert not any(isinstance(o, gr.Image) for o in fn.outputs)
    assert seen == tick_names

    # 継続開始は gr.Tabs を出力に持つ（①新規生成タブへ切り替えるため）
    for api_name in ("on_start_continuation", "on_history_continuation"):
        fn = next(f for f in demo.fns.values() if f.api_name == api_name)
        assert any(isinstance(o, gr.Tabs) for o in fn.outputs), api_name
        assert any(isinstance(o, gr.Image) for o in fn.outputs), api_name


# ------------------------------------------------------------ ④履歴タブ


def test_history_tab_lists_all_statuses(p4_app):
    """④は成功・失敗・取消・中断・異常データ（QUEUED/RUNNING）まで全部出す。"""
    client, _service, _cfg = p4_app
    listing, choices = client.predict("すべて", api_name="/on_history_tick")

    for job_id in (
        "v_p4_root",
        "v_p4_child",
        "v_p4_failed",
        "v_p4_canceled",
        "v_p4_interrupted",
        "v_p4_queued",
        "v_p4_running",
    ):
        assert job_id in listing, f"{job_id} が履歴一覧にありません"
    assert "完成" in listing and "失敗" in listing
    assert "取消済み" in listing
    assert "中断（アプリ終了のため）" in listing
    # 異常データは注意書きつきで見せる（隠さない）
    assert "生成待ち" in listing and "実行中" in listing
    assert "異常データ" in listing
    # 個別と連結を識別できる（連結成果物は連結行としても並ぶ）
    assert "| 連結 |" in listing
    assert "| 個別 |" in listing
    values = [c[1] if isinstance(c, (list, tuple)) else c for c in choices["choices"]]
    assert "clip:v_p4_child" in values
    assert "concat:v_p4_child" in values


def test_history_tab_shows_columns_required_by_design(p4_app):
    """設計書 §5.5 の列（状態・日時・ID・親ID・長さ・step・seed・backend・時間・分類）。"""
    client, _service, _cfg = p4_app
    listing = client.predict("すべて", api_name="/on_history_tick")[0]

    for header in ("状態", "日時", "ID", "親ID", "長さ", "step", "seed指定", "seed実際",
                   "backend・model", "処理時間", "実行方式", "エラー"):
        assert header in listing, f"列 {header} がありません"
    assert "メモリ不足（OOM）" in listing          # エラー分類の日本語
    assert "MPS backend out of memory" in listing  # エラー本文
    assert "minimax_h3・nf4-2026-07" in listing
    assert "mock" in listing                       # execution_engine
    # 分類の無いエラー（中断など）に「分類なし:」の飾りを付けない
    assert "アプリ終了により中断" in listing
    assert "分類なし" not in listing


def test_history_filter_narrows_rows(p4_app):
    """状態フィルタで表示が絞られる（すべて／成功／失敗）。"""
    client, _service, _cfg = p4_app

    success = client.predict("成功", api_name="/on_history_filter")[0]
    assert "v_p4_root" in success
    assert "v_p4_failed" not in success
    assert "v_p4_queued" not in success

    failed = client.predict("失敗", api_name="/on_history_filter")[0]
    assert "v_p4_failed" in failed
    assert "v_p4_root" not in failed
    assert "メモリ不足（OOM）" in failed

    everything = client.predict("すべて", api_name="/on_history_filter")[0]
    assert "v_p4_failed" in everything and "v_p4_root" in everything


def test_history_detail_and_preview(p4_app):
    """成功記録は詳細＋プレビュー、失敗記録は詳細のみ（プレビューは None）。"""
    client, _service, _cfg = p4_app

    video, detail, tech = client.predict(
        "clip:v_p4_child", "すべて", api_name="/on_select_history"
    )
    assert _video_path(video) is not None
    assert "状態: **完成**" in detail
    assert "v_p4_root" in detail                 # 親ID
    assert "処理時間: 06:52" in detail
    # 内部の言い方（execution_engine）は「詳しい情報」だけに出す（P5 §6.4）
    assert "execution_engine" not in detail
    assert "execution_engine" in tech

    video, detail, tech = client.predict(
        "clip:v_p4_failed", "すべて", api_name="/on_select_history"
    )
    assert _video_path(video) is None
    assert "状態: **失敗**" in detail
    assert "メモリ不足（OOM）" in detail          # エラー分類は主要部に残す
    assert "MPS backend out of memory" not in detail  # 例外文はそのまま出さない
    assert "MPS backend out of memory" in tech
    assert "v_p4_failed" in tech


def test_history_tick_recovers_after_exception(p4_app, monkeypatch):
    """④の Timer が例外を投げても次の tick で回復する。"""
    client, service, _cfg = p4_app

    def boom(*a, **kw):
        raise RuntimeError("履歴一覧の擬似障害")

    monkeypatch.setattr(service.history, "list_records", boom)
    if hasattr(service, "history_rows"):
        monkeypatch.setattr(service, "history_rows", boom)
    broken = client.predict("すべて", api_name="/on_history_tick")
    assert "一覧を取得できません" in broken[0]

    monkeypatch.undo()
    recovered = client.predict("すべて", api_name="/on_history_tick")
    assert "v_p4_root" in recovered[0]


def test_history_json_and_logs_are_not_served(p4_app):
    """履歴JSON・ログは HTTP で配信しない（設計書 §15）。"""
    import urllib.error
    import urllib.request

    client, _service, cfg = p4_app
    for target in (cfg.history_path, cfg.logs_dir / "app.log", cfg.data_root):
        url = f"{client.src.rstrip('/')}/file={target}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status != 200, f"{target} が配信されています"


# ------------------------------------------------------------ 継続生成（§5.2・§10.5）


def _continuation_outputs(result):
    """`/on_start_continuation` の戻り値を名前付きで取り出す（Tabs/Group は API 対象外）。"""
    thumb, banner, parent, prompt, length, steps, seed_random, seed_value, message = result
    return dict(
        thumbnail=thumb,
        banner=banner,
        parent=parent,
        prompt=prompt,
        length=_value_of(length),
        steps=_value_of(steps),
        seed_random=_value_of(seed_random),
        seed_value=_value_of(seed_value),
        message=message,
    )


def test_continuation_banner_and_prefill(p4_app):
    """[この動画の続きを作る] でバナー・サムネイル・プリフィルがそろう。"""
    client, _service, _cfg = p4_app
    out = _continuation_outputs(
        client.predict("clip:v_p4_root", api_name="/on_start_continuation")
    )

    # バナー（設計書 §5.2）
    assert "続きを作成中" in out["banner"]
    assert "v_p4_root" in out["banner"]
    assert "キャラクターと声の説明は変えず、セリフと動きだけ書き換えると安定します" in out["banner"]
    assert "近似的に引き継ぎます" in out["banner"]
    assert "完全" not in out["banner"] or "完全一致" not in out["banner"]

    # 先頭フレームのサムネイル
    thumb = _video_path(out["thumbnail"])
    assert thumb is not None and Path(thumb).is_file()

    # プリフィル（設計書 §10.5）
    assert out["prompt"].startswith("Continue directly from the supplied first frame.")
    assert "dinosaur wizard" in out["prompt"]
    assert out["length"] == "約2.33秒（56フレーム）"
    assert out["steps"] == "4ステップ（高速）"
    assert out["seed_random"] is False           # ランダムのチェックを外す
    assert int(out["seed_value"]) == 42          # 親の seed_used
    assert out["parent"] == "v_p4_root"          # 継続モードの親ID
    assert "継続モード" in out["message"]


def test_continuation_can_be_started_from_history_tab(p4_app):
    """④履歴タブからも同じ継続モードに入れる。"""
    client, _service, _cfg = p4_app
    out = _continuation_outputs(
        client.predict("clip:v_p4_child", api_name="/on_history_continuation")
    )
    assert out["parent"] == "v_p4_child"
    assert "続きを作成中" in out["banner"]
    assert out["prompt"].startswith("Continue directly from the supplied first frame.")


def test_continuation_clear_returns_to_normal(p4_app):
    """[継続モードを解除] でバナーが消え、親ID（＝keyframe）が外れる。"""
    client, _service, _cfg = p4_app
    client.predict("clip:v_p4_root", api_name="/on_start_continuation")

    thumb, banner, parent, message = client.predict(api_name="/on_clear_continuation")
    assert _video_path(thumb) is None
    assert _value_of(banner) == ""
    assert _value_of(parent) == ""
    assert "継続モードを解除しました" in message


@pytest.mark.parametrize(
    "job_id",
    ["v_p4_failed", "v_p4_canceled", "v_p4_interrupted", "v_p4_nopng", "v_no_such_id"],
)
def test_continuation_rejected_with_japanese_message(p4_app, job_id):
    """継続元にできない記録は日本語で理由を返し、継続モードには入らない。"""
    client, _service, _cfg = p4_app
    out = _continuation_outputs(
        client.predict(f"clip:{job_id}", api_name="/on_start_continuation")
    )
    assert out["message"].startswith("❌"), out["message"]
    assert "続きは作れません" in out["message"] or "読み込めませんでした" in out["message"]
    # バナー・プロンプト・シードなど入力欄には触れない（gr.update() のまま）
    assert _value_of(out["parent"]) in (None, "")


def test_continuation_rejected_for_concat_row(p4_app):
    """連結動画の続きは作れない（日本語で案内する）。"""
    client, _service, _cfg = p4_app
    out = _continuation_outputs(
        client.predict("concat:v_p4_child", api_name="/on_start_continuation")
    )
    assert "連結動画の続きは作れません" in out["message"]


def test_continuation_submit_passes_parent_and_keyframe(p4_app, monkeypatch):
    """継続モードの投入は parent_id と keyframe_path つきで submit_generation を呼ぶ。"""
    client, service, cfg = p4_app
    seen: list[dict] = []

    def fake_submit(**kwargs):
        seen.append(kwargs)
        return JobView(
            job_id="v_p4_new",
            status=JobStatus.QUEUED,
            prompt_head=str(kwargs.get("prompt", ""))[:80],
            num_frames=kwargs["num_frames"],
            steps=kwargs["steps"],
            duration_label="2.33秒",
            seed_requested=kwargs.get("seed_requested"),
        )

    monkeypatch.setattr(service, "submit_generation", fake_submit)
    message, _header, _progress = client.predict(
        "Continue directly from the supplied first frame.\nA dinosaur speaks.",
        "約2.33秒（56フレーム）",
        "4ステップ（高速）",
        False,
        42,
        "v_p4_root",
        api_name="/on_submit_v2",
    )

    assert "キューに追加しました" in message
    assert "継続生成" in message and "v_p4_root" in message
    assert len(seen) == 1
    assert seen[0]["parent_id"] == "v_p4_root"
    keyframe = Path(str(seen[0]["keyframe_path"]))
    assert keyframe.is_file()
    assert keyframe.is_relative_to(cfg.data_root)  # data_root の外は渡さない
    assert seen[0]["seed_requested"] == 42


def test_submit_v2_without_parent_is_a_normal_generation(p4_app, monkeypatch):
    """親IDが空なら単発生成（parent_id / keyframe_path を渡さない）。"""
    client, service, _cfg = p4_app
    seen: list[dict] = []

    def fake_submit(**kwargs):
        seen.append(kwargs)
        return JobView(
            job_id="v_p4_plain",
            status=JobStatus.QUEUED,
            prompt_head="x",
            num_frames=kwargs["num_frames"],
            steps=kwargs["steps"],
            duration_label="2.33秒",
            seed_requested=kwargs.get("seed_requested"),
        )

    monkeypatch.setattr(service, "submit_generation", fake_submit)
    message, _h, _p = client.predict(
        "A plain new video.",
        "約2.33秒（56フレーム）",
        "4ステップ（高速）",
        True,
        42,
        "",
        api_name="/on_submit_v2",
    )
    assert "キューに追加しました" in message and "新規生成" in message
    assert "parent_id" not in seen[0] and "keyframe_path" not in seen[0]


def test_submit_v2_reports_continuation_failure_in_japanese(p4_app, monkeypatch):
    """投入時に継続元を解決できなければ日本語で拒否し、キューへは登録しない。"""
    client, service, _cfg = p4_app
    submitted: list[dict] = []
    monkeypatch.setattr(
        service, "submit_generation", lambda **kw: submitted.append(kw)
    )
    message, _h, _p = client.predict(
        "A continuation of a failed job.",
        "約2.33秒（56フレーム）",
        "4ステップ（高速）",
        True,
        42,
        "v_p4_failed",
        api_name="/on_submit_v2",
    )
    assert message.startswith("❌")
    assert not submitted


# ------------------------------------------------------------ 連結・Finder


def test_concat_button_starts_background_job(p4_app, monkeypatch):
    """[ルートからここまでを連結] は即座に戻り、日本語の案内と進行状態を出す。

    ffmpeg を動かさないよう `start_concat` はモックする（本物は「バックグラウンドで
    開始してキーを返す」だけなので、UI 側の文言生成と非ブロッキング性を検証する）。
    """
    client, service, _cfg = p4_app
    seen: list[str] = []
    monkeypatch.setattr(
        service,
        "start_concat",
        lambda job_id: (seen.append(job_id), f"concat-1-{job_id}")[1],
    )

    started = time.monotonic()
    message, status = client.predict("clip:v_p4_child", api_name="/on_start_concat")
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, "連結ボタンがコールバックを長時間ブロックしています"
    assert seen == ["v_p4_child"]
    assert not message.startswith("❌")
    assert "v_p4_child" in message and "連結を開始しました" in message
    assert "concat-1-" not in message  # 内部キーをそのまま画面へ出さない
    assert "連結の状態" in status


def test_concat_passes_through_japanese_service_message(p4_app, monkeypatch):
    """AppService が日本語メッセージを返した場合はそのまま表示する。"""
    client, service, _cfg = p4_app
    monkeypatch.setattr(
        service, "start_concat", lambda job_id: "連結を開始できませんでした: ffmpeg が見つかりません"
    )
    message = client.predict("clip:v_p4_child", api_name="/on_start_concat")[0]
    assert message == "連結を開始できませんでした: ffmpeg が見つかりません"


def test_concat_rejects_single_clip(p4_app):
    """2本未満（ルート単体）の連結は日本語で断る。

    ここは本物の ConcatService を通す。チェーン解決の時点で失敗するため
    ffmpeg は起動しない（成果物も作られない）。
    """
    client, service, cfg = p4_app
    message, status = client.predict("clip:v_p4_root", api_name="/on_start_concat")

    assert "2本" in message or "できません" in message or message.startswith("❌")
    assert "連結の状態" in status
    assert not list(cfg.concat_dir.glob("c_v_p4_root_*.mp4"))

    empty = client.predict("", api_name="/on_start_concat")[0]
    assert "選んでください" in empty


def test_concat_status_states_are_japanese(p4_app, monkeypatch):
    """連結の進行状態（解決中・連結中・検証中・完成・失敗）を日本語で表示する。"""
    from app.core.concat_service import ConcatStatus

    client, service, _cfg = p4_app
    expected = {
        "resolving": "確認中",
        "concatenating": "連結中",
        "verifying": "検証中",
        "done": "完成",
        "failed": "失敗",
    }
    for state, text in expected.items():
        status_obj = ConcatStatus(
            state=state,
            job_id="v_p4_child",
            clips=2,
            message=f"{state} の説明文です",
            output_path=Path("c_v_p4_child_2clips.mp4") if state == "done" else None,
        )
        monkeypatch.setattr(service, "concat_status", lambda s=status_obj: s)
        status = client.predict(api_name="/on_videos_tick")[2]
        assert text in status, f"{state} の表示が日本語になっていません: {status}"
        assert "2本" in status
    # 連結サービスが使えない構成でも UI は落ちない
    monkeypatch.setattr(service, "concat_status", lambda: None)
    assert "利用できません" in client.predict(api_name="/on_videos_tick")[2]


def test_history_tab_concat_button(p4_app, monkeypatch):
    """④履歴タブからも連結を開始できる。"""
    client, service, _cfg = p4_app
    seen: list[str] = []
    monkeypatch.setattr(
        service,
        "start_concat",
        lambda job_id: (seen.append(job_id), f"concat-9-{job_id}")[1],
    )
    message = client.predict("clip:v_p4_child", api_name="/on_history_concat")
    assert seen == ["v_p4_child"]
    assert "連結を開始しました" in message


def test_reveal_in_finder_calls_service_without_subprocess(p4_app, monkeypatch):
    """[Finderで表示] は AppService へ委譲するだけ。subprocess は実行しない。"""
    import subprocess

    client, service, cfg = p4_app
    seen: list[str] = []
    monkeypatch.setattr(
        service,
        "reveal_in_finder",
        lambda target, kind="clip": (
            seen.append(Path(str(target)).name),
            f"✅ Finder で表示しました: {Path(str(target)).name}",
        )[1],
    )

    def forbidden(*a, **kw):  # 実際に Finder を開かせない
        raise AssertionError("テストから subprocess を実行してはいけません")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "call", forbidden)

    message = client.predict("clip:v_p4_root", api_name="/on_reveal_video")
    assert "Finder" in message
    assert seen == ["v_p4_root.mp4"]  # 解決済みの正式成果物パスが渡る

    # 連結行は job_id だけでは個別動画と区別できないため、
    # UI がサーバ側で解決した連結成果物のパスを渡す（data_root 配下・実在）
    from_history = client.predict("concat:v_p4_child", api_name="/on_history_reveal")
    assert "Finder" in from_history
    assert len(seen) == 2 and seen[0] == "v_p4_root.mp4"
    assert seen[1] == "c_v_p4_child_2clips.mp4"  # 連結行では連結ファイルが開く

    empty = client.predict("", api_name="/on_reveal_video")
    assert "選んでください" in empty


def test_reveal_reports_service_error_in_japanese(p4_app, monkeypatch):
    """AppService が拒否した場合は日本語のまま UI に出す（UI は落ちない）。"""
    client, service, _cfg = p4_app

    def refuse(target, kind="clip"):
        raise ValidationError("成果物が見つかりません（Finder では表示できません）")

    monkeypatch.setattr(service, "reveal_in_finder", refuse)
    # 欠損動画は UI 側（サーバ解決）で止まり、サービスまで到達しない
    message = client.predict("clip:v_p4_missing", api_name="/on_reveal_video")
    assert "見つかりません" in message
    # 実在する動画ではサービスの日本語エラーがそのまま出る
    message = client.predict("clip:v_p4_root", api_name="/on_reveal_video")
    assert message.startswith("❌")
    assert "成果物が見つかりません" in message

    def boom(target, kind="clip"):
        raise RuntimeError("擬似障害")

    monkeypatch.setattr(service, "reveal_in_finder", boom)
    message = client.predict("clip:v_p4_root", api_name="/on_reveal_video")
    assert message.startswith("❌")
    # 例外文はそのまま出さず、対象IDと「詳しい情報」への案内を出す（P5 §6.4）
    assert "擬似障害" not in message
    assert "v_p4_root" in message and "詳しい情報" in message


def test_existing_p1_api_signature_is_preserved(p4_app):
    """P1 の `/on_submit`（5引数・3戻り値）が P4 でもそのまま使える。"""
    client, _service, _cfg = p4_app
    info = client.view_api(return_format="dict", print_info=False)
    endpoints = info["named_endpoints"]
    for name, n_in, n_out in (
        ("/on_submit", 5, 3),
        ("/on_tick", 0, 3),
        ("/on_estimate_change", 2, 1),
        ("/on_insert_hint", 1, 1),
        ("/on_queue_tick", 0, 6),
        ("/on_cancel_queued", 1, 7),
        ("/on_restart_worker", 1, 7),
    ):
        assert name in endpoints, f"{name} が失われています"
        assert len(endpoints[name]["parameters"]) == n_in, name
        assert len(endpoints[name]["returns"]) == n_out, name
