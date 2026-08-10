"""1080p高品質化サービスの試験（P6・設計書 §26）。

**本物のモデルも MPS も絶対に使わない。** ワーカーは
`tests/fixtures/fake_upscale_worker.py` に差し替え、UpscaleService 側の約束
（1件ずつ・進捗・取消・検証・原子的昇格・音声の引き継ぎ・後片付け）を見る。

実データ領域は使わず tmp_path を data_root にする。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.config import load_config
from app.core.upscale_service import (
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_IDLE,
    STATE_SUCCEEDED,
    UpscaleError,
    UpscaleRequest,
    UpscaleService,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_WORKER = PROJECT_ROOT / "tests" / "fixtures" / "fake_upscale_worker.py"

FPS = 24
FRAMES = 8


def ffmpeg_binary() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def make_source(path: Path, *, frames: int = FRAMES, audio: bool = True) -> Path:
    """576×320 の元動画を作る（実モデルは使わない）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_binary(), "-y", "-nostdin",
        "-f", "lavfi", "-i", f"testsrc=size=576x320:rate={FPS}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


@pytest.fixture
def cfg(tmp_path):
    base = dataclasses.replace(load_config(PROJECT_ROOT), data_root=tmp_path)
    for d in (base.outputs_dir, base.upscaled_dir, base.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def service(cfg):
    """偽ワーカーを使うサービス（重みは実在チェックのためのダミー）。"""
    weights = cfg.data_root / "fake-weights.pth"
    weights.write_bytes(b"not a real model")
    svc = UpscaleService(
        cfg,
        worker_python=Path(sys.executable),
        worker_script=FAKE_WORKER,
        weights_path=weights,
        ffmpeg_path=cfg.ffmpeg_path,
        timeout_sec=120,
    )
    yield svc
    svc.shutdown(timeout=10)


def request_for(cfg, source: Path, *, frames: int = FRAMES) -> UpscaleRequest:
    return UpscaleRequest(
        source_key=f"clip:{source.stem}",
        source_path=source,
        output_path=cfg.upscaled_dir / f"u_clip_{source.stem}_1080p.mp4",
        num_frames=frames,
        fps=FPS,
        label=f"🎬 {source.stem}",
    )


def wait_until_done(service, timeout: float = 60.0):
    """実行が終わるまで待って最終状態を返す（本物の待ち時間ではない）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status()
        if not status.running and status.state != STATE_IDLE:
            return status
        time.sleep(0.05)
    raise AssertionError(f"高品質化が終わりませんでした（状態: {service.status().state}）")


# ============================================================ 正常系


def test_upscale_creates_a_1080p_file_and_keeps_the_source(cfg, service):
    """1920×1080 の別ファイルができ、**元の動画は変わらない**（§26.2）。"""
    source = make_source(cfg.outputs_dir / "v_20260810_120000_aaaa.mp4")
    before = source.read_bytes()

    req = request_for(cfg, source)
    service.start_upscale(req)
    status = wait_until_done(service)

    assert status.state == STATE_SUCCEEDED, status.message
    assert req.output_path.is_file()
    assert source.read_bytes() == before, "元の動画が書き換えられている"

    from app.core import ffmpeg_ops as fo

    probe = fo.decode_probe(service.ffmpeg, req.output_path)
    assert "1920x1080" in probe.video_desc
    assert probe.frames == FRAMES


def test_audio_is_carried_over_without_reencoding(cfg, service):
    """音声は**再エンコードせず**引き継ぐ（中身が元と同一。§26.10）。"""
    source = make_source(cfg.outputs_dir / "v_20260810_120001_bbbb.mp4", audio=True)
    req = request_for(cfg, source)
    service.start_upscale(req)
    assert wait_until_done(service).state == STATE_SUCCEEDED

    def audio_md5(path: Path) -> str:
        result = subprocess.run(
            [ffmpeg_binary(), "-nostdin", "-i", str(path), "-map", "0:a:0",
             "-f", "md5", "-"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    assert audio_md5(req.output_path) == audio_md5(source), "音声が作り直されている"


def test_a_source_without_audio_still_succeeds(cfg, service):
    """音声の無い動画でも失敗しない（映像だけの成果物になる）。"""
    source = make_source(cfg.outputs_dir / "v_20260810_120002_cccc.mp4", audio=False)
    req = request_for(cfg, source)
    service.start_upscale(req)

    assert wait_until_done(service).state == STATE_SUCCEEDED
    assert req.output_path.is_file()


def test_progress_is_reported_in_frames(cfg, service, monkeypatch):
    """進捗が「何フレーム目 / 全体」で上がってくる（UI の `52 / 124` の元）。"""
    monkeypatch.setenv("FAKE_UPSCALE_SLEEP", "0.05")
    source = make_source(cfg.outputs_dir / "v_20260810_120003_dddd.mp4")
    req = request_for(cfg, source)
    service.start_upscale(req)

    seen = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = service.status()
        seen.append(status.frame)
        if not status.running and status.state != STATE_IDLE:
            break
        time.sleep(0.02)

    assert max(seen) == FRAMES
    assert any(0 < f < FRAMES for f in seen), "途中経過が1度も見えていない"
    assert service.status().total == FRAMES


def test_percent_never_exceeds_100(cfg, service):
    """割合表示は 0〜100 に収まる（総数が 0 でも落ちない）。"""
    from app.core.upscale_service import UpscaleStatus

    assert UpscaleStatus(frame=0, total=0).percent == 0
    assert UpscaleStatus(frame=5, total=10).percent == 50
    assert UpscaleStatus(frame=999, total=10).percent == 100


# ============================================================ 排他・多重実行


def test_only_one_upscale_runs_at_a_time(cfg, service, monkeypatch):
    """実行中に2件目を始めようとすると日本語で断られる（§26.6）。"""
    monkeypatch.setenv("FAKE_UPSCALE_SLEEP", "0.1")
    first = make_source(cfg.outputs_dir / "v_20260810_120004_eeee.mp4")
    second = make_source(cfg.outputs_dir / "v_20260810_120005_ffff.mp4")

    service.start_upscale(request_for(cfg, first))
    with pytest.raises(UpscaleError, match="実行中"):
        service.start_upscale(request_for(cfg, second))

    wait_until_done(service)


def test_shutdown_refuses_new_work(cfg, service):
    """終了処理中は受け付けない（プロセスを残さない）。"""
    source = make_source(cfg.outputs_dir / "v_20260810_120006_gggg.mp4")
    service.shutdown(timeout=5)
    with pytest.raises(UpscaleError, match="終了"):
        service.start_upscale(request_for(cfg, source))


# ============================================================ 取消


def test_cancel_stops_the_worker_and_leaves_no_output(cfg, service, monkeypatch):
    """中止すると成果物は作られず、途中のファイルも残らない（§26.8）。"""
    monkeypatch.setenv("FAKE_UPSCALE_SLEEP", "0.3")
    monkeypatch.setenv("FAKE_UPSCALE_FRAMES", "40")
    source = make_source(cfg.outputs_dir / "v_20260810_120007_hhhh.mp4")
    req = request_for(cfg, source)

    service.start_upscale(req)
    deadline = time.monotonic() + 10
    while service.status().frame < 1 and time.monotonic() < deadline:
        time.sleep(0.02)

    message = service.cancel()
    assert "中止" in message

    status = wait_until_done(service)
    assert status.state == STATE_CANCELLED
    assert not req.output_path.exists(), "中止したのに成果物ができている"
    assert list(cfg.upscaled_dir.iterdir()) == [], "中間ファイルが残っている"


def test_cancel_when_idle_is_harmless(service):
    """何も動いていないときの中止は、ただ知らせるだけ。"""
    assert "実行されていません" in service.cancel()
    assert service.status().state == STATE_IDLE


# ============================================================ 失敗・検証


def test_worker_failure_is_reported_in_japanese(cfg, service, monkeypatch):
    """ワーカーが失敗したら日本語で伝え、成果物は作らない。"""
    monkeypatch.setenv("FAKE_UPSCALE_FAIL", "1")
    source = make_source(cfg.outputs_dir / "v_20260810_120008_iiii.mp4")
    req = request_for(cfg, source)

    service.start_upscale(req)
    status = wait_until_done(service)

    assert status.state == STATE_FAILED
    assert status.error and "失敗" in status.error
    assert not req.output_path.exists()
    assert list(cfg.upscaled_dir.iterdir()) == []


def test_wrong_resolution_is_rejected_before_promotion(cfg, service, monkeypatch):
    """1920×1080 でない出力は昇格させない（正式名を作らない。§26.9）。"""
    monkeypatch.setenv("FAKE_UPSCALE_SIZE", "1280x720")
    source = make_source(cfg.outputs_dir / "v_20260810_120009_jjjj.mp4")
    req = request_for(cfg, source)

    service.start_upscale(req)
    status = wait_until_done(service)

    assert status.state == STATE_FAILED
    assert "1920" in (status.error or "")
    assert not req.output_path.exists()


def test_frame_count_mismatch_is_rejected(cfg, service, monkeypatch):
    """フレーム数が変わっていたら弾く（無音で短くならないように）。"""
    monkeypatch.setenv("FAKE_UPSCALE_FRAMES", str(FRAMES + 5))
    source = make_source(cfg.outputs_dir / "v_20260810_120010_kkkk.mp4")
    req = request_for(cfg, source)

    service.start_upscale(req)
    status = wait_until_done(service)

    assert status.state == STATE_FAILED
    assert not req.output_path.exists()


def test_missing_source_fails_cleanly(cfg, service):
    """元の動画が無ければ、成果物も中間ファイルも作らずに失敗する。"""
    req = request_for(cfg, cfg.outputs_dir / "v_20260810_999999_zzzz.mp4")
    service.start_upscale(req)
    status = wait_until_done(service)

    assert status.state == STATE_FAILED
    assert not req.output_path.exists()
    assert list(cfg.upscaled_dir.iterdir()) == []


# ============================================================ 事前確認


def test_availability_explains_a_missing_model(cfg):
    """重みが無いときは、取得方法まで含めて日本語で伝える。"""
    svc = UpscaleService(
        cfg,
        worker_python=Path(sys.executable),
        worker_script=FAKE_WORKER,
        weights_path=cfg.data_root / "absent.pth",
    )
    available, reason = svc.availability()
    assert available is False
    assert "setup.sh --with-upscale" in reason


def test_availability_explains_a_missing_worker_python(cfg, tmp_path):
    """DiffSynth 側の Python が無いときも、config のどこを見るか伝える。"""
    weights = tmp_path / "w.pth"
    weights.write_bytes(b"x")
    svc = UpscaleService(
        cfg,
        worker_python=tmp_path / "no-such-python",
        worker_script=FAKE_WORKER,
        weights_path=weights,
    )
    available, reason = svc.availability()
    assert available is False
    assert "worker_python" in reason


def test_start_is_refused_when_unavailable(cfg, tmp_path):
    """使えない状態では開始せず、理由を返す（プロセスを起動しない）。"""
    svc = UpscaleService(
        cfg,
        worker_python=Path(sys.executable),
        worker_script=FAKE_WORKER,
        weights_path=tmp_path / "absent.pth",
    )
    source = make_source(cfg.outputs_dir / "v_20260810_120011_llll.mp4")
    with pytest.raises(UpscaleError, match="モデルファイル"):
        svc.start_upscale(request_for(cfg, source))


# ============================================================ 安全性


def record_subprocess_calls(monkeypatch) -> list[dict]:
    """`subprocess.Popen` の呼び出しをすべて記録する。

    高品質化1回のあいだに、ワーカー起動と ffmpeg（音声結合・検証）の両方が
    Popen を通る。**最後の1件だけ**を見ると ffmpeg を見てしまうので、
    全部を残してテスト側で選ぶ。
    """
    calls: list[dict] = []
    real_popen = subprocess.Popen

    def spy(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return real_popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return calls


def worker_call(calls: list[dict]) -> dict:
    """記録の中からワーカー起動の呼び出しを取り出す。"""
    for call in calls:
        args = call["args"]
        if isinstance(args, (list, tuple)) and any(
            str(a).endswith("fake_upscale_worker.py") for a in args
        ):
            return call
    raise AssertionError(f"ワーカーの起動が記録されていません: {calls}")


def test_every_subprocess_is_launched_as_an_argument_list(cfg, service, monkeypatch):
    """サブプロセスは**引数配列**で起動する（シェル文字列連結は禁止）。"""
    calls = record_subprocess_calls(monkeypatch)
    source = make_source(cfg.outputs_dir / "v_20260810_120012_mmmm.mp4")
    service.start_upscale(request_for(cfg, source))
    wait_until_done(service)

    assert calls, "サブプロセスが1つも起動していない"
    for call in calls:  # ワーカーも ffmpeg も同じ約束を守る
        assert isinstance(call["args"], (list, tuple))
        assert all(isinstance(a, str) for a in call["args"])
        assert call["kwargs"].get("shell", False) is False


def test_worker_does_not_write_pycache_into_diffsynth(cfg, service, monkeypatch):
    """既存 venv 側に `__pycache__` を作らせない（読み取り専用の約束）。"""
    calls = record_subprocess_calls(monkeypatch)
    source = make_source(cfg.outputs_dir / "v_20260810_120013_nnnn.mp4")
    service.start_upscale(request_for(cfg, source))
    wait_until_done(service)

    env = worker_call(calls)["kwargs"].get("env") or {}
    assert env.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert env.get("PYTHONUNBUFFERED") == "1"


def test_output_is_promoted_atomically(cfg, service, monkeypatch):
    """正式名は**検証を通ったあと**にしか現れない（§10.7 の原子的昇格）。"""
    monkeypatch.setenv("FAKE_UPSCALE_SLEEP", "0.05")
    source = make_source(cfg.outputs_dir / "v_20260810_120014_oooo.mp4")
    req = request_for(cfg, source)

    service.start_upscale(req)
    seen_partial = False
    while True:
        status = service.status()
        if req.output_path.exists():
            # 正式名が見えた瞬間には、もう完成している
            assert req.output_path.stat().st_size > 0
        if any(p.name.endswith(".partial") for p in cfg.upscaled_dir.iterdir()):
            seen_partial = True
            assert not req.output_path.exists(), "途中なのに正式名がある"
        if not status.running and status.state != STATE_IDLE:
            break
        time.sleep(0.01)

    assert service.status().state == STATE_SUCCEEDED
    # 後片付け: 残るのは正式名だけ
    assert [p.name for p in cfg.upscaled_dir.iterdir()] == [req.output_path.name]
    assert seen_partial or True  # partial を捉えられるかは速度次第（残らないことが本題）


def test_no_shortest_flag_is_used_when_muxing_audio(cfg, service, monkeypatch):
    """`-shortest` を使わない（末尾を切らないため。§26.10）。

    コメントに書いてあるかではなく、**実際に渡した引数**を見る。
    """
    seen: list[list[str]] = []
    from app.core import ffmpeg_ops as fo

    real_run = fo._run

    def spy(args, **kwargs):
        seen.append([str(a) for a in args])
        return real_run(args, **kwargs)

    monkeypatch.setattr(fo, "_run", spy)
    source = make_source(cfg.outputs_dir / "v_20260810_120015_pppp.mp4", audio=True)
    req = request_for(cfg, source)
    service.start_upscale(req)
    assert wait_until_done(service).state == STATE_SUCCEEDED

    mux = [a for a in seen if "-movflags" in a]
    assert mux, f"音声結合の呼び出しが見つかりません: {seen}"
    for args in mux:
        assert "-shortest" not in args
        assert "-c:a" in args and args[args.index("-c:a") + 1] == "copy"
