"""P1 結合テスト（設計書 §17.2-1 / §17.2-2 ＋ 失敗混在ケース）。

実データ領域は使わず、tmp_path を data_root に差し替えて実行する。
MockEngine には短縮 sleep を注入し、実時間で10〜20秒待たない。
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.core import ffmpeg_ops
from app.core.app_service import AppService
from app.core.config import load_config
from app.core.contracts import MOCK_FAIL_PREFIX, JobStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 各 sleep をこの秒数で頭打ちにする（進捗を観測できる程度に短く保つ）
FAST_SLEEP_CAP = 0.02


def _fast_sleep(seconds: float) -> None:
    time.sleep(min(seconds, FAST_SLEEP_CAP))


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = load_config(PROJECT_ROOT)
    cfg = replace(cfg, data_root=tmp_path)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=_fast_sleep)
    svc = AppService.build(cfg, "mock", engine=engine)
    svc.start()
    try:
        yield svc
    finally:
        svc.shutdown(timeout=5.0)


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_terminal(service, job_id: str, timeout: float = 30.0):
    assert _wait_until(
        lambda: (r := service.history.get(job_id)) is not None
        and r.status
        in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELED, JobStatus.INTERRUPTED),
        timeout=timeout,
    ), f"ジョブが終了しませんでした: {job_id}"
    return service.history.get(job_id)


class _Sampler(threading.Thread):
    """スナップショットを高頻度で観測し、直列性と待機数の推移を記録する。"""

    def __init__(self, service):
        super().__init__(daemon=True)
        self._service = service
        # threading.Thread は内部で _stop() を使うため、その名前は避ける
        self._stop_flag = threading.Event()
        self.max_running = 0
        self.queue_sizes: list[int] = []

    def run(self) -> None:
        while not self._stop_flag.is_set():
            snap = self._service.snapshot()
            running = 1 if snap.current is not None else 0
            self.max_running = max(self.max_running, running)
            if not self.queue_sizes or self.queue_sizes[-1] != snap.queue_size:
                self.queue_sizes.append(snap.queue_size)
            time.sleep(0.005)

    def stop(self) -> None:
        self._stop_flag.set()
        self.join(timeout=2.0)


# ------------------------------------------------------------------ 17.2-1


def test_17_2_1_single_job_end_to_end(service):
    """起動 → 初期化 → READY → 単発投入 → progress → saving → SUCCESS → 履歴1件。"""
    view = service.submit_generation(
        prompt="A cute small green dinosaur wizard. <d>[Japanese] ローカル生成、成功！</d>",
        num_frames=56,
        steps=4,
        seed_requested=42,
    )
    # 投入直後は QUEUED（コールバックは生成完了を待たない）
    assert view.status == JobStatus.QUEUED

    record = _wait_terminal(service, view.job_id)
    assert record.status == JobStatus.SUCCESS, record.error

    # 成果物が正式名で存在する
    video = service.history.to_absolute(record.output_path)
    png = service.history.to_absolute(record.last_frame_path)
    assert video is not None and video.is_file() and video.stat().st_size > 0
    assert png is not None and png.is_file() and png.stat().st_size > 0
    assert not Path(str(video) + ".partial").exists()
    assert not Path(str(png) + ".partial").exists()

    # data_root 配下に収まっている（ブラウザ配信の allowed_paths 範囲）
    assert video.resolve().is_relative_to(service.cfg.data_root.resolve())

    # 実際に再生できる形式（映像＋音声が読める）
    probe = ffmpeg_ops.decode_probe(ffmpeg_ops.resolve_ffmpeg(""), video)
    assert probe.has_video and probe.has_audio
    assert "576x320" in probe.video_desc

    # v1.2 スキーマの backend identity が記録されている
    assert record.execution_engine == "mock"
    assert record.backend_id == "minimax_h3"
    assert record.model_id == service.cfg.backend.model_id
    assert record.model_revision == service.cfg.backend.model_revision
    assert record.seed_used == 42
    assert record.num_frames == 56 and record.steps == 4
    assert record.width == 576 and record.height == 320 and record.fps == 24
    assert record.elapsed_sec is not None and record.started_at and record.finished_at

    # 履歴は1件
    assert len(service.history.list_records()) == 1

    # UI がプレビューに使う最新完成動画として取得できる
    latest = service.latest_completed()
    assert latest is not None and latest.job_id == record.id


# ------------------------------------------------------------------ 17.2-2


def test_17_2_2_three_jobs_serialized(service):
    """3件連続投入 → 常に1件のみ RUNNING → 待機数 2→1→0 → 3件とも完了。"""
    sampler = _Sampler(service)
    sampler.start()
    try:
        ids = [
            service.submit_generation(
                prompt=f"テストプロンプト {i}", num_frames=56, steps=4, seed_requested=i
            ).job_id
            for i in range(3)
        ]
        assert service.snapshot().queue_size >= 1  # 直列なので必ず待機が発生する

        records = [_wait_terminal(service, job_id, timeout=60.0) for job_id in ids]
    finally:
        sampler.stop()

    assert sampler.max_running <= 1, "同時に2件以上が RUNNING になりました"

    # 待機数はピーク（投入直後）以降、2 → 1 → 0 と単調に減る
    sizes = sampler.queue_sizes
    peak = max(sizes)
    assert peak >= 2, f"待機列が積み上がりませんでした: {sizes}"
    tail = sizes[sizes.index(peak) :]
    assert tail == sorted(tail, reverse=True), f"待機数が単調に減っていません: {sizes}"
    assert 2 in tail and 1 in tail and tail[-1] == 0, f"待機数の推移が不正です: {sizes}"

    assert all(r.status == JobStatus.SUCCESS for r in records)
    assert len(service.history.list_records()) == 3
    for r in records:
        assert service.history.to_absolute(r.output_path).is_file()
        assert service.history.to_absolute(r.last_frame_path).is_file()

    # 投入順（FIFO）に完了している
    finished = [r.finished_at for r in records]
    assert finished == sorted(finished)


def test_mock_fail_in_middle_keeps_queue_alive(service):
    """3件中の2件目を [MOCK_FAIL] にしても、1件目成功・3件目成功で継続する。"""
    prompts = [
        "1本目 正常",
        f"{MOCK_FAIL_PREFIX} 2本目 わざと失敗",
        "3本目 正常",
    ]
    ids = [
        service.submit_generation(
            prompt=p, num_frames=56, steps=4, seed_requested=None
        ).job_id
        for p in prompts
    ]
    records = [_wait_terminal(service, job_id, timeout=60.0) for job_id in ids]

    assert records[0].status == JobStatus.SUCCESS
    assert records[1].status == JobStatus.FAILED
    assert records[1].error  # エラー要約が残る
    assert records[1].error_category == "input"  # プロンプト起因＝非fatal（§13.3）
    assert records[2].status == JobStatus.SUCCESS

    # 失敗ジョブは成果物を残さない（履歴も null、実ディスクにも正式名が無い）
    assert records[1].output_path is None
    failed_id = records[1].id
    assert not (service.cfg.outputs_dir / f"{failed_id}.mp4").exists()
    assert not (service.cfg.outputs_dir / f"{failed_id}_last.png").exists()

    # 失敗後もアプリ・キュー・エンジンが生存し、4件目を処理できる
    extra = service.submit_generation(
        prompt="4本目 失敗後も動く", num_frames=124, steps=8, seed_requested=7
    )
    rec4 = _wait_terminal(service, extra.job_id, timeout=60.0)
    assert rec4.status == JobStatus.SUCCESS
    assert rec4.seed_used == 7
    assert rec4.num_frames == 124 and rec4.steps == 8


def test_random_seed_is_recorded(service):
    """seed ランダム指定でも実際に使われた seed が履歴へ記録される。"""
    view = service.submit_generation(
        prompt="シードランダムの確認", num_frames=56, steps=4, seed_requested=None
    )
    record = _wait_terminal(service, view.job_id)
    assert record.status == JobStatus.SUCCESS
    assert record.seed_requested is None
    assert record.seed_used is not None and 0 <= record.seed_used <= 2_147_483_647


def test_history_survives_restart_and_marks_interrupted(service, tmp_path):
    """アプリ再起動を模して、残存 QUEUED/RUNNING が INTERRUPTED になる。"""
    view = service.submit_generation(
        prompt="再起動テスト", num_frames=56, steps=4, seed_requested=1
    )
    _wait_terminal(service, view.job_id)
    service.shutdown(timeout=5.0)

    # 中断状態のレコードを人為的に作る（前回終了時に残っていた想定）
    from datetime import datetime

    from app.core.contracts import BackendIdentity, JobSpec
    from app.core.history import HistoryRecord, HistoryStore

    store = HistoryStore(service.cfg.history_path, service.cfg.data_root)
    store.load()
    spec = JobSpec(
        job_id="v_20260807_010101_zzzz",
        prompt="前回の残り",
        num_frames=56,
        steps=4,
        seed_requested=1,
        output_path=service.cfg.outputs_dir / "v_20260807_010101_zzzz.mp4",
        last_frame_path=service.cfg.outputs_dir / "v_20260807_010101_zzzz_last.png",
    )
    identity = BackendIdentity(
        backend_id=service.cfg.backend_id,
        display_name=service.cfg.backend.display_name,
        model_id=service.cfg.backend.model_id,
        model_revision=service.cfg.backend.model_revision,
    )
    store.add(
        HistoryRecord.from_job_spec(
            spec,
            identity=identity,
            execution_engine="mock",
            app_version="1.0.0",
            data_root=service.cfg.data_root,
            created_at=datetime.now(),
        )
    )

    # 再読込 → 起動時復旧
    store2 = HistoryStore(service.cfg.history_path, service.cfg.data_root)
    store2.load()
    count = store2.startup_recover()
    assert count == 1
    assert store2.get("v_20260807_010101_zzzz").status == JobStatus.INTERRUPTED
    # 完了済みレコードは変更されない
    assert store2.get(view.job_id).status == JobStatus.SUCCESS


def test_history_file_is_valid_utf8_json_on_disk(service):
    """履歴ファイルが実ディスク上でも v1.2 スキーマ・UTF-8・原子的保存であること。"""
    import json

    view = service.submit_generation(
        prompt="日本語プロンプトの保存確認 <d>[Japanese] こんにちは</d>",
        num_frames=56,
        steps=4,
        seed_requested=7,
    )
    _wait_terminal(service, view.job_id)

    raw = service.cfg.history_path.read_text(encoding="utf-8")
    assert "\\u" not in raw, "日本語が Unicode エスケープされています"
    assert "日本語プロンプトの保存確認" in raw

    doc = json.loads(raw)
    assert doc["schema_version"] == 1
    rec = doc["records"][0]
    for field in (
        "execution_engine", "backend_id", "model_id", "model_revision",
        "backend_params", "seed_requested", "seed_used", "parent_id",
        "output_path", "last_frame_path", "elapsed_sec", "app_version",
    ):
        assert field in rec, f"v1.2 スキーマの {field} がありません"
    assert rec["output_path"].startswith("outputs/")  # data_root 相対で保存
    assert not list(service.cfg.data_root.glob("*.tmp")), "tmp が残っています"


def test_cancel_queued_job_is_recorded_in_history(service):
    """待機中ジョブの取消が実 HistoryStore まで CANCELED として届く。"""
    first = service.submit_generation(
        prompt="1本目（実行中にする）", num_frames=124, steps=8, seed_requested=1
    )
    second = service.submit_generation(
        prompt="2本目（取り消す）", num_frames=56, steps=4, seed_requested=2
    )
    assert _wait_until(lambda: service.queue.queue_size() >= 1)

    assert service.queue.cancel_queued(second.job_id) is True
    assert _wait_until(
        lambda: service.history.get(second.job_id).status == JobStatus.CANCELED
    )
    # 取消済みは実行されない
    assert service.history.get(second.job_id).output_path is None
    # 1本目は最後まで処理される
    assert _wait_terminal(service, first.job_id, timeout=60.0).status == JobStatus.SUCCESS


def test_running_job_at_shutdown_becomes_interrupted_on_restart(tmp_path, monkeypatch):
    """生成中にアプリを終了すると、次回起動で INTERRUPTED として確定する（本番経路）。"""
    monkeypatch.delenv("ATELIER_MOCK", raising=False)
    cfg = load_config(PROJECT_ROOT)
    cfg = replace(cfg, data_root=tmp_path)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.core.history import HistoryStore
    from app.engine.mock_engine import MockEngine

    # 生成に時間がかかるようにして、実行中に終了させる
    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: time.sleep(min(s, 0.3)))
    svc = AppService.build(cfg, "mock", engine=engine)
    svc.start()
    view = svc.submit_generation(
        prompt="実行中に終了する", num_frames=56, steps=4, seed_requested=3
    )
    assert _wait_until(
        lambda: svc.history.get(view.job_id).status == JobStatus.RUNNING, timeout=30
    ), "RUNNING になりませんでした"
    svc.shutdown(timeout=5.0)

    # 終了時点では RUNNING のまま残る（自動再実行はしない）
    assert svc.history.get(view.job_id).status == JobStatus.RUNNING

    # 次回起動で INTERRUPTED へ確定
    store = HistoryStore(cfg.history_path, cfg.data_root)
    store.load()
    assert store.startup_recover() == 1
    rec = store.get(view.job_id)
    assert rec.status == JobStatus.INTERRUPTED
    assert rec.output_path is None  # 未昇格の成果物は参照させない


def test_step_progress_becomes_visible_to_ui(service):
    """UI が「生成中 ステップ i/N」を出せる状態（stage=GENERATING かつ step/total）を観測する。"""
    from app.core.contracts import JobStage

    observed: list[tuple] = []
    stop = threading.Event()

    def _sample():
        while not stop.is_set():
            cur = service.snapshot().current
            if cur is not None:
                observed.append((cur.stage, cur.step, cur.total_steps))
            time.sleep(0.003)

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()
    try:
        view = service.submit_generation(
            prompt="進捗表示の回帰テスト", num_frames=56, steps=4, seed_requested=5
        )
        _wait_terminal(service, view.job_id, timeout=60.0)
    finally:
        stop.set()
        sampler.join(timeout=2.0)

    generating = [o for o in observed if o[0] is JobStage.GENERATING]
    assert generating, f"stage=GENERATING が一度も観測されませんでした: {set(o[0] for o in observed)}"
    # UI の表示条件（stage が GENERATING かつ step/total が揃う）を満たす瞬間がある
    displayable = [o for o in generating if o[1] and o[2]]
    assert displayable, f"ステップ表示の条件を満たしませんでした: {generating[:5]}"
    # 最終ステップ直後に SAVING へ移るため 4/4 の瞬間は取り逃しうる。
    # 「1 から始まる連続したステップが総数4として見えること」を検証する。
    steps_seen = sorted({o[1] for o in displayable})
    assert steps_seen[0] == 1, f"ステップ系列が 1 から始まっていません: {steps_seen}"
    assert steps_seen == list(range(1, len(steps_seen) + 1)), f"ステップが不連続: {steps_seen}"
    assert all(o[2] == 4 for o in displayable), "総ステップ数が 4 ではありません"
