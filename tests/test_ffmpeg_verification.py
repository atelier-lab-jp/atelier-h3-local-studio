"""P0 の FFmpeg 検証（設計書 §18 P0 検証項目①）。

imageio-ffmpeg 同梱バイナリで以下を実機確認する:
 1. バイナリの解決
 2. -version
 3. 576×320・24fps・H.264/yuv420p・AAC のモック動画生成
 4. 56フレーム相当と124フレーム相当の生成
 5. index 55 / 123 の PNG 抽出（select フィルタ）
 6. PNG が画像として開けること
 7. 2本以上の動画を PTSリセット＋再エンコードで連結
 8. 連結動画の映像と音声が読み取れること
 9. duration が入力合計とおおむね一致すること
10. 元動画のサイズ・mtime が変わっていないこと
補助: -c copy 連結（合格条件には含めない）
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.core import ffmpeg_ops as fo
from app.core.fileops import FileopsError

FPS = 24


@pytest.fixture(scope="module")
def ffmpeg():
    return fo.resolve_ffmpeg("")


@pytest.fixture(scope="module")
def clips(ffmpeg, tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    c56 = d / "clip_56.mp4"
    c124 = d / "clip_124.mp4"
    fo.make_mock_clip(ffmpeg, c56, 56)
    fo.make_mock_clip(ffmpeg, c124, 124)
    return c56, c124


@pytest.fixture(scope="module")
def concat_result(ffmpeg, clips, tmp_path_factory):
    c56, c124 = clips
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in clips}
    out = tmp_path_factory.mktemp("concat") / "chain.mp4"
    expected = (56 + 124) / FPS
    fo.concat_reencode(
        ffmpeg, [c56, c124], out, fps=FPS, expected_duration_sec=expected
    )
    return {"out": out, "before": before, "expected": expected}


def test_01_resolve_binary(ffmpeg):
    from pathlib import Path

    assert Path(ffmpeg).is_file()


def test_02_version(ffmpeg):
    assert "ffmpeg version" in fo.ffmpeg_version(ffmpeg)


def test_03_04_clip_specs(ffmpeg, clips):
    for clip, frames in zip(clips, (56, 124)):
        probe = fo.decode_probe(ffmpeg, clip)
        assert probe.has_video and probe.has_audio
        assert "h264" in probe.video_desc.lower()
        assert "yuv420p" in probe.video_desc
        assert "576x320" in probe.video_desc
        assert "aac" in probe.audio_desc.lower()
        assert probe.frames == frames
        assert probe.duration_sec == pytest.approx(frames / FPS, abs=0.5)


def test_05_06_extract_exact_index(ffmpeg, clips, tmp_path):
    for clip, frames in zip(clips, (56, 124)):
        png = tmp_path / f"last_{frames}.png"
        fo.extract_frame_exact(ffmpeg, clip, frames - 1, png)
        assert png.is_file() and png.stat().st_size > 0
        with Image.open(png) as img:
            img.load()
            assert img.size == (576, 320)


def test_05b_extract_beyond_last_frame_fails(ffmpeg, clips, tmp_path):
    """存在しないフレーム番号の指定は検証段階で弾かれる（正確なindexの裏取り）。"""
    c56, _ = clips
    png = tmp_path / "beyond.png"
    with pytest.raises(Exception):
        fo.extract_frame_exact(ffmpeg, c56, 56, png)  # 0始まりなので 56 は存在しない
    assert not png.exists()


def test_07_concat_reencode_creates_output(concat_result):
    out = concat_result["out"]
    assert out.is_file() and out.stat().st_size > 0
    assert not out.with_name(out.name + ".partial").exists()


def test_08_09_concat_decodes_and_duration(ffmpeg, concat_result):
    probe = fo.decode_probe(ffmpeg, concat_result["out"])
    assert probe.has_video and probe.has_audio
    assert "h264" in probe.video_desc.lower()
    assert "yuv420p" in probe.video_desc
    assert probe.duration_sec == pytest.approx(concat_result["expected"], abs=0.5)
    assert probe.frames == 56 + 124


def test_10_sources_untouched(concat_result):
    for path, (size, mtime_ns) in concat_result["before"].items():
        assert path.stat().st_size == size
        assert path.stat().st_mtime_ns == mtime_ns


@pytest.mark.xfail(reason="補助確認: -c copy は実験モードでありP0合格条件外", strict=False)
def test_aux_concat_copy(ffmpeg, clips, tmp_path):
    c56, c124 = clips
    out = tmp_path / "copy_chain.mp4"
    fo.concat_copy(ffmpeg, [c56, c124], out, expected_duration_sec=(56 + 124) / FPS)
    assert out.is_file() and out.stat().st_size > 0


# =========================================================== P4: 重複1フレーム
# 設計書 §10.6.1 / P4契約 §5。**必ず比較してから除去する**ことを担保する。


def stream_times(ffmpeg: str, path: Path) -> dict[str, float]:
    """映像・音声それぞれの duration を測る（imageio-ffmpeg には ffprobe が無い）。"""
    result: dict[str, float] = {}
    for kind, sel in (("video", "v"), ("audio", "a")):
        proc = subprocess.run(
            [ffmpeg, "-nostdin", "-i", str(path), "-map", f"0:{sel}", "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        times = re.findall(r"time=(\d+):(\d+):([\d.]+)", proc.stderr)
        assert times, f"{kind} の duration を取得できません: {path.name}"
        h, m, s = times[-1]
        result[kind] = int(h) * 3600 + int(m) * 60 + float(s)
    return result


@pytest.fixture(scope="module")
def dup_clips(ffmpeg, tmp_path_factory):
    """同一素材のコピー2本（＝境界フレームが完全一致する最良ケース）。"""
    d = tmp_path_factory.mktemp("dup")
    a = d / "a.mp4"
    fo.make_mock_clip(ffmpeg, a, 56)
    b = d / "b.mp4"
    shutil.copy(a, b)
    c = d / "c.mp4"
    shutil.copy(a, c)
    return a, b, c


@pytest.fixture(scope="module")
def boundary_frames(ffmpeg, dup_clips, tmp_path_factory):
    """親の最終フレーム / 子の先頭フレーム / 別コピーの先頭フレーム。"""
    a, b, _ = dup_clips
    d = tmp_path_factory.mktemp("frames")
    a_last = d / "a_last.png"
    a_first = d / "a_first.png"
    b_first = d / "b_first.png"
    fo.extract_frame_exact(ffmpeg, a, 55, a_last)
    fo.extract_frame_exact(ffmpeg, a, 0, a_first)
    fo.extract_frame_exact(ffmpeg, b, 0, b_first)
    return {"a_last": a_last, "a_first": a_first, "b_first": b_first, "dir": d}


# ---------------------------------------------------------------- compare_frames


def test_compare_frames_identical(boundary_frames):
    """同じ素材の同じフレーム同士は完全一致（平均差0・最大差0）。"""
    diff = fo.compare_frames(boundary_frames["a_first"], boundary_frames["b_first"])
    assert diff.same_size is True
    assert diff.mean_diff == 0.0
    assert diff.max_diff == 0.0
    assert diff.identical is True
    assert diff.matches(0.0, 0.0) is True
    assert diff.matches(1.0, 16.0) is True


def test_compare_frames_different_content(boundary_frames):
    """別の絵は既定閾値（平均1.0 / 最大16.0）で必ず「非一致」。"""
    diff = fo.compare_frames(boundary_frames["a_last"], boundary_frames["b_first"])
    assert diff.same_size is True
    assert diff.identical is False
    assert diff.mean_diff > 1.0
    assert diff.max_diff > 16.0
    assert diff.matches(1.0, 16.0) is False


def test_compare_frames_size_mismatch_is_never_a_match(boundary_frames, tmp_path):
    """サイズ違いは即「非一致」。どんなに緩い閾値でも一致にしない（安全側）。"""
    small = tmp_path / "small.png"
    with Image.open(boundary_frames["a_first"]) as img:
        img.convert("RGB").resize((288, 160)).save(small, format="PNG")

    diff = fo.compare_frames(boundary_frames["a_first"], small)
    assert diff.same_size is False
    assert diff.identical is False
    assert diff.size_a == (576, 320) and diff.size_b == (288, 160)
    assert diff.matches(1e9, 1e9) is False
    assert "サイズ不一致" in diff.describe()


def test_compare_frames_symmetric(boundary_frames):
    a = fo.compare_frames(boundary_frames["a_last"], boundary_frames["b_first"])
    b = fo.compare_frames(boundary_frames["b_first"], boundary_frames["a_last"])
    assert a.mean_diff == pytest.approx(b.mean_diff)
    assert a.max_diff == b.max_diff


def test_compare_frames_rejects_missing_and_broken(boundary_frames, tmp_path):
    with pytest.raises(fo.FfmpegError, match="見つかりません"):
        fo.compare_frames(tmp_path / "nope.png", boundary_frames["a_first"])
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")
    with pytest.raises(fo.FfmpegError, match="画像として開けません"):
        fo.compare_frames(broken, boundary_frames["a_first"])


# ---------------------------------------------------------------- filter 生成


def _capture_args(monkeypatch) -> dict:
    seen: dict = {}

    def fake_run(args, timeout=300):
        seen["args"] = list(args)
        raise fo.FfmpegError("停止（テスト用）")

    monkeypatch.setattr(fo, "_run", fake_run)
    return seen


def _filter_of(args: list[str]) -> str:
    return args[args.index("-filter_complex") + 1]


def test_concat_without_trim_keeps_original_filter(dup_clips, tmp_path, monkeypatch):
    """既定（trim なし）のフィルタは P0 から変えない（回帰防止）。"""
    a, b, _ = dup_clips
    seen = _capture_args(monkeypatch)
    with pytest.raises(fo.FfmpegError):
        fo.concat_reencode(fo.resolve_ffmpeg(""), [a, b], tmp_path / "o.mp4")
    fc = _filter_of(seen["args"])
    assert fc == (
        "[0:v]setpts=PTS-STARTPTS[v0];[0:a]asetpts=PTS-STARTPTS[a0];"
        "[1:v]setpts=PTS-STARTPTS[v1];[1:a]asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )


def test_concat_trim_touches_video_only(dup_clips, tmp_path, monkeypatch):
    """既定では音声を切らない（語頭欠けを避ける。§10.6.1-4）。"""
    a, b, c = dup_clips
    seen = _capture_args(monkeypatch)
    with pytest.raises(fo.FfmpegError):
        fo.concat_reencode(
            fo.resolve_ffmpeg(""),
            [a, b, c],
            tmp_path / "o.mp4",
            trim_first_frame_of={1, 2},
        )
    fc = _filter_of(seen["args"])
    assert "[0:v]setpts=PTS-STARTPTS[v0]" in fc  # 先頭は削らない
    assert "[1:v]trim=start_frame=1,setpts=PTS-STARTPTS[v1]" in fc
    assert "[2:v]trim=start_frame=1,setpts=PTS-STARTPTS[v2]" in fc
    assert "atrim" not in fc  # 音声は切らない


def test_concat_trim_audio_option_adds_atrim(dup_clips, tmp_path, monkeypatch):
    a, b, _ = dup_clips
    seen = _capture_args(monkeypatch)
    with pytest.raises(fo.FfmpegError):
        fo.concat_reencode(
            fo.resolve_ffmpeg(""),
            [a, b],
            tmp_path / "o.mp4",
            trim_first_frame_of={1},
            trim_audio_with_video=True,
        )
    fc = _filter_of(seen["args"])
    assert "[1:a]atrim=start=0.041667,asetpts=PTS-STARTPTS[a1]" in fc


def test_concat_trim_index_out_of_range(ffmpeg, dup_clips, tmp_path):
    a, b, _ = dup_clips
    for bad in ({2}, {-1}, {5}):
        with pytest.raises(fo.FfmpegError, match="範囲外"):
            fo.concat_reencode(
                ffmpeg, [a, b], tmp_path / "o.mp4", trim_first_frame_of=bad
            )
    assert not (tmp_path / "o.mp4").exists()


# ---------------------------------------------------------------- 実測: 除去の効果


@pytest.fixture(scope="module")
def dedupe_measurements(ffmpeg, dup_clips, tmp_path_factory):
    """OFF / 映像のみ除去 / 映像＋音声除去 を同一素材で実測する。"""
    a, b, c = dup_clips
    d = tmp_path_factory.mktemp("dedupe")
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (a, b, c)}

    results: dict[str, dict] = {}
    cases = {
        "off": ([a, b], None, False),
        "video_only": ([a, b], {1}, False),
        "video_and_audio": ([a, b], {1}, True),
        "chain3_off": ([a, b, c], None, False),
        "chain3_video_only": ([a, b, c], {1, 2}, False),
    }
    for name, (inputs, trim, trim_audio) in cases.items():
        out = d / f"{name}.mp4"
        warnings: list[str] = []
        fo.concat_reencode(
            ffmpeg,
            inputs,
            out,
            fps=FPS,
            trim_first_frame_of=trim,
            trim_audio_with_video=trim_audio,
            warnings_out=warnings,
        )
        probe = fo.decode_probe(ffmpeg, out)
        results[name] = {
            "path": out,
            "frames": probe.frames,
            "duration": probe.duration_sec,
            "streams": stream_times(ffmpeg, out),
            "warnings": warnings,
        }
    results["_before"] = before
    return results


def test_dedupe_off_keeps_every_frame(dedupe_measurements):
    off = dedupe_measurements["off"]
    assert off["frames"] == 112  # 56 + 56（除去なし）
    assert off["warnings"] == []
    assert off["duration"] == pytest.approx(112 / FPS, abs=0.5)


def test_dedupe_video_only_removes_one_frame_per_boundary(dedupe_measurements):
    trimmed = dedupe_measurements["video_only"]
    assert trimmed["frames"] == 111  # 境界1箇所ぶん減る
    assert dedupe_measurements["chain3_off"]["frames"] == 168
    assert dedupe_measurements["chain3_video_only"]["frames"] == 166  # 境界2箇所


def test_dedupe_reports_boundaries_and_count(dedupe_measurements):
    warnings = dedupe_measurements["chain3_video_only"]["warnings"]
    assert len(warnings) == 1
    assert "2 箇所" in warnings[0]
    assert "除去 2 フレーム" in warnings[0]
    assert "映像のみ" in warnings[0]


def test_dedupe_duration_change_matches_frame_count(dedupe_measurements):
    """映像 duration がちょうど 1フレーム（41.7ms）ぶん短くなる。"""
    off = dedupe_measurements["off"]["streams"]
    trimmed = dedupe_measurements["video_only"]["streams"]
    delta = off["video"] - trimmed["video"]
    assert delta == pytest.approx(1 / FPS, abs=0.01)
    # 音声は切っていないので長さは変わらない
    assert trimmed["audio"] == pytest.approx(off["audio"], abs=0.01)


def test_dedupe_av_sync_is_measured(dedupe_measurements):
    """A/V 同期: 映像だけ削ると音声との差が 1フレームぶん広がる（数値で確認）。"""
    off = dedupe_measurements["off"]["streams"]
    video_only = dedupe_measurements["video_only"]["streams"]
    with_audio = dedupe_measurements["video_and_audio"]["streams"]

    skew_off = off["video"] - off["audio"]
    skew_video_only = video_only["video"] - video_only["audio"]
    skew_with_audio = with_audio["video"] - with_audio["audio"]

    # 映像のみ除去: 基準からちょうど1フレームぶんずれる
    assert (skew_off - skew_video_only) == pytest.approx(1 / FPS, abs=0.01)
    # ずれの絶対値そのものは1フレーム（約41.7ms）以内に収まる
    assert abs(skew_video_only) <= 1 / FPS + 0.01
    # 音声も同じだけ切ると基準と同じ同期に戻る（ただし語頭 41.7ms を失う）
    assert skew_with_audio == pytest.approx(skew_off, abs=0.005)


def test_dedupe_output_is_still_h264_yuv420p_aac(ffmpeg, dedupe_measurements):
    probe = fo.decode_probe(ffmpeg, dedupe_measurements["video_only"]["path"])
    assert probe.has_video and probe.has_audio
    assert "h264" in probe.video_desc.lower()
    assert "yuv420p" in probe.video_desc
    assert "576x320" in probe.video_desc
    assert "aac" in probe.audio_desc.lower()
    assert "32000 Hz" in probe.audio_desc


def test_dedupe_expected_duration_accounts_for_trim(ffmpeg, dup_clips, tmp_path):
    """除去ぶんを引いた expected_duration_sec で検証が通る。"""
    a, b, _ = dup_clips
    out = tmp_path / "trimmed.mp4"
    fo.concat_reencode(
        ffmpeg,
        [a, b],
        out,
        fps=FPS,
        expected_duration_sec=(112 - 1) / FPS,
        trim_first_frame_of={1},
    )
    assert out.is_file()
    assert not out.with_name(out.name + ".partial").exists()


def test_dedupe_sources_untouched(dedupe_measurements):
    """除去つき連結でも入力ファイルは一切変更されない。"""
    for path, (size, mtime_ns) in dedupe_measurements["_before"].items():
        assert path.stat().st_size == size
        assert path.stat().st_mtime_ns == mtime_ns


# ===================================================== P5: 長チェーンの連結検証
# 設計書 §10.6・P5契約 §5.2。
#
# 許容式の根拠となる実測（本ファイルの test_p5_* が同じ素材で再現する）:
#
#   素材    n   期待frame  実測frame  期待秒     実測秒     誤差
#   56f     2      112       112       4.6667    4.6400    -0.027
#   56f     5      280       280      11.6667   11.6400    -0.027
#   56f    10      560       560      23.3333   23.3200    -0.013
#   56f    20     1120      1120      46.6667   46.6500    -0.017
#   124f    2      248       248      10.3333   10.3300    -0.003
#   124f    5      620       621      25.8333   25.8700    +0.037
#   124f   10     1240      1243      51.6667   51.7900    +0.123
#   124f   20     2480      2487     103.3333  103.6200    +0.287
#
# フレームが増えるのは、AAC が 1024 サンプル単位でしか書けず各クリップの音声が
# 映像より最大 32ms 長くなるため（concat フィルタは max(映像,音声) だけ進む）。
# よって誤差は本数ではなく**境界数 (n-1) に比例**する。


SAMPLE_RATE = 32000


def test_p5_audio_quantum_is_one_aac_frame():
    """1024 サンプル ÷ 32000Hz = 32ms（許容式の per-boundary 項の正体）。"""
    assert fo.AAC_FRAME_SAMPLES == 1024
    assert fo.audio_quantum_sec(SAMPLE_RATE) == pytest.approx(0.032)
    assert fo.audio_quantum_sec(48000) == pytest.approx(1024 / 48000)


def test_p5_audio_quantum_rejects_bad_sample_rate():
    for bad in (0, -1):
        with pytest.raises(fo.FfmpegError):
            fo.audio_quantum_sec(bad)


@pytest.mark.parametrize(
    ("clips", "expected"),
    [(1, 0.5), (2, 0.532), (5, 0.628), (10, 0.788), (20, 1.108)],
)
def test_p5_duration_tolerance_grows_with_boundaries(clips, expected):
    """固定値は 0.5 のまま。増えるのは (n-1)×0.032 の項だけ。"""
    tol = fo.concat_duration_tolerance_sec(clips, FPS, SAMPLE_RATE)
    assert tol == pytest.approx(expected, abs=1e-9)


def test_p5_duration_tolerance_is_linear_in_boundaries():
    quantum = fo.audio_quantum_sec(SAMPLE_RATE)
    for n in range(2, 25):
        prev = fo.concat_duration_tolerance_sec(n - 1, FPS, SAMPLE_RATE)
        cur = fo.concat_duration_tolerance_sec(n, FPS, SAMPLE_RATE)
        assert cur - prev == pytest.approx(quantum)


@pytest.mark.parametrize(
    ("clips", "expected"), [(1, 0), (2, 1), (5, 4), (10, 7), (20, 15)]
)
def test_p5_max_extra_frames(clips, expected):
    """境界ごとに最大1フレーム弱（0.768フレーム）。20本で15フレームが上限。"""
    assert fo.concat_max_extra_frames(clips, FPS, SAMPLE_RATE) == expected


def test_p5_measured_errors_fit_the_formula():
    """実測誤差が許容式に収まり、かつ 2倍以上の余裕があること（表の数値そのもの）。"""
    measured = {2: 0.0267, 5: 0.0367, 10: 0.1233, 20: 0.2867}
    for clips, err in measured.items():
        tol = fo.concat_duration_tolerance_sec(clips, FPS, SAMPLE_RATE)
        assert err <= tol, clips
        assert tol / err >= 2.0, (clips, tol, err)


def test_p5_measured_extra_frames_fit_the_formula():
    """実測の増加フレーム数（124f素材）が上限に収まること。"""
    measured = {2: 0, 5: 1, 10: 3, 20: 7}
    for clips, extra in measured.items():
        assert extra <= fo.concat_max_extra_frames(clips, FPS, SAMPLE_RATE)


def test_p5_p4_real_measurement_fits(ffmpeg, tmp_path):
    """P4 実機2本（248フレーム・10.33秒・A/V skew +0.030秒）が検証を通ること。"""
    validate = fo.concat_validator(
        ffmpeg, clips=2, expected_frames=248, fps=FPS, sample_rate=SAMPLE_RATE
    )
    # 実機で観測された値を直接 ProbeResult として流し込む
    probe = fo.ProbeResult(
        duration_sec=10.33,
        frames=248,
        has_video=True,
        has_audio=True,
        video_desc="Video: h264 (High), yuv420p, 576x320",
        audio_desc="Audio: aac, 32000 Hz, stereo",
    )
    _run_validator_with_probe(validate, probe, tmp_path / "p4.mp4")
    # A/V skew 0.030秒 は1フレーム（41.7ms）未満なので余剰フレームは出ない
    assert 0.030 < 1 / FPS


def _run_validator_with_probe(validate, probe, path: Path):
    """decode_probe を差し替えて検証関数だけを動かす（ffmpeg を実行しない）。"""
    import unittest.mock

    with unittest.mock.patch.object(fo, "decode_probe", return_value=probe):
        validate(path)


def _probe(frames: int, duration: float) -> "fo.ProbeResult":
    return fo.ProbeResult(
        duration_sec=duration,
        frames=frames,
        has_video=True,
        has_audio=True,
        video_desc="Video: h264 (High), yuv420p, 576x320",
        audio_desc="Audio: aac, 32000 Hz, stereo",
    )


def test_p5_validator_rejects_missing_frames(ffmpeg, tmp_path):
    """フレーム数が期待を下回ったら必ず拒否（主検証）。"""
    validate = fo.concat_validator(
        ffmpeg, clips=5, expected_frames=280, fps=FPS, sample_rate=SAMPLE_RATE
    )
    with pytest.raises(FileopsError, match="フレーム数が足りません"):
        _run_validator_with_probe(validate, _probe(279, 279 / FPS), tmp_path / "a.mp4")


def test_p5_validator_rejects_too_many_frames(ffmpeg, tmp_path):
    validate = fo.concat_validator(
        ffmpeg, clips=5, expected_frames=280, fps=FPS, sample_rate=SAMPLE_RATE
    )
    # 5本なら余剰上限は 4 フレーム
    _run_validator_with_probe(validate, _probe(284, 280 / FPS), tmp_path / "ok.mp4")
    with pytest.raises(FileopsError, match="多すぎます"):
        _run_validator_with_probe(validate, _probe(285, 280 / FPS), tmp_path / "b.mp4")


def test_p5_validator_rejects_90_percent_truncation(ffmpeg, tmp_path):
    """明らかな短縮（期待の90%）は必ず拒否できること（契約 §5.2）。"""
    validate = fo.concat_validator(
        ffmpeg, clips=2, expected_frames=112, fps=FPS, sample_rate=SAMPLE_RATE
    )
    expected_sec = 112 / FPS
    with pytest.raises(FileopsError, match="フレーム数が足りません"):
        _run_validator_with_probe(
            validate, _probe(100, expected_sec * 0.9), tmp_path / "c.mp4"
        )
    # フレーム数が取れなかった場合でも duration の下限で弾ける
    with pytest.raises(FileopsError, match="短すぎます"):
        _run_validator_with_probe(
            validate, _probe(112, expected_sec * 0.9), tmp_path / "d.mp4"
        )


def test_p5_validator_rejects_unreadable_frame_count(ffmpeg, tmp_path):
    validate = fo.concat_validator(
        ffmpeg, clips=2, expected_frames=112, fps=FPS, sample_rate=SAMPLE_RATE
    )
    with pytest.raises(FileopsError, match="フレーム数を確認できません"):
        _run_validator_with_probe(validate, _probe(None, 112 / FPS), tmp_path / "e.mp4")


def test_p5_validator_rejects_duration_far_beyond_tolerance(ffmpeg, tmp_path):
    validate = fo.concat_validator(
        ffmpeg, clips=2, expected_frames=112, fps=FPS, sample_rate=SAMPLE_RATE
    )
    # フレーム数は合っているのに再生時間だけ大きく外れている（壊れたタイムスタンプ）
    with pytest.raises(FileopsError, match="再生時間が想定と一致しません"):
        _run_validator_with_probe(
            validate, _probe(112, 112 / FPS + 5.0), tmp_path / "f.mp4"
        )


def test_p5_validator_requires_both_streams(ffmpeg, tmp_path):
    validate = fo.concat_validator(
        ffmpeg, clips=2, expected_frames=112, fps=FPS, sample_rate=SAMPLE_RATE
    )
    no_video = fo.ProbeResult(112 / FPS, 112, False, True, "", "Audio: aac")
    with pytest.raises(FileopsError, match="映像ストリーム"):
        _run_validator_with_probe(validate, no_video, tmp_path / "g.mp4")
    no_audio = fo.ProbeResult(112 / FPS, 112, True, False, "Video: h264", "")
    with pytest.raises(FileopsError, match="音声ストリーム"):
        _run_validator_with_probe(validate, no_audio, tmp_path / "h.mp4")


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "112"])
def test_p5_validator_rejects_bad_expected_frames(ffmpeg, bad):
    with pytest.raises(fo.FfmpegError, match="expected_frames"):
        fo.concat_validator(
            ffmpeg, clips=2, expected_frames=bad, fps=FPS, sample_rate=SAMPLE_RATE
        )


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "112"])
def test_p5_concat_reencode_rejects_bad_expected_frames(ffmpeg, clips, tmp_path, bad):
    """不正な検証条件は ffmpeg を走らせる前に弾く（無駄な再エンコードをしない）。"""
    c56, c124 = clips
    out = tmp_path / "bad.mp4"
    with pytest.raises(fo.FfmpegError, match="expected_frames"):
        fo.concat_reencode(
            ffmpeg, [c56, c124], out, fps=FPS, expected_frames=bad
        )
    assert not out.exists()
    assert not out.with_name(out.name + ".partial").exists()


# ---------------------------------------------------------------- 実測（長チェーン）


@pytest.fixture(scope="module")
def long_chain_results(ffmpeg, tmp_path_factory):
    """2/5/10/20本のモック連結を実際に作って実測する（tmp_path のみ）。"""
    d = tmp_path_factory.mktemp("longchain")
    src56 = d / "s56.mp4"
    src124 = d / "s124.mp4"
    fo.make_mock_clip(ffmpeg, src56, 56)
    fo.make_mock_clip(ffmpeg, src124, 124)
    before = {
        p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (src56, src124)
    }

    results: dict[tuple[str, int], dict] = {}
    for label, src, frames in (("56f", src56, 56), ("124f", src124, 124)):
        for n in (2, 5, 10, 20):
            out = d / f"chain_{label}_{n}.mp4"
            total = frames * n
            fo.concat_reencode(
                ffmpeg,
                [src] * n,
                out,
                fps=FPS,
                sample_rate=SAMPLE_RATE,
                expected_frames=total,
                timeout=900,
            )
            probe = fo.decode_probe(ffmpeg, out)
            results[(label, n)] = {
                "path": out,
                "expected_frames": total,
                "frames": probe.frames,
                "expected_sec": total / FPS,
                "duration": probe.duration_sec,
            }
    results["_before"] = before
    return results


@pytest.mark.parametrize("clips", [2, 5, 10, 20])
@pytest.mark.parametrize("label", ["56f", "124f"])
def test_p5_long_chain_is_promoted_atomically(long_chain_results, label, clips):
    """2/5/10/20本のいずれでも昇格・検証が成立し、partial が残らない。"""
    r = long_chain_results[(label, clips)]
    assert r["path"].is_file() and r["path"].stat().st_size > 0
    assert not r["path"].with_name(r["path"].name + ".partial").exists()


@pytest.mark.parametrize("clips", [2, 5, 10, 20])
@pytest.mark.parametrize("label", ["56f", "124f"])
def test_p5_long_chain_frame_count_within_bounds(long_chain_results, label, clips):
    """フレーム数は「合計以上・合計＋上限以下」（主検証）。"""
    r = long_chain_results[(label, clips)]
    max_extra = fo.concat_max_extra_frames(clips, FPS, SAMPLE_RATE)
    assert r["frames"] >= r["expected_frames"]
    assert r["frames"] <= r["expected_frames"] + max_extra


@pytest.mark.parametrize("clips", [2, 5, 10, 20])
@pytest.mark.parametrize("label", ["56f", "124f"])
def test_p5_long_chain_duration_within_tolerance(long_chain_results, label, clips):
    """duration は補助。許容式の内側で、なお2倍以上の余裕があること。"""
    r = long_chain_results[(label, clips)]
    tol = fo.concat_duration_tolerance_sec(clips, FPS, SAMPLE_RATE)
    err = abs(r["duration"] - r["expected_sec"])
    assert err <= tol, (label, clips, err, tol)
    assert err <= tol / 2.0, (label, clips, err, tol)
    # 明らかな短縮ではない
    assert r["duration"] >= r["expected_sec"] * fo.CONCAT_MIN_DURATION_RATIO


def test_p5_long_chain_sources_untouched(long_chain_results):
    """20本まで連結しても入力ファイルの size / mtime は変わらない。"""
    for path, (size, mtime_ns) in long_chain_results["_before"].items():
        assert path.stat().st_size == size
        assert path.stat().st_mtime_ns == mtime_ns


def test_p5_long_chain_keeps_codec_contract(ffmpeg, long_chain_results):
    """20本連結でも H.264/yuv420p/576×320/AAC 32kHz を維持する。"""
    probe = fo.decode_probe(ffmpeg, long_chain_results[("124f", 20)]["path"])
    assert "h264" in probe.video_desc.lower()
    assert "yuv420p" in probe.video_desc
    assert "576x320" in probe.video_desc
    assert "aac" in probe.audio_desc.lower()
    assert "32000 Hz" in probe.audio_desc


def test_p5_long_chain_rejects_wrong_expected_frames(ffmpeg, tmp_path):
    """期待フレーム数を偽ると昇格しない（partial も正式名も残さない）。"""
    src = tmp_path / "s.mp4"
    fo.make_mock_clip(ffmpeg, src, 56)
    out = tmp_path / "bad_chain.mp4"
    with pytest.raises(FileopsError, match="フレーム数が足りません"):
        fo.concat_reencode(
            ffmpeg,
            [src] * 5,
            out,
            fps=FPS,
            sample_rate=SAMPLE_RATE,
            expected_frames=56 * 6,  # 実際より1本ぶん多い
        )
    assert not out.exists()


def test_p5_single_clip_tolerance_is_unchanged():
    """単一動画の許容差は P0 以来の 0.5秒のまま（既存経路を変えない）。"""
    assert fo.DURATION_TOLERANCE_SEC == 0.5
    assert fo.concat_duration_tolerance_sec(1, FPS, SAMPLE_RATE) == 0.5


def test_p5_video_validator_accepts_explicit_tolerance(ffmpeg, tmp_path):
    """video_validator の tolerance_sec 省略時は従来どおり 0.5秒。"""
    probe = _probe(56, 2.33)
    strict = fo.video_validator(ffmpeg, 2.33 + 0.3, tolerance_sec=0.1)
    with pytest.raises(FileopsError, match="許容 ±0.10s"):
        _run_validator_with_probe(strict, probe, tmp_path / "i.mp4")
    default = fo.video_validator(ffmpeg, 2.33 + 0.3)
    _run_validator_with_probe(default, probe, tmp_path / "j.mp4")
