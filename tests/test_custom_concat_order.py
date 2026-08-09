"""指定順連結の**機械的な順序証明**（P5.2・設計書 §23.7）。

「渡した順で ffmpeg を呼んだ」ことは fake runner で確かめられるが、それだけでは
**出来上がった動画が本当にその順番か**は分からない。ここでは実 ffmpeg で
色と音が識別できる素材を作り、完成した1本の動画から

  - 各区間の**中央フレームの色**（赤／緑／青）
  - 同じ区間の**音声の主要周波数**（440 / 880 / 1320 Hz）

を取り出して、映像と音声の**両方**が指定順どおりであることを確認する。
片方だけを見ると A/V が入れ違っていても気付けないので、必ず両方を見る。

実モデル（H3）は使わない。書き込み先は `tmp_path` のみ。
"""

from __future__ import annotations

import dataclasses
import math
import struct
import subprocess
import wave
from datetime import datetime
from pathlib import Path

import pytest

from app.core import ffmpeg_ops as fo
from app.core.concat_manifest import ConcatManifest
from app.core.concat_service import STATE_DONE, ConcatService
from app.core.config import load_config
from app.core.contracts import BackendIdentity, JobSpec
from app.core.history import HistoryRecord, HistoryStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FPS = 24
NUM_FRAMES = 24  # 1秒。素材が短いほど試験が速い
SAMPLE_RATE = 32000

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax-H3-NF4",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)
T0 = datetime(2026, 8, 9, 21, 0, 0).astimezone()

#: 素材の識別子。色（RGB の代表値）と音の周波数を1対1で対応させる
MARKERS = {
    "A": {"id": "v_20260809_210000_maaa", "color": "red", "rgb": (255, 0, 0), "hz": 440},
    "B": {"id": "v_20260809_210001_mbbb", "color": "lime", "rgb": (0, 255, 0), "hz": 880},
    "C": {"id": "v_20260809_210002_mccc", "color": "blue", "rgb": (0, 0, 255), "hz": 1320},
}


@pytest.fixture(scope="module")
def ffmpeg() -> str:
    return fo.resolve_ffmpeg("")


@pytest.fixture()
def cfg(tmp_path: Path):
    base = load_config(PROJECT_ROOT)
    return dataclasses.replace(
        base, data_root=tmp_path / "data", dedupe_boundary_frame=False
    )


@pytest.fixture()
def env(cfg, ffmpeg):
    """色と音で識別できる素材3本を実 ffmpeg で作り、履歴へ SUCCESS で登録する。"""
    cfg.outputs_dir.mkdir(parents=True)
    cfg.concat_dir.mkdir()
    cfg.tmp_dir.mkdir()
    history = HistoryStore(cfg.history_path, cfg.data_root)
    history.load()
    manifest = ConcatManifest(cfg.concat_manifest_path, cfg.data_root)
    manifest.load()

    for marker in MARKERS.values():
        make_marker_clip(
            ffmpeg,
            cfg.outputs_dir / f"{marker['id']}.mp4",
            color=marker["color"],
            hz=marker["hz"],
        )
        register_success(cfg, history, marker["id"])

    service = ConcatService(cfg, history, ffmpeg_path="", manifest=manifest)
    return cfg, history, manifest, service, ffmpeg


def make_marker_clip(ffmpeg: str, out_path: Path, *, color: str, hz: int) -> Path:
    """単色映像＋単一周波数の音声を持つ MP4（本アプリの成果物と同じ形式）。"""
    duration = NUM_FRAMES / FPS
    args = [
        ffmpeg, "-y", "-nostdin",
        "-f", "lavfi",
        "-i", f"color=c={color}:size=576x320:rate={FPS}:duration={duration:.6f}",
        "-f", "lavfi",
        "-i", f"sine=frequency={hz}:sample_rate={SAMPLE_RATE}:duration={duration:.6f}",
        "-frames:v", str(NUM_FRAMES),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", str(SAMPLE_RATE), "-ac", "2",
        "-f", "mp4", str(out_path),
    ]
    subprocess.run(args, check=True, capture_output=True, timeout=120)
    return out_path


def register_success(cfg, history: HistoryStore, job_id: str) -> None:
    out = cfg.outputs_dir / f"{job_id}.mp4"
    last = cfg.outputs_dir / f"{job_id}_last.png"
    last.write_bytes(b"\x89PNG\r\n\x1a\n")  # 連結では使わない（存在だけ持たせる）
    spec = JobSpec(
        job_id=job_id,
        prompt=f"marker {job_id}",
        num_frames=NUM_FRAMES,
        steps=4,
        seed_requested=None,
        output_path=out,
        last_frame_path=last,
        job_type="single",
        parent_id=None,
        keyframe_path=None,
    )
    history.add(
        HistoryRecord.from_job_spec(
            spec,
            identity=IDENTITY,
            execution_engine="mock",
            app_version=cfg.version,
            data_root=cfg.data_root,
            created_at=T0,
        )
    )
    history.mark_running(job_id, T0)
    history.mark_success(
        job_id,
        output_path=out,
        last_frame_path=last,
        seed_used=42,
        elapsed_sec=1.0,
        finished_at=T0,
    )


# ------------------------------------------------------------ 取り出し


def dominant_color(ffmpeg: str, video: Path, frame_index: int, tmp: Path) -> tuple[int, int, int]:
    """指定フレームの平均色（R, G, B）。"""
    from PIL import Image

    png = tmp / f"probe_{frame_index}.png"
    fo.extract_frame_exact(ffmpeg, video, frame_index, png)
    with Image.open(png) as img:
        rgb = img.convert("RGB").resize((1, 1), Image.BOX)
        return rgb.getpixel((0, 0))


def dominant_frequency(
    ffmpeg: str, video: Path, start_sec: float, duration_sec: float, tmp: Path
) -> float:
    """指定区間の音声から、いちばん強い周波数を求める（外部ライブラリ不要のDFT）。"""
    wav = tmp / f"probe_{start_sec:.3f}.wav"
    subprocess.run(
        [
            ffmpeg, "-y", "-nostdin",
            "-ss", f"{start_sec:.6f}", "-t", f"{duration_sec:.6f}",
            "-i", str(video),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-f", "wav", str(wav),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    with wave.open(str(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    if not samples:
        raise AssertionError(f"音声を取り出せませんでした: {video.name} @ {start_sec}s")

    # 候補周波数ごとの相関（Goertzel 相当）。候補は素材に使った3つだけなので十分速い
    candidates = [m["hz"] for m in MARKERS.values()]
    n = min(len(samples), rate // 4)  # 0.25秒ぶんで判定する
    best_hz, best_power = 0.0, -1.0
    for hz in candidates:
        real = sum(s * math.cos(2 * math.pi * hz * i / rate) for i, s in enumerate(samples[:n]))
        imag = sum(s * math.sin(2 * math.pi * hz * i / rate) for i, s in enumerate(samples[:n]))
        power = real * real + imag * imag
        if power > best_power:
            best_hz, best_power = float(hz), power
    return best_hz


def wait_done(service: ConcatService, timeout: float = 180.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.status()
        if status.state in (STATE_DONE, "failed"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"連結が終わりませんでした: {service.status()}")


def assert_segments_match(env, output: Path, expected_keys: list[str]) -> None:
    """完成動画の各区間が、指定した素材の**色と音**に一致することを確かめる。"""
    cfg, _history, _manifest, _service, ffmpeg = env
    for position, key in enumerate(expected_keys):
        marker = MARKERS[key]
        # 区間の中央フレーム（境界の1フレームずれに影響されない位置）
        middle_frame = position * NUM_FRAMES + NUM_FRAMES // 2
        red, green, blue = dominant_color(ffmpeg, output, middle_frame, cfg.tmp_dir)
        expected_rgb = marker["rgb"]
        assert _closest_marker((red, green, blue)) == key, (
            f"{position + 1}番目の映像が違います"
            f"（実測 RGB={red},{green},{blue} / 期待 {key}={expected_rgb}）"
        )

        start = position * (NUM_FRAMES / FPS) + 0.2  # 境界を避けて区間の内側を見る
        hz = dominant_frequency(ffmpeg, output, start, 0.4, cfg.tmp_dir)
        assert hz == pytest.approx(marker["hz"], abs=1.0), (
            f"{position + 1}番目の音声が違います"
            f"（実測 {hz:.0f}Hz / 期待 {key}={marker['hz']}Hz）"
        )


def _closest_marker(rgb: tuple[int, int, int]) -> str:
    """実測 RGB にいちばん近いマーカー（H.264 の往復で数値がぶれるため）。"""
    def distance(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))

    return min(MARKERS, key=lambda k: distance(rgb, MARKERS[k]["rgb"]))


# ================================================================ 本体


def test_markers_are_distinguishable_before_concat(env):
    """前提の確認: 素材そのものが色でも音でも区別できる。"""
    cfg, _history, _manifest, _service, ffmpeg = env
    for key, marker in MARKERS.items():
        video = cfg.outputs_dir / f"{marker['id']}.mp4"
        assert _closest_marker(dominant_color(ffmpeg, video, 12, cfg.tmp_dir)) == key
        hz = dominant_frequency(ffmpeg, video, 0.2, 0.4, cfg.tmp_dir)
        assert hz == pytest.approx(marker["hz"], abs=1.0)


def test_custom_concat_output_follows_the_requested_order(env):
    """**B → C → A** を指定したら、出来上がりも B → C → A になる。"""
    _cfg, _history, manifest, service, _ffmpeg = env
    order = ["B", "C", "A"]
    service.start_custom_concat([MARKERS[k]["id"] for k in order])
    status = wait_done(service)

    assert status.state == STATE_DONE, status.message
    assert_segments_match(env, status.output_path, order)
    assert manifest.list_entries()[0].sources == tuple(MARKERS[k]["id"] for k in order)


def test_a_different_order_produces_a_different_video(env):
    """順番を変えれば中身も変わる（試験が順番を本当に見ている証明）。"""
    _cfg, _history, _manifest, service, _ffmpeg = env
    service.start_custom_concat([MARKERS[k]["id"] for k in ["C", "A", "B"]])
    status = wait_done(service)

    assert status.state == STATE_DONE, status.message
    assert_segments_match(env, status.output_path, ["C", "A", "B"])
    # 別の順番として解釈すると必ず落ちる（＝素通りしない試験である）
    with pytest.raises(AssertionError):
        assert_segments_match(env, status.output_path, ["A", "B", "C"])


def test_audio_and_video_stay_aligned_per_segment(env):
    """A/V の入れ違いを検出する: 各区間で色と音の組が**同じ素材**を指す。"""
    cfg, _history, _manifest, service, ffmpeg = env
    order = ["A", "C", "B"]
    service.start_custom_concat([MARKERS[k]["id"] for k in order])
    status = wait_done(service)
    assert status.state == STATE_DONE, status.message

    for position, key in enumerate(order):
        rgb = dominant_color(
            ffmpeg, status.output_path, position * NUM_FRAMES + NUM_FRAMES // 2, cfg.tmp_dir
        )
        hz = dominant_frequency(
            ffmpeg, status.output_path, position * (NUM_FRAMES / FPS) + 0.2, 0.4, cfg.tmp_dir
        )
        from_video = _closest_marker(rgb)
        from_audio = next(k for k, m in MARKERS.items() if m["hz"] == pytest.approx(hz, abs=1.0))
        assert from_video == from_audio == key, (
            f"{position + 1}番目で映像と音声が食い違っています"
            f"（映像={from_video} / 音声={from_audio} / 期待={key}）"
        )


def test_two_clip_order_is_preserved_both_ways(env):
    """2本でも順番が保たれる（A→B と B→A が別物になる）。"""
    _cfg, _history, _manifest, service, _ffmpeg = env
    service.start_custom_concat([MARKERS["A"]["id"], MARKERS["B"]["id"]])
    first = wait_done(service)
    assert first.state == STATE_DONE, first.message
    assert_segments_match(env, first.output_path, ["A", "B"])

    service.start_custom_concat([MARKERS["B"]["id"], MARKERS["A"]["id"]])
    second = wait_done(service)
    assert second.state == STATE_DONE, second.message
    assert_segments_match(env, second.output_path, ["B", "A"])

    assert first.output_path != second.output_path  # 別ファイルとして残る
    assert first.output_path.is_file() and second.output_path.is_file()


def test_output_is_h264_yuv420p_with_aac_at_24fps(env):
    """成果物契約（§22.5）を満たす: H.264/yuv420p・AAC・24fps・576×320。"""
    _cfg, _history, _manifest, service, ffmpeg = env
    service.start_custom_concat([MARKERS[k]["id"] for k in ["A", "B", "C"]])
    status = wait_done(service)
    assert status.state == STATE_DONE, status.message

    probe = fo.decode_probe(ffmpeg, status.output_path)
    assert probe.has_video and probe.has_audio

    # フレーム数は「合計以上・合計＋境界数比例の許容まで」（設計書 §10.6.2）。
    # AAC は 1024 サンプル単位でしか書けないため、境界ごとに最大1フレーム弱の
    # 隙間が入る。**チェーン連結と同じ式**で判定する（指定順連結でも条件は同じ）。
    expected = NUM_FRAMES * 3
    allowance = fo.concat_max_extra_frames(3, FPS, SAMPLE_RATE)
    assert expected <= probe.frames <= expected + allowance, (
        f"フレーム数が許容外です（実測 {probe.frames} / 想定 {expected}＋許容 {allowance}）"
    )
    tolerance = fo.concat_duration_tolerance_sec(3, FPS, SAMPLE_RATE)
    assert probe.duration_sec == pytest.approx(expected / FPS, abs=tolerance)
    assert "h264" in probe.video_desc and "yuv420p" in probe.video_desc
    assert "576x320" in probe.video_desc
    assert "24 fps" in probe.video_desc
    assert "aac" in probe.audio_desc and f"{SAMPLE_RATE} Hz" in probe.audio_desc


def test_sources_are_not_modified_by_the_concat(env):
    """元動画のサイズ・更新時刻が変わらない。"""
    cfg, _history, _manifest, service, _ffmpeg = env
    sources = [cfg.outputs_dir / f"{m['id']}.mp4" for m in MARKERS.values()]
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sources}

    service.start_custom_concat([MARKERS[k]["id"] for k in ["C", "B", "A"]])
    assert wait_done(service).state == STATE_DONE

    for path, (size, mtime_ns) in before.items():
        assert path.stat().st_size == size
        assert path.stat().st_mtime_ns == mtime_ns
