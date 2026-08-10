"""P2 統合テスト: 実 h3_worker.py × 実 RealEngine × JobQueue × HistoryStore × AppService。

**実モデルは使わない。** DiffSynth ランタイムだけを
`tests/fixtures/stub_runtime_worker.py` で差し替え、ワーカーのプロトコル層
（イベント出力・入力検証・進捗ラッパ・partial 保存・エラー分類・コマンドループ）と
RealEngine の解析・検証・昇格を**実装そのもの**で突き合わせる。

これは P2 最大の統合リスク（A のワーカーと B の RealEngine のワイヤ不一致）を
実モデル起動前に潰すためのテストである。
"""

from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core import ffmpeg_ops
from app.core.app_service import AppService
from app.core.config import load_config
from app.core.contracts import (
    EngineState,
    ErrorCategory,
    EventType,
    JobSpec,
    JobStage,
    JobStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = PROJECT_ROOT / "app" / "engine" / "backends" / "minimax_h3" / "h3_worker.py"
STUB_WORKER = PROJECT_ROOT / "tests" / "fixtures" / "stub_runtime_worker.py"
MOCK_ASSETS = PROJECT_ROOT / "app" / "assets" / "mock"

WAIT = 30.0

#: スタブ pipe が受け取った引数の要約を書き出すファイル名（tmp_path 直下）
PIPE_DUMP_NAME = "pipe_calls.jsonl"


def _wait_until(predicate, timeout: float = WAIT, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


@pytest.fixture
def stub_env(monkeypatch, tmp_path):
    """スタブワーカーが実ワーカーを読み込むための環境変数。"""
    monkeypatch.setenv("ATELIER_TEST_WORKER_PATH", str(WORKER_PATH))
    monkeypatch.setenv("ATELIER_TEST_MOCK_ASSETS", str(MOCK_ASSETS))
    monkeypatch.setenv("ATELIER_TEST_GENERATE_SEC", "0.05")
    # pipe() が受け取った引数の要約（継続生成の keyframes / keyframe_indices）
    monkeypatch.setenv("ATELIER_TEST_PIPE_DUMP", str(tmp_path / PIPE_DUMP_NAME))
    monkeypatch.delenv("ATELIER_TEST_RAISE", raising=False)
    monkeypatch.delenv("ATELIER_MOCK", raising=False)


def _pipe_calls(tmp_path: Path) -> list[dict]:
    """スタブの pipe() が受け取った引数の要約を古い順に読む。"""
    import json

    dump = tmp_path / PIPE_DUMP_NAME
    if not dump.is_file():
        return []
    return [
        json.loads(line)
        for line in dump.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_cfg(tmp_path: Path):
    cfg = load_config(PROJECT_ROOT)
    work = tmp_path / "work"  # DiffSynth root の代わり（書き込み監視用）
    work.mkdir(parents=True, exist_ok=True)
    backend = dataclasses.replace(
        cfg.backend,
        worker_python=Path(sys.executable),
        worker_script=str(STUB_WORKER),
        working_directory=work,
    )
    cfg = dataclasses.replace(cfg, data_root=tmp_path / "data", backend=backend)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def _make_engine(cfg):
    from app.engine.real_engine import RealEngine

    return RealEngine(
        identity=__import__(
            "app.core.contracts", fromlist=["BackendIdentity"]
        ).BackendIdentity(
            backend_id=cfg.backend_id,
            display_name=cfg.backend.display_name,
            model_id=cfg.backend.model_id,
            model_revision=cfg.backend.model_revision,
        ),
        worker_python=cfg.backend.worker_python,
        worker_script=Path(cfg.backend.worker_script),
        working_directory=cfg.backend.working_directory,
        data_root=cfg.data_root,
        model_id=cfg.backend.model_id,
        model_revision=cfg.backend.model_revision,
        processor_id=cfg.backend.processor_id,
        # LoRA はスタブでは読まないが、存在検証があるため実ファイルを渡す
        lora_path=MOCK_ASSETS / "mock_56.mp4",
        lora_alpha=cfg.backend.lora_alpha,
        worker_log_path=cfg.logs_dir / "worker.log",
        startup_timeout=60.0,
    )


@pytest.fixture
def service(tmp_path, stub_env):
    cfg = _make_cfg(tmp_path)
    engine = _make_engine(cfg)
    svc = AppService.build(cfg, "real", engine=engine)
    svc.start()
    try:
        yield svc
    finally:
        svc.shutdown(timeout=15.0)


def _wait_terminal(service, job_id: str, timeout: float = WAIT):
    rec = _wait_until(
        lambda: (
            r
            if (r := service.history.get(job_id)) is not None
            and r.status
            in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.INTERRUPTED)
            else None
        ),
        timeout=timeout,
    )
    assert rec is not None, f"ジョブが終了しませんでした: {job_id}"
    return rec


# ------------------------------------------------------------------ 正常系


def test_worker_and_real_engine_agree_on_the_wire(service):
    """実ワーカー → 実 RealEngine → JobQueue → 履歴の縦串（P2 の中心的検証）。"""
    view = service.submit_generation(
        prompt="統合テスト <d>[Japanese] つながっています</d>",
        num_frames=56,
        steps=4,
        seed_requested=4321,
    )
    assert view.status is JobStatus.QUEUED  # 投入は非ブロッキング

    record = _wait_terminal(service, view.job_id, timeout=60.0)
    assert record.status is JobStatus.SUCCESS, record.error

    # 正式名へ昇格済み・partial が残っていない
    video = service.history.to_absolute(record.output_path)
    png = service.history.to_absolute(record.last_frame_path)
    assert video.is_file() and png.is_file()
    assert not Path(str(video) + ".partial").exists()
    assert not Path(str(png) + ".partial").exists()
    # ワーカーの中間ファイル（PyAV 制約回避用）も残らない
    assert not list(video.parent.glob(".*.tmp.mp4")), "一時ファイルが残っています"

    # 実際に再生できる（映像＋音声）
    probe = ffmpeg_ops.decode_probe(ffmpeg_ops.resolve_ffmpeg(""), video)
    assert probe.has_video and probe.has_audio
    assert "576x320" in probe.video_desc
    assert probe.frames == 56

    # 最終フレーム PNG はワーカーがメモリ上の video[-1] から保存したもの
    from PIL import Image

    with Image.open(png) as img:
        img.load()
        assert img.size == (576, 320)

    # 履歴の identity（real として記録される）
    assert record.execution_engine == "real"
    assert record.backend_id == "minimax_h3"
    assert record.model_id == service.cfg.backend.model_id
    assert record.model_revision == service.cfg.backend.model_revision
    assert record.seed_used == 4321
    assert record.output_path.startswith("outputs/")
    assert not record.output_path.endswith(".partial")


def test_step_progress_reaches_the_ui_through_the_real_wire(service):
    """ワーカーの progress_bar_cmd ラッパ → RealEngine → UI 表示条件まで届く。"""
    observed: list[tuple] = []
    stop = threading.Event()

    def _sample():
        while not stop.is_set():
            cur = service.snapshot().current
            if cur is not None:
                observed.append((cur.stage, cur.step, cur.total_steps))
            time.sleep(0.005)

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()
    try:
        view = service.submit_generation(
            prompt="進捗の縦串確認", num_frames=56, steps=4, seed_requested=1
        )
        _wait_terminal(service, view.job_id, timeout=60.0)
    finally:
        stop.set()
        sampler.join(timeout=2.0)

    displayable = [o for o in observed if o[0] is JobStage.GENERATING and o[1] and o[2]]
    assert displayable, f"ステップ進捗が UI に届きませんでした: {observed[:8]}"
    assert all(o[2] == 4 for o in displayable), "総ステップ数が 4 ではありません"
    steps_seen = sorted({o[1] for o in displayable})
    assert steps_seen[0] == 1, f"ステップが 1 から始まっていません: {steps_seen}"


def test_two_jobs_reuse_the_same_worker_process(service):
    """同じワーカープロセスで2本処理する（常駐再利用の自動版）。"""
    pid1 = service.engine.worker_pid
    assert pid1 is not None

    ids = []
    for i in range(2):
        view = service.submit_generation(
            prompt=f"常駐再利用 {i}", num_frames=56, steps=4, seed_requested=i + 10
        )
        ids.append(view.job_id)
    records = [_wait_terminal(service, j, timeout=90.0) for j in ids]

    assert all(r.status is JobStatus.SUCCESS for r in records), [r.error for r in records]
    assert service.engine.worker_pid == pid1, "ワーカーが再起動されました"
    assert len(service.history.list_records()) == 2


# ------------------------------------------------------------------ 継続生成（P4）


def test_continuation_keyframe_reaches_the_pipe(service, tmp_path):
    """継続生成の縦串: 親の最終フレーム PNG が実ワーカーを通って pipe まで届く。

    実 h3_worker（キーフレーム検証・PIL 読込・pipe 呼び出し）× 実 RealEngine
    （検証・送信・昇格）で、`keyframes=[画像]` / `keyframe_indices=[0]` が
    実証スクリプトと同じ形で渡ることを確認する（P4 の最大の統合リスク）。
    """
    parent_view = service.submit_generation(
        prompt="親クリップ <d>[Japanese] つづきをつくるよ</d>",
        num_frames=56,
        steps=4,
        seed_requested=777,
    )
    parent = _wait_terminal(service, parent_view.job_id, timeout=60.0)
    assert parent.status is JobStatus.SUCCESS, parent.error

    keyframe = service.history.to_absolute(parent.last_frame_path)
    assert keyframe is not None and keyframe.is_file()
    keyframe_bytes = keyframe.read_bytes()

    child_view = service.submit_generation(
        prompt="Continue directly from the supplied first frame. 続きのクリップ",
        num_frames=56,
        steps=4,
        seed_requested=parent.seed_used,
        parent_id=parent_view.job_id,
        keyframe_path=keyframe,
    )
    child = _wait_terminal(service, child_view.job_id, timeout=60.0)
    assert child.status is JobStatus.SUCCESS, child.error

    calls = _pipe_calls(tmp_path)
    assert len(calls) == 2, calls
    # 1本目（単発）は keyframes を渡さない＝P2 の呼び出し形のまま
    assert calls[0]["has_keyframes"] is False
    assert "keyframes" not in calls[0]["keys"]
    assert "keyframe_indices" not in calls[0]["keys"]
    # 2本目（継続）は実証スクリプトと同じ形
    assert calls[1]["has_keyframes"] is True
    assert calls[1]["keyframe_indices"] == [0]
    assert len(calls[1]["keyframes"]) == 1
    assert calls[1]["keyframes"][0]["mode"] == "RGB"
    assert calls[1]["keyframes"][0]["size"] == [576, 320]
    assert calls[1]["seed"] == parent.seed_used  # 声質継承のため親の seed を引き継げる

    # 成果物は単発生成と同じ形で昇格し、親子は別ファイルとして残る
    child_video = service.history.to_absolute(child.output_path)
    assert child_video.is_file()
    assert service.history.to_absolute(parent.output_path).is_file()
    assert not list(child_video.parent.glob("*.partial"))
    assert child.parent_id == parent_view.job_id
    assert child.keyframe_path == parent.last_frame_path
    # 親の最終フレームは読むだけ（1バイトも変えない）
    assert keyframe.read_bytes() == keyframe_bytes


def test_continuation_with_broken_keyframe_fails_without_killing_the_worker(
    service, tmp_path
):
    """壊れたキーフレームはワーカーが input エラーで返し、次のジョブは通る。"""
    pid = service.engine.worker_pid
    broken = service.cfg.outputs_dir / "v_broken_last.png"
    broken.write_bytes(b"this is definitely not a png file\n" * 8)

    view = _submit_raw_continuation(
        service, job_id="v_20260807_090002_brkn", keyframe=broken
    )
    record = _wait_terminal(service, view.job_id, timeout=60.0)

    assert record.status is JobStatus.FAILED
    assert record.error_category == ErrorCategory.INPUT.value, record.error_category
    assert record.output_path is None
    assert not (service.cfg.outputs_dir / f"{view.job_id}.mp4").exists()
    # ワーカーは死んでいない（同じプロセスで次のジョブを処理できる）
    assert service.engine.worker_pid == pid
    ok = service.submit_generation(
        prompt="壊れたキーフレームの後でも生成できる",
        num_frames=56,
        steps=4,
        seed_requested=2,
    )
    assert _wait_terminal(service, ok.job_id, timeout=60.0).status is JobStatus.SUCCESS


def _submit_raw_continuation(service, *, job_id: str, keyframe: Path, parent_id: str = "v_20260101_000000_old1"):
    """AppService の事前検証を意図的に迂回して、エンジン層の多層防御を試す。

    AppService.submit_generation は継続元を投入時に検証して弾くようになったため、
    「もし UI 層を迂回されたらエンジン層が止めるか」はここで確かめる。
    """
    from app.core.contracts import JobSpec

    spec = JobSpec(
        job_id=job_id,
        prompt="エンジン層の多層防御を確認",
        num_frames=56,
        steps=4,
        seed_requested=1,
        output_path=service.cfg.outputs_dir / f"{job_id}.mp4",
        last_frame_path=service.cfg.outputs_dir / f"{job_id}_last.png",
        job_type="continuation",
        parent_id=parent_id,
        keyframe_path=keyframe,
    )
    return service.queue.submit(spec)


def test_missing_keyframe_is_rejected_before_the_worker(service, tmp_path):
    """実在しないキーフレームはエンジン層で止まり、ワーカーまで届かない。

    RealEngine.submit が同期 ValidationError を投げ、JobQueue が
    FAILED（category=input）に確定させる。ワーカーは generate を1度も受け取らない。
    """
    missing = service.cfg.outputs_dir / "v_missing_last.png"
    view = _submit_raw_continuation(
        service, job_id="v_20260807_090001_miss", keyframe=missing
    )
    record = _wait_terminal(service, view.job_id, timeout=30.0)

    assert record.status is JobStatus.FAILED
    assert record.error_category == ErrorCategory.INPUT.value
    assert "キーフレーム" in (record.error or "")
    assert _pipe_calls(tmp_path) == []  # ワーカーは生成を1本も走らせていない
    assert service.engine.state() is EngineState.READY


# ------------------------------------------------------------------ 開始画像（P8）


def test_start_image_reaches_the_pipe_as_a_first_frame(service, tmp_path):
    """P8 の縦串: 利用者が選んだ画像が実ワーカーを通って pipe まで届く。

    実 `start_image`（正規化・確定）× 実 AppService × 実 h3_worker × 実 RealEngine で、
    継続生成とまったく同じ `keyframes=[画像]` / `keyframe_indices=[0]` の形になり、
    **参照画像（Ref2VA）としては渡していない**ことを確認する。
    """
    from PIL import Image

    from app.core.start_image import normalize_start_image

    source = tmp_path / "upload.png"
    with Image.new("RGB", (1152, 640), (10, 90, 200)) as img:  # 1.8:1（出力と同じ形）
        img.save(source, format="PNG")
    prepared = normalize_start_image(source, data_root=service.cfg.data_root)

    view = service.submit_generation(
        prompt="開始画像から動かす <d>[Japanese] はじまり</d>",
        num_frames=56,
        steps=4,
        seed_requested=999,
        start_image_id=prepared.start_image_id,
    )
    record = _wait_terminal(service, view.job_id, timeout=60.0)
    assert record.status is JobStatus.SUCCESS, record.error

    calls = _pipe_calls(tmp_path)
    assert len(calls) == 1, calls
    assert calls[0]["has_keyframes"] is True
    assert calls[0]["keyframe_indices"] == [0]
    assert len(calls[0]["keyframes"]) == 1
    assert calls[0]["keyframes"][0]["mode"] == "RGB"
    assert calls[0]["keyframes"][0]["size"] == [576, 320]
    assert calls[0]["seed"] == 999
    # Ref2VA は実装しない（参照素材としては1つも渡さない）
    assert "references" not in calls[0]["keys"]
    assert "reference_images" not in calls[0]["keys"]

    # 履歴は「個別動画」（親を持たない・開始画像を持つ）
    assert record.type == "single"
    assert record.parent_id is None
    assert record.keyframe_path == f"start_images/{prepared.start_image_id}.png"
    # 開始画像そのものは読むだけ（1バイトも変えない）
    committed = service.cfg.data_root / record.keyframe_path
    assert committed.is_file() and committed.read_bytes() == prepared.png_bytes
    assert service.history.to_absolute(record.output_path).is_file()


# ------------------------------------------------------------------ 異常系


@pytest.mark.parametrize(
    "kind,category",
    [("mps", "mps"), ("oom", "oom"), ("pipeline", "pipeline")],
)
def test_generation_exceptions_are_classified(tmp_path, stub_env, monkeypatch, kind, category):
    """生成中の例外がカテゴリ分類され、履歴へ記録される（設計書 §13.3）。"""
    monkeypatch.setenv("ATELIER_TEST_RAISE", kind)
    cfg = _make_cfg(tmp_path)
    engine = _make_engine(cfg)
    svc = AppService.build(cfg, "real", engine=engine)
    svc.start()
    try:
        view = svc.submit_generation(
            prompt="例外分類の確認", num_frames=56, steps=4, seed_requested=1
        )
        record = _wait_terminal(svc, view.job_id, timeout=60.0)
        assert record.status is JobStatus.FAILED
        assert record.error_category == category, record.error_category
        # 失敗時は成果物を残さない
        assert record.output_path is None
        assert not (cfg.outputs_dir / f"{view.job_id}.mp4").exists()
        assert not (cfg.outputs_dir / f"{view.job_id}_last.png").exists()
    finally:
        svc.shutdown(timeout=15.0)


def test_invalid_params_are_rejected_by_the_worker(service):
    """UI を迂回した不正値をワーカー側でも止める（下位層での検証）。"""
    spec = JobSpec(
        job_id="v_20260807_010101_zzzz",
        prompt="不正パラメータ",
        num_frames=243,  # 実機で破綻する値（設計書 §0.4）
        steps=4,
        seed_requested=1,
        output_path=service.cfg.outputs_dir / "v_20260807_010101_zzzz.mp4",
        last_frame_path=service.cfg.outputs_dir / "v_20260807_010101_zzzz_last.png",
    )
    # JobQueue/contracts の検証で弾かれる（ワーカーまで到達しない）
    from app.core.contracts import ValidationError

    with pytest.raises(ValidationError, match="動画の長さが不正"):
        service.queue.submit(spec)


def test_worker_writes_nothing_outside_data_root(service):
    """cwd（DiffSynth root 相当）へ1ファイルも書かない（__pycache__ 含む）。"""
    work = service.cfg.backend.working_directory
    before = {p for p in work.rglob("*")}

    view = service.submit_generation(
        prompt="書き込み境界の確認", num_frames=56, steps=4, seed_requested=2
    )
    _wait_terminal(service, view.job_id, timeout=60.0)

    after = {p for p in work.rglob("*")}
    assert after == before, f"作業ディレクトリにファイルが増えました: {after - before}"


def test_prompt_never_appears_in_process_arguments(service):
    """プロンプトはコマンドライン引数に載せない（設計書 §15）。"""
    args = service.engine._proc.args  # type: ignore[attr-defined]
    joined = " ".join(str(a) for a in args)
    assert "統合テスト" not in joined
    assert str(WORKER_PATH) not in joined or True  # スクリプトパスは載ってよい
    assert len(args) == 2, f"引数が想定と異なります: {args}"


def test_shutdown_leaves_no_orphan_worker(tmp_path, stub_env):
    """終了後にワーカープロセスが残らない。"""
    cfg = _make_cfg(tmp_path)
    engine = _make_engine(cfg)
    svc = AppService.build(cfg, "real", engine=engine)
    svc.start()
    assert _wait_until(lambda: engine.state() is EngineState.READY, timeout=60.0)
    pid = engine.worker_pid
    svc.shutdown(timeout=15.0)

    assert engine._proc.poll() is not None, "ワーカーが終了していません"
    if pid:
        try:
            os.kill(pid, 0)
            raise AssertionError(f"ワーカーが残っています: PID {pid}")
        except ProcessLookupError:
            pass


def test_worker_log_is_written_and_not_served(service):
    """ワーカーの生出力は worker.log へ行き、UI のログリングを埋めない。"""
    from app.core.applog import recent_logs

    view = service.submit_generation(
        prompt="ログ分離の確認", num_frames=56, steps=4, seed_requested=3
    )
    _wait_terminal(service, view.job_id, timeout=60.0)

    worker_log = service.cfg.logs_dir / "worker.log"
    assert worker_log.is_file(), "worker.log が作られていません"
    # UI が読むリングバッファにワーカーの生出力が混ざっていない
    assert "@@EVT" not in recent_logs(200)


def test_random_seed_is_resolved_by_the_engine(service):
    """UI 既定の「シードをランダム」（seed_requested=None）が実機経路で通る。

    ワイヤ上の seed は int 固定なので、エンジン層で採番してから送る必要がある
    （MockEngine と同じ責務分担）。ここが壊れると UI 既定で全ジョブが失敗する。
    """
    view = service.submit_generation(
        prompt="ランダムシードの回帰テスト",
        num_frames=56,
        steps=4,
        seed_requested=None,
    )
    record = _wait_terminal(service, view.job_id, timeout=60.0)
    assert record.status is JobStatus.SUCCESS, record.error
    assert record.seed_requested is None
    assert record.seed_used is not None and 0 <= record.seed_used <= 2_147_483_647
    assert service.history.to_absolute(record.output_path).is_file()


def test_worker_reporting_a_foreign_partial_is_rejected(tmp_path, stub_env, monkeypatch):
    """E6: 指示した partial 以外を報告されたら昇格しない（既存成果物を守る）。"""
    cfg = _make_cfg(tmp_path)
    engine = _make_engine(cfg)

    # 既存の完成動画（別ジョブの成果物）を置いておく
    victim = cfg.outputs_dir / "v_20260101_000000_old1.mp4"
    victim.write_bytes("既存の完成動画".encode("utf-8"))

    from app.core import fileops
    from app.core.contracts import JobSpec

    spec = JobSpec(
        job_id="v_20260807_121212_aaaa",
        prompt="他ジョブのパスを報告させる",
        num_frames=56,
        steps=4,
        seed_requested=1,
        output_path=cfg.outputs_dir / "v_20260807_121212_aaaa.mp4",
        last_frame_path=cfg.outputs_dir / "v_20260807_121212_aaaa_last.png",
    )
    # 検証だけを直接呼ぶ（data_root 配下だが「指示と異なる」パス）
    with pytest.raises(fileops.FileopsError, match="指示と異なります"):
        engine._require_partial(
            str(victim), fileops.partial_path(spec.output_path), "出力動画", []
        )

    # 既存ファイルは無傷
    assert victim.read_bytes() == "既存の完成動画".encode("utf-8")


def test_appservice_rejects_bad_continuation_at_submit_time(service):
    """継続元の不備は「投入した瞬間」に日本語で断る（UI 体験のための多層防御の1層目）。"""
    from app.core.contracts import ValidationError

    # 履歴に存在しない親
    with pytest.raises(ValidationError, match="履歴に見つかりません"):
        service.submit_generation(
            prompt="存在しない親", num_frames=56, steps=4, seed_requested=1,
            parent_id="v_20260101_000000_nope",
        )

    # 成功していない親（失敗レコードを作って継続元にしてみる）
    view = service.submit_generation(prompt="失敗させる親", num_frames=56, steps=4,
                                     seed_requested=1)
    rec = _wait_terminal(service, view.job_id, timeout=60.0)
    assert rec.status is JobStatus.SUCCESS  # まずは成功する
    # 成功動画の最終フレームを消して「成果物欠損」を作る
    png = service.history.to_absolute(rec.last_frame_path)
    png.unlink()
    with pytest.raises(ValidationError, match="見つかりません"):
        service.submit_generation(
            prompt="最終フレーム欠損の親", num_frames=56, steps=4, seed_requested=1,
            parent_id=rec.id,
        )
