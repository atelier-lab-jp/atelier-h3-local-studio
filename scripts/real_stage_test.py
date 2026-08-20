"""P2 実機試験スクリプト（メインエージェント／人が明示的に実行する）。

**pytest には含めない。** 実モデル（MiniMax-H3-NF4 + Turbo LoRA）を実際に読み込み、
Mac mini M4 24GB で数分〜十数分かかる。同時に2つ実行しないこと。

使い方:
    .venv/bin/python scripts/real_stage_test.py stage0   # 起動・初期化・ping・shutdown のみ
    .venv/bin/python scripts/real_stage_test.py stage1   # 56f/4step を1本生成
    .venv/bin/python scripts/real_stage_test.py stage2   # 同じワーカーで2本目（常駐再利用）
    .venv/bin/python scripts/real_stage_test.py stage12  # stage1 と stage2 を続けて実行
    .venv/bin/python scripts/real_stage_test.py s3       # P3: 124フレーム・4ステップ（約14分）
    .venv/bin/python scripts/real_stage_test.py s4       # P3: 56フレーム・8ステップ（約13分）
    .venv/bin/python scripts/real_stage_test.py s8       # P3: ワーカー強制終了→自動復旧（約14分）

実機検証済みの条件のみを使う: 576×320 / 24fps / 56フレーム / 4ステップ / seed固定。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# app/main.py と同じく強制代入（setdefault だと外部環境変数で上書きできてしまう）
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.app_service import AppService, estimate_seconds  # noqa: E402
from app.core.applog import setup_logging  # noqa: E402
from app.core.config import load_config  # noqa: E402
from app.core.contracts import EngineState, EventType, JobStatus  # noqa: E402

PROMPT = """
A cute small green dinosaur wizard stands inside a magical atelier.
He raises his wooden staff, sparkling green and golden particles swirl around him,
and he smiles proudly at the camera.
He says clearly in Japanese:
<d>[Japanese] ローカル生成、成功！</d>
Cinematic lighting, smooth natural motion, detailed animation.
No subtitles, no captions, no watermark.
""".strip()

NUM_FRAMES = 56
STEPS = 4
SEED = 42

_results: dict = {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _model_state(cfg) -> dict:
    """モデルファイルの mtime/サイズを記録（DiffSynth 側無変更の確認用）。"""
    root = cfg.backend.working_directory / "models"
    state = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            state[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return state


def stage0(cfg) -> None:
    """起動 → loading_model → loading_lora → ready → ping → shutdown。"""
    from app.engine.real_engine import RealEngine

    log("=== Stage 0: 起動・初期化のみ ===")
    before = _model_state(cfg)

    engine = RealEngine.from_config(cfg)
    t0 = time.monotonic()
    engine.start()
    log(f"ワーカーを起動しました（PID={engine.worker_pid}）")

    stages_seen: list[str] = []
    ready_event = None
    deadline = time.monotonic() + engine._startup_timeout  # RealEngine の監視と揃える
    while time.monotonic() < deadline:
        ev = engine.poll_event(timeout=1.0)
        if ev is None:
            if engine.state() in (EngineState.DEAD, EngineState.HALTED):
                raise SystemExit(f"ワーカーが停止しました: {engine.state()}")
            continue
        if ev.type is EventType.STAGE:
            stages_seen.append(str(ev.stage.value if ev.stage else ev.stage))
            log(f"stage: {stages_seen[-1]}（{time.monotonic() - t0:.1f}秒）")
        elif ev.type is EventType.READY:
            ready_event = ev
            break
        elif ev.type is EventType.ERROR:
            raise SystemExit(f"初期化エラー: {ev.category} {ev.message}\n{ev.detail}")

    if ready_event is None:
        raise SystemExit("READY に到達しませんでした（タイムアウト）")

    init_sec = time.monotonic() - t0
    log(f"READY 到達: {init_sec:.1f}秒")
    log(f"  backend_id={ready_event.backend_id}")
    log(f"  model_id={ready_event.model_id} @ {ready_event.model_revision}")
    caps = ready_event.capabilities
    log(f"  capabilities: audio={caps.audio} seed={caps.seed} "
        f"last_frame={caps.last_frame_output} frames={caps.num_frames} steps={caps.steps} "
        f"{caps.width}x{caps.height}@{caps.fps}")
    assert engine.state() is EngineState.READY, engine.state()

    # ping は戻り値を返さない（pong は非同期）。last_pong_monotonic の更新で確認する。
    before_pong = engine.last_pong_monotonic
    engine.ping()
    alive = False
    for _ in range(300):  # 最大30秒
        if engine.last_pong_monotonic != before_pong:
            alive = True
            break
        time.sleep(0.1)
    log(f"ping 応答（pong 受信）: {alive}")
    assert alive, "ping に応答がありません"

    pid = engine.worker_pid
    engine.shutdown(timeout=30.0)
    log("shutdown 完了")

    # orphan worker が残っていないこと
    if pid:
        try:
            os.kill(pid, 0)
            raise SystemExit(f"!!! ワーカーが残っています: PID {pid}")
        except ProcessLookupError:
            log(f"orphan worker なし（PID {pid} は終了）")

    after = _model_state(cfg)
    changed = [k for k in after if before.get(k) != after[k]]
    added = sorted(set(after) - set(before))
    log(f"モデルファイルの変更: {changed or 'なし'} / 追加: {added or 'なし'}")
    if changed or added:
        raise SystemExit("!!! モデルファイルが変化しました（追加ダウンロードの疑い）")

    _results["stage0"] = {
        "init_sec": round(init_sec, 1),
        "stages": stages_seen,
        "backend_id": ready_event.backend_id,
        "model_id": ready_event.model_id,
        "model_revision": ready_event.model_revision,
        "ping": alive,
        "worker_pid": pid,
    }
    log("=== Stage 0: 合格 ===")


def _generate_one(service, label: str, seed_requested: int | None = SEED) -> dict:
    view = service.submit_generation(
        prompt=PROMPT, num_frames=NUM_FRAMES, steps=STEPS, seed_requested=seed_requested
    )
    log(f"{label}: 投入 {view.job_id}（status={view.status.value}）")
    assert view.status is JobStatus.QUEUED, "投入直後は QUEUED であること"

    t0 = time.monotonic()
    last_report = 0.0
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        rec = service.history.get(view.job_id)
        if rec and rec.status in (JobStatus.SUCCESS, JobStatus.FAILED):
            break
        snap = service.snapshot()
        now = time.monotonic()
        if now - last_report >= 30:
            cur = snap.current
            log(f"  …{now - t0:.0f}秒経過 / engine={snap.engine_state.value} "
                f"stage={cur.stage.value if cur and cur.stage else '-'} "
                f"step={cur.step if cur else '-'}/{cur.total_steps if cur else '-'}")
            last_report = now
        time.sleep(1.0)

    rec = service.history.get(view.job_id)
    if rec is None or rec.status is not JobStatus.SUCCESS:
        raise SystemExit(f"{label}: 生成に失敗しました: {rec.error if rec else 'レコードなし'}")

    video = service.history.to_absolute(rec.output_path)
    png = service.history.to_absolute(rec.last_frame_path)
    log(f"{label}: SUCCESS {rec.elapsed_sec:.1f}秒 / {video.name}")

    # 成果物の検証（H.264 / yuv420p / AAC / フレーム数 / 長さ）
    from app.core import ffmpeg_ops

    ffmpeg = ffmpeg_ops.resolve_ffmpeg(cfg_global.ffmpeg_path)
    probe = ffmpeg_ops.decode_probe(ffmpeg, video)
    log(f"  映像: {probe.video_desc[:100]}")
    log(f"  音声: {probe.audio_desc[:100]}")
    assert probe.has_video and probe.has_audio, "映像または音声がありません"
    assert probe.duration_sec is not None, "再生時間を取得できませんでした"
    log(f"  frames={probe.frames} duration={probe.duration_sec:.2f}s")
    assert "h264" in probe.video_desc.lower() and "yuv420p" in probe.video_desc
    assert "576x320" in probe.video_desc
    assert "aac" in probe.audio_desc.lower()
    assert probe.frames == NUM_FRAMES, f"フレーム数が {probe.frames}"
    assert abs(probe.duration_sec - NUM_FRAMES / 24) < 0.5

    from PIL import Image

    with Image.open(png) as img:
        img.load()
        assert img.size == (576, 320), img.size
    log(f"  最終フレーム PNG: {png.name} {img.size}")

    assert not Path(str(video) + ".partial").exists()
    assert not Path(str(png) + ".partial").exists()
    assert rec.execution_engine == "real", rec.execution_engine
    assert rec.backend_id == "minimax_h3"
    if seed_requested is None:
        # UI 既定の「シードをランダム」経路（エンジン層で採番される）
        assert rec.seed_requested is None
        assert rec.seed_used is not None and 0 <= rec.seed_used <= 2_147_483_647
    else:
        assert rec.seed_used == seed_requested

    return {
        "job_id": rec.id,
        "elapsed_sec": round(rec.elapsed_sec, 1),
        "video": str(video),
        "png": str(png),
        "frames": probe.frames,
        "duration_sec": round(probe.duration_sec, 2),
        "seed_used": rec.seed_used,
    }


def stage12(cfg, do_stage2: bool) -> None:
    """Stage 1（1本目）と Stage 2（同じワーカーで2本目）。"""
    from app.engine.real_engine import RealEngine

    log("=== Stage 1: 56f・4step を1本生成 ===")
    before = _model_state(cfg)
    engine = RealEngine.from_config(cfg)
    service = AppService.build(cfg, "real", engine=engine)
    service.start()

    try:
        t0 = time.monotonic()
        first = _generate_one(service, "Stage1")
        pid1 = engine.worker_pid
        assert pid1 is not None, "ワーカー PID を取得できませんでした（S5 検証が無効になります）"
        _results["stage1"] = {**first, "worker_pid": pid1,
                              "wall_sec": round(time.monotonic() - t0, 1)}
        log(f"=== Stage 1: 合格（worker PID={pid1}）===")

        if do_stage2:
            log("=== Stage 2: 同じワーカーで2本目（常駐再利用）===")
            t1 = time.monotonic()
            # 2本目は UI 既定の「シードをランダム」経路で実機検証する
            second = _generate_one(service, "Stage2", seed_requested=None)
            pid2 = engine.worker_pid
            _results["stage2"] = {**second, "worker_pid": pid2,
                                  "wall_sec": round(time.monotonic() - t1, 1)}
            assert pid1 == pid2, f"ワーカーが再起動されました: {pid1} → {pid2}"
            ratio = second["elapsed_sec"] / max(first["elapsed_sec"], 0.1)
            log(f"  1本目 {first['elapsed_sec']}秒 → 2本目 {second['elapsed_sec']}秒"
                f"（比 {ratio:.2f}）/ worker PID 同一: {pid1}")
            assert 0.5 <= ratio <= 2.0, f"2本目の所要時間が大きく乖離しています（比 {ratio:.2f}）"
            assert len(service.history.list_records()) >= 2
            log("=== Stage 2: 合格 ===")
    finally:
        service.shutdown(timeout=60.0)
        log("停止しました")

    after = _model_state(cfg)
    changed = [k for k in after if before.get(k) != after[k]]
    added = sorted(set(after) - set(before))
    log(f"モデルファイルの変更: {changed or 'なし'} / 追加: {added or 'なし'}")
    if changed or added:
        raise SystemExit("!!! モデルファイルが変化しました")


def stage_s3_s4(cfg, which: str) -> None:
    """S3: 124フレーム・4ステップ ／ S4: 56フレーム・8ステップ の実測（設計書 §17.3）。

    P2 で未実施だった長尺・高ステップの実測を行い、estimates の妥当性を確認する。
    """
    from app.engine.real_engine import RealEngine

    specs = {
        "s3": (124, 4, "S3: 5.17秒（124フレーム）・4ステップ"),
        "s4": (56, 8, "S4: 2.33秒（56フレーム）・8ステップ"),
    }
    frames, steps, label = specs[which]
    log(f"=== {label} ===")
    before = _model_state(cfg)
    engine = RealEngine.from_config(cfg)
    service = AppService.build(cfg, "real", engine=engine)
    service.start()
    try:
        global NUM_FRAMES, STEPS
        NUM_FRAMES, STEPS = frames, steps
        result = _generate_one(service, which.upper())
        estimate = estimate_seconds(frames, steps, cfg.estimates)
        ratio = result["elapsed_sec"] / estimate
        log(f"  実測 {result['elapsed_sec']}秒 / 目安 {estimate:.0f}秒（比 {ratio:.2f}）")
        _results[which] = {**result, "estimate_sec": round(estimate, 1),
                           "ratio": round(ratio, 2), "worker_pid": engine.worker_pid}
        log(f"=== {label}: 合格 ===")
    finally:
        service.shutdown(timeout=60.0)

    after = _model_state(cfg)
    if [k for k in after if before.get(k) != after[k]] or set(after) - set(before):
        raise SystemExit("!!! モデルファイルが変化しました")


def stage_s8(cfg) -> None:
    """S8: 生成中にワーカーを強制終了 → 自動再起動 → 次ジョブが成功する（設計書 §17.3）。

    P3 の中心的な受け入れ基準。実モデルを2本分使うため約14分かかる。
    """
    import os as _os
    import signal as _signal

    from app.core.contracts import JobStatus as _JS
    from app.core.contracts import RestartState
    from app.engine.real_engine import RealEngine

    log("=== S8: ワーカー強制終了からの自動復旧 ===")
    engine = RealEngine.from_config(cfg)
    service = AppService.build(cfg, "real", engine=engine)
    service.start()
    try:
        # 1本目を投入し、生成が始まったらワーカーを SIGKILL する
        first = service.submit_generation(
            prompt=PROMPT, num_frames=NUM_FRAMES, steps=STEPS, seed_requested=SEED
        )
        log(f"1本目を投入: {first.job_id}")
        deadline = time.monotonic() + 600
        killed_pid = None
        while time.monotonic() < deadline:
            cur = service.snapshot().current
            if cur is not None and cur.step:  # デノイズが始まった
                killed_pid = engine.worker_pid
                log(f"生成中（step={cur.step}/{cur.total_steps}）。ワーカー PID {killed_pid} を SIGKILL します")
                _os.kill(killed_pid, _signal.SIGKILL)
                break
            time.sleep(1.0)
        assert killed_pid is not None, "生成が始まりませんでした"

        # 1本目は worker_dead で FAILED になる
        deadline = time.monotonic() + 300
        rec1 = None
        while time.monotonic() < deadline:
            rec1 = service.history.get(first.job_id)
            if rec1 and rec1.status in (_JS.FAILED, _JS.SUCCESS):
                break
            time.sleep(1.0)
        assert rec1 is not None and rec1.status is _JS.FAILED, f"1本目が FAILED になりません: {rec1}"
        log(f"1本目 FAILED / category={rec1.error_category} / {rec1.error}")
        assert rec1.error_category == "worker_dead", rec1.error_category
        assert rec1.output_path is None, "失敗ジョブに成果物が残っています"

        # バックオフ → 再起動 → READY を待つ
        deadline = time.monotonic() + 600
        seen_states = set()
        while time.monotonic() < deadline:
            snap = service.snapshot()
            seen_states.add(snap.restart_state)
            if snap.engine_state.value == "ready" and snap.restart_state is RestartState.IDLE:
                break
            time.sleep(1.0)
        log(f"観測した再起動状態: {sorted(s.value for s in seen_states)}")
        new_pid = engine.worker_pid
        log(f"再起動後のワーカー PID: {new_pid}（旧 {killed_pid}）")
        assert new_pid is not None and new_pid != killed_pid, "ワーカーが再起動されていません"

        # 2本目が成功する
        second = service.submit_generation(
            prompt=PROMPT, num_frames=NUM_FRAMES, steps=STEPS, seed_requested=SEED
        )
        log(f"2本目を投入: {second.job_id}")
        rec2 = None
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            rec2 = service.history.get(second.job_id)
            if rec2 and rec2.status in (_JS.SUCCESS, _JS.FAILED):
                break
            time.sleep(2.0)
        assert rec2 is not None and rec2.status is _JS.SUCCESS, f"2本目が成功しません: {rec2}"
        log(f"2本目 SUCCESS {rec2.elapsed_sec:.1f}秒")

        _results["s8"] = {
            "killed_pid": killed_pid, "restarted_pid": new_pid,
            "first_status": rec1.status.value, "first_category": rec1.error_category,
            "restart_states": sorted(s.value for s in seen_states),
            "second_status": rec2.status.value, "second_elapsed": round(rec2.elapsed_sec, 1),
        }
        log("=== S8: 合格 ===")
    finally:
        service.shutdown(timeout=60.0)
        # orphan worker が残っていないこと
        pid = engine.worker_pid
        if pid:
            try:
                _os.kill(pid, 0)
                log(f"!!! ワーカーが残っています: PID {pid}")
            except ProcessLookupError:
                log("orphan worker なし")


def main() -> int:
    global cfg_global
    stage = sys.argv[1] if len(sys.argv) > 1 else "stage0"
    cfg = load_config(PROJECT_ROOT)
    cfg_global = cfg
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.logs_dir, cfg.log_level, cfg.log_max_bytes, cfg.log_backup_count)

    log(f"実機試験を開始します: {stage}")
    log(f"  worker_python = {cfg.backend.worker_python}")
    log(f"  working_dir   = {cfg.backend.working_directory}")
    log(f"  data_root     = {cfg.data_root}")

    if stage == "stage0":
        stage0(cfg)
    elif stage == "stage1":
        stage12(cfg, do_stage2=False)
    elif stage in ("stage2", "stage12"):
        stage12(cfg, do_stage2=True)
    elif stage in ("s3", "s4"):
        stage_s3_s4(cfg, stage)
    elif stage == "s8":
        stage_s8(cfg)
    else:
        print(f"不明なステージ: {stage}")
        return 2

    out = PROJECT_ROOT / "data" / "logs" / f"real_stage_{stage}_result.json"
    out.write_text(json.dumps(_results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"結果を書き出しました: {out}")
    print(json.dumps(_results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
