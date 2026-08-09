"""ConcatService のテスト（設計書 §10.6・§10.6.1・§10.7、P4契約 §4・§5）。

すべて `tmp_path` 上で完結させる（プロジェクトの `data/` には一切書き込まない）。
`app/assets/mock/*.mp4` は**読み取り専用**でコピー元としてのみ使う。
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core import ffmpeg_ops as fo
from app.core.concat_service import (
    STATE_DONE,
    STATE_FAILED,
    STATE_IDLE,
    ConcatError,
    ConcatService,
    ConcatStatus,
)
from app.core.config import load_config
from app.core.contracts import BackendIdentity, JobSpec
from app.core.fileops import FileopsError, partial_path
from app.core.history import HistoryRecord, HistoryStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = PROJECT_ROOT / "app" / "assets" / "mock"
FPS = 24

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax-H3-NF4",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)
T0 = datetime(2026, 8, 7, 10, 15, 30).astimezone()

CHAIN_IDS = [
    "v_20260807_101530_0001",
    "v_20260807_101531_0002",
    "v_20260807_101532_0003",
]


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def ffmpeg() -> str:
    return fo.resolve_ffmpeg("")


@pytest.fixture()
def cfg(tmp_path: Path):
    """実 config を読み、data_root だけを tmp_path へ差し替える（読み取り専用運用）。"""
    base = load_config(PROJECT_ROOT)
    return dataclasses.replace(
        base,
        data_root=tmp_path / "data",
        dedupe_boundary_frame=False,
    )


@pytest.fixture()
def history(cfg) -> HistoryStore:
    cfg.outputs_dir.mkdir(parents=True)
    cfg.concat_dir.mkdir()
    cfg.tmp_dir.mkdir()
    store = HistoryStore(cfg.history_path, cfg.data_root)
    assert store.load() == []
    return store


def build_chain(
    cfg,
    history: HistoryStore,
    ids: list[str],
    *,
    num_frames: int = 56,
    source: str = "mock_56.mp4",
    last_png: str = "mock_56_last.png",
) -> list[Path]:
    """SUCCESS の親子チェーンを作り、モック素材をコピーして成果物を置く。"""
    videos: list[Path] = []
    parent: str | None = None
    for job_id in ids:
        out = cfg.outputs_dir / f"{job_id}.mp4"
        last = cfg.outputs_dir / f"{job_id}_last.png"
        spec = JobSpec(
            job_id=job_id,
            prompt="テスト用プロンプト",
            num_frames=num_frames,
            steps=4,
            seed_requested=None,
            output_path=out,
            last_frame_path=last,
            job_type="single" if parent is None else "continuation",
            parent_id=parent,
            keyframe_path=(
                None if parent is None else cfg.outputs_dir / f"{parent}_last.png"
            ),
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
        shutil.copy(MOCK_DIR / source, out)
        shutil.copy(MOCK_DIR / last_png, last)
        history.mark_success(
            job_id,
            output_path=out,
            last_frame_path=last,
            seed_used=42,
            elapsed_sec=1.0,
            finished_at=T0,
        )
        videos.append(out)
        parent = job_id
    return videos


def wait_finished(svc: ConcatService, timeout: float = 180.0) -> ConcatStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = svc.status()
        if st.state in (STATE_DONE, STATE_FAILED):
            return st
        time.sleep(0.02)
    raise AssertionError(f"連結が終わりませんでした: {svc.status()}")


def snapshot(paths: list[Path]) -> dict[Path, tuple[int, int]]:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}


def assert_unchanged(before: dict[Path, tuple[int, int]]) -> None:
    for path, (size, mtime_ns) in before.items():
        assert path.stat().st_size == size, f"source が変更されました: {path.name}"
        assert path.stat().st_mtime_ns == mtime_ns, f"source の mtime が変わりました: {path.name}"


def orphan_partials(cfg) -> list[Path]:
    return sorted(cfg.concat_dir.glob("*.partial")) + sorted(cfg.tmp_dir.glob("*.partial"))


# ---------------------------------------------------------------- fake runner


class RecordingRunner:
    """ffmpeg を呼ばずに連結結果を捏造するランナー（配線の確認用）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def concat(self, inputs, out_path, **kw):
        self.calls.append({"inputs": list(inputs), "out_path": out_path, **kw})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake concat output")
        return out_path

    def extract_frame(self, video, frame_index, out_png):  # pragma: no cover
        raise AssertionError("dedupe 無効のはずです")

    def compare(self, png_a, png_b):  # pragma: no cover
        raise AssertionError("dedupe 無効のはずです")


class BlockingRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def concat(self, inputs, out_path, **kw):
        self.entered.set()
        assert self.release.wait(30), "テストが release し忘れています"
        return super().concat(inputs, out_path, **kw)


class FailingRunner(RecordingRunner):
    def __init__(self, exc: BaseException, *, leave_partial: bool = False) -> None:
        super().__init__()
        self.exc = exc
        self.leave_partial = leave_partial

    def concat(self, inputs, out_path, **kw):
        self.calls.append({"inputs": list(inputs), "out_path": out_path, **kw})
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.leave_partial:
            partial_path(out_path).write_bytes(b"broken partial")
        raise self.exc


# ================================================================ 基本


def test_status_is_idle_before_start(cfg, history):
    svc = ConcatService(cfg, history, runner=RecordingRunner())
    st = svc.status()
    assert st.state == STATE_IDLE
    assert st.running is False
    assert st.job_id is None and st.output_path is None
    assert st.sources == () and st.warnings == ()
    svc.shutdown()


def test_start_concat_rejects_empty_job_id(cfg, history):
    svc = ConcatService(cfg, history, runner=RecordingRunner())
    for bad in ("", "   ", None):
        with pytest.raises(ConcatError, match="連結する動画を選んでください"):
            svc.start_concat(bad)
    svc.shutdown()


def test_status_snapshot_is_immutable(cfg, history):
    svc = ConcatService(cfg, history, runner=RecordingRunner())
    st = svc.status()
    with pytest.raises(Exception):
        st.state = "hacked"
    svc.shutdown()


# ================================================================ 実 ffmpeg 連結


@pytest.fixture()
def real_service(cfg, history):
    svc = ConcatService(cfg, history)
    yield svc
    svc.shutdown()


def test_concat_two_clips_end_to_end(cfg, history, real_service, ffmpeg):
    videos = build_chain(cfg, history, CHAIN_IDS[:2])
    before = snapshot(videos)

    key = real_service.start_concat(CHAIN_IDS[1])
    assert key.endswith(CHAIN_IDS[1])
    st = wait_finished(real_service)

    assert st.state == STATE_DONE, st.message
    assert st.key == key
    assert st.error is None
    assert st.clips == 2
    assert st.sources == tuple(CHAIN_IDS[:2])
    assert st.output_path == cfg.concat_dir / f"c_{CHAIN_IDS[1]}_2clips.mp4"
    assert st.output_path.is_file() and st.output_path.stat().st_size > 0
    assert "連結が完了しました" in st.message

    # 出力仕様（P0 と同じ: H.264 / yuv420p / AAC 32kHz / 576×320 / 24fps）
    probe = fo.decode_probe(ffmpeg, st.output_path)
    assert probe.has_video and probe.has_audio
    assert "h264" in probe.video_desc.lower()
    assert "yuv420p" in probe.video_desc
    assert "576x320" in probe.video_desc
    assert "24 fps" in probe.video_desc
    assert "aac" in probe.audio_desc.lower()
    assert "32000 Hz" in probe.audio_desc
    assert probe.frames == 112
    assert probe.duration_sec == pytest.approx(112 / FPS, abs=0.5)

    # 履歴は昇格後にのみ更新される
    child = history.get(CHAIN_IDS[1])
    assert child.concat_path == f"concat/c_{CHAIN_IDS[1]}_2clips.mp4"
    assert child.concat_sources == CHAIN_IDS[:2]
    assert history.get(CHAIN_IDS[0]).concat_path is None

    assert orphan_partials(cfg) == []
    assert_unchanged(before)


def test_concat_three_clips(cfg, history, real_service, ffmpeg):
    videos = build_chain(cfg, history, CHAIN_IDS)
    before = snapshot(videos)

    real_service.start_concat(CHAIN_IDS[2])
    st = wait_finished(real_service)

    assert st.state == STATE_DONE, st.message
    assert st.clips == 3
    assert st.output_path.name == f"c_{CHAIN_IDS[2]}_3clips.mp4"
    probe = fo.decode_probe(ffmpeg, st.output_path)
    assert probe.frames == 168
    assert probe.duration_sec == pytest.approx(168 / FPS, abs=0.5)
    assert history.get(CHAIN_IDS[2]).concat_sources == CHAIN_IDS
    assert_unchanged(before)


def test_concat_middle_node_excludes_descendant(cfg, history, real_service, ffmpeg):
    """選択ノードより後の子孫は連結に含めない。"""
    build_chain(cfg, history, CHAIN_IDS)
    real_service.start_concat(CHAIN_IDS[1])
    st = wait_finished(real_service)

    assert st.state == STATE_DONE, st.message
    assert st.sources == tuple(CHAIN_IDS[:2])
    assert fo.decode_probe(ffmpeg, st.output_path).frames == 112
    assert history.get(CHAIN_IDS[2]).concat_path is None


def test_rerun_is_safe(cfg, history, real_service, ffmpeg):
    """同じチェーンを2回連結しても壊れない（上書き・履歴も同じ内容）。"""
    videos = build_chain(cfg, history, CHAIN_IDS[:2])
    before = snapshot(videos)

    real_service.start_concat(CHAIN_IDS[1])
    first = wait_finished(real_service)
    assert first.state == STATE_DONE, first.message
    first_record = history.get(CHAIN_IDS[1])

    real_service.start_concat(CHAIN_IDS[1])
    second = wait_finished(real_service)
    assert second.state == STATE_DONE, second.message
    assert second.output_path == first.output_path
    assert second.key != first.key

    assert history.get(CHAIN_IDS[1]).concat_path == first_record.concat_path
    assert history.get(CHAIN_IDS[1]).concat_sources == first_record.concat_sources
    assert fo.decode_probe(ffmpeg, second.output_path).frames == 112
    assert orphan_partials(cfg) == []
    assert_unchanged(before)


# ================================================================ チェーン検証の伝搬


def test_single_clip_chain_fails_with_japanese_message(cfg, history, real_service):
    build_chain(cfg, history, CHAIN_IDS[:1])
    real_service.start_concat(CHAIN_IDS[0])
    st = wait_finished(real_service)
    assert st.state == STATE_FAILED
    assert "連結には2本以上" in st.message
    assert list(cfg.concat_dir.iterdir()) == []
    assert history.get(CHAIN_IDS[0]).concat_path is None


def test_missing_source_file_fails(cfg, history, real_service):
    build_chain(cfg, history, CHAIN_IDS[:2])
    (cfg.outputs_dir / f"{CHAIN_IDS[0]}.mp4").unlink()
    real_service.start_concat(CHAIN_IDS[0 + 1])
    st = wait_finished(real_service)
    assert st.state == STATE_FAILED
    assert "見つかりません" in st.message
    assert CHAIN_IDS[0] in st.message
    assert list(cfg.concat_dir.iterdir()) == []


def test_unknown_job_id_fails(cfg, history, real_service):
    real_service.start_concat("v_20260807_101530_zzzz")
    st = wait_finished(real_service)
    assert st.state == STATE_FAILED
    assert "履歴に存在しないジョブID" in st.message


# ================================================================ 失敗と後始末


def test_ffmpeg_failure_leaves_no_output_and_no_partial(cfg, history):
    videos = build_chain(cfg, history, CHAIN_IDS[:2])
    before = snapshot(videos)
    runner = FailingRunner(
        fo.FfmpegError("ffmpeg が異常終了しました（終了コード 1）"), leave_partial=True
    )
    svc = ConcatService(cfg, history, runner=runner)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_FAILED
    assert "ffmpeg が異常終了しました" in st.message
    assert st.output_path is None
    assert not (cfg.concat_dir / f"c_{CHAIN_IDS[1]}_2clips.mp4").exists()
    assert orphan_partials(cfg) == []
    assert history.get(CHAIN_IDS[1]).concat_path is None
    assert_unchanged(before)
    svc.shutdown()


def test_validation_failure_is_cleaned_up(cfg, history):
    """promote の検証失敗（再生時間不一致など）でも孤児 partial を残さない。"""
    build_chain(cfg, history, CHAIN_IDS[:2])
    runner = FailingRunner(
        FileopsError("再生時間が想定と一致しません: c_x_2clips.mp4"), leave_partial=True
    )
    svc = ConcatService(cfg, history, runner=runner)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_FAILED
    assert "再生時間が想定と一致しません" in st.message
    assert orphan_partials(cfg) == []
    assert list(cfg.concat_dir.iterdir()) == []
    svc.shutdown()


def test_unexpected_exception_is_reported_in_japanese(cfg, history):
    build_chain(cfg, history, CHAIN_IDS[:2])
    svc = ConcatService(cfg, history, runner=FailingRunner(RuntimeError("boom")))
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)
    assert st.state == STATE_FAILED
    assert "連結に失敗しました" in st.message
    assert "RuntimeError" in st.message
    svc.shutdown()


def test_broken_input_video_fails_without_residue(cfg, history, real_service):
    """破損した入力: 実 ffmpeg が失敗し、正式名も partial も残らない。"""
    videos = build_chain(cfg, history, CHAIN_IDS[:2])
    videos[1].write_bytes(b"this is not a video file" * 100)
    real_service.start_concat(CHAIN_IDS[1])
    st = wait_finished(real_service)

    assert st.state == STATE_FAILED
    assert st.output_path is None
    assert not (cfg.concat_dir / f"c_{CHAIN_IDS[1]}_2clips.mp4").exists()
    assert orphan_partials(cfg) == []
    assert history.get(CHAIN_IDS[1]).concat_path is None


def test_duration_mismatch_fails_verification(cfg, history, real_service):
    """履歴の想定長（124f×2）と実体（56f×2）がずれていれば昇格させない。

    P5 以降は**フレーム数が主検証**なので、まずフレーム数不足で弾かれる
    （248 フレームのはずが 112 しかない）。
    """
    build_chain(cfg, history, CHAIN_IDS[:2], num_frames=124)
    real_service.start_concat(CHAIN_IDS[1])
    st = wait_finished(real_service)

    assert st.state == STATE_FAILED
    assert "フレーム数が足りません" in st.message
    assert not (cfg.concat_dir / f"c_{CHAIN_IDS[1]}_2clips.mp4").exists()
    assert orphan_partials(cfg) == []
    assert history.get(CHAIN_IDS[1]).concat_path is None


def test_failure_keeps_previous_successful_output(cfg, history, real_service):
    """過去の成功で作った完成動画を、後の失敗で消してしまわない。"""
    videos = build_chain(cfg, history, CHAIN_IDS[:2])
    real_service.start_concat(CHAIN_IDS[1])
    ok = wait_finished(real_service)
    assert ok.state == STATE_DONE, ok.message
    kept = ok.output_path
    kept_size = kept.stat().st_size

    videos[1].write_bytes(b"broken now")
    real_service.start_concat(CHAIN_IDS[1])
    st = wait_finished(real_service)

    assert st.state == STATE_FAILED
    assert kept.is_file() and kept.stat().st_size == kept_size
    assert orphan_partials(cfg) == []


# ================================================================ 排他・非ブロッキング


def test_concurrent_concat_is_rejected(cfg, history):
    build_chain(cfg, history, CHAIN_IDS[:2])
    runner = BlockingRunner()
    svc = ConcatService(cfg, history, runner=runner)

    svc.start_concat(CHAIN_IDS[1])
    assert runner.entered.wait(10)
    assert svc.status().running is True

    with pytest.raises(ConcatError, match="連結を実行中です"):
        svc.start_concat(CHAIN_IDS[1])
    with pytest.raises(ConcatError, match="連結を実行中です"):
        svc.start_concat(CHAIN_IDS[0])

    runner.release.set()
    st = wait_finished(svc)
    assert st.state == STATE_DONE, st.message
    assert len(runner.calls) == 1
    # 完了後は再度受け付ける
    svc.start_concat(CHAIN_IDS[1])
    assert wait_finished(svc).state == STATE_DONE
    svc.shutdown()


def test_start_concat_does_not_block_caller(cfg, history):
    """Gradio コールバックを止めない（開始呼び出しは即戻る）。"""
    build_chain(cfg, history, CHAIN_IDS[:2])
    runner = BlockingRunner()
    svc = ConcatService(cfg, history, runner=runner)

    t0 = time.monotonic()
    svc.start_concat(CHAIN_IDS[1])
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"start_concat がブロックしました（{elapsed:.2f}秒）"

    assert runner.entered.wait(10)
    # 実行中でも status() は即座に返る
    t1 = time.monotonic()
    st = svc.status()
    assert (time.monotonic() - t1) < 0.5
    assert st.running is True

    runner.release.set()
    wait_finished(svc)
    svc.shutdown()


def test_shutdown_waits_and_blocks_new_requests(cfg, history):
    build_chain(cfg, history, CHAIN_IDS[:2])
    runner = BlockingRunner()
    svc = ConcatService(cfg, history, runner=runner)
    svc.start_concat(CHAIN_IDS[1])
    assert runner.entered.wait(10)

    svc.shutdown(timeout=0.2)  # 強制終了はしない（すぐ戻る）
    with pytest.raises(ConcatError, match="終了中"):
        svc.start_concat(CHAIN_IDS[1])

    runner.release.set()
    svc.shutdown(timeout=10)
    assert svc.status().state == STATE_DONE


def test_shutdown_is_safe_when_idle(cfg, history):
    svc = ConcatService(cfg, history, runner=RecordingRunner())
    svc.shutdown()
    svc.shutdown()


# ================================================================ 重複フレーム除去


@pytest.fixture()
def dedupe_cfg(cfg):
    return dataclasses.replace(cfg, dedupe_boundary_frame=True)


def _make_first_frame_the_parent_last(ffmpeg: str, cfg, parent_id: str, child_video: Path):
    """親の「最終フレームPNG」を子動画の先頭フレームで置き換える（完全一致の再現）。"""
    target = cfg.outputs_dir / f"{parent_id}_last.png"
    fo.extract_frame_exact(ffmpeg, child_video, 0, target)
    return target


def test_dedupe_disabled_by_default(cfg, history, ffmpeg):
    """既定 OFF: 比較も除去も行わない（RecordingRunner が呼ばれても例外にならない）。"""
    assert cfg.dedupe_boundary_frame is False
    build_chain(cfg, history, CHAIN_IDS[:2])
    runner = RecordingRunner()  # extract_frame / compare を呼ぶと AssertionError
    svc = ConcatService(cfg, history, runner=runner)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == ()
    assert runner.calls[0]["trim_first_frame_of"] is None
    assert runner.calls[0]["expected_duration_sec"] == pytest.approx(112 / FPS)
    svc.shutdown()


def test_dedupe_exact_match_removes_one_frame(dedupe_cfg, history, ffmpeg):
    """完全一致の境界だけを除去する（同じ素材を2本連結）。"""
    videos = build_chain(dedupe_cfg, history, CHAIN_IDS[:2])
    _make_first_frame_the_parent_last(ffmpeg, dedupe_cfg, CHAIN_IDS[0], videos[1])
    before = snapshot(videos)

    svc = ConcatService(dedupe_cfg, history)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == (1,)
    assert any("重複フレームとして除去を試みます" in w for w in st.warnings)
    assert any("平均差 0.000" in w for w in st.warnings)
    assert any("除去 1 フレーム" in w for w in st.warnings)

    probe = fo.decode_probe(ffmpeg, st.output_path)
    assert probe.frames == 111  # 112 - 1
    assert probe.duration_sec == pytest.approx(111 / FPS, abs=0.5)
    assert orphan_partials(dedupe_cfg) == []
    assert list(dedupe_cfg.tmp_dir.glob("dedupe_*")) == []  # 作業PNGを残さない
    assert_unchanged(before)
    svc.shutdown()


def test_dedupe_three_clips_removes_two_frames(dedupe_cfg, history, ffmpeg):
    videos = build_chain(dedupe_cfg, history, CHAIN_IDS)
    _make_first_frame_the_parent_last(ffmpeg, dedupe_cfg, CHAIN_IDS[0], videos[1])
    _make_first_frame_the_parent_last(ffmpeg, dedupe_cfg, CHAIN_IDS[1], videos[2])

    svc = ConcatService(dedupe_cfg, history)
    svc.start_concat(CHAIN_IDS[2])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == (1, 2)
    assert fo.decode_probe(ffmpeg, st.output_path).frames == 166  # 168 - 2
    svc.shutdown()


def test_dedupe_mismatch_falls_back_to_normal_concat(dedupe_cfg, history, ffmpeg):
    """一致しない境界は絶対に除去しない（通常連結へフォールバック）。"""
    build_chain(dedupe_cfg, history, CHAIN_IDS[:2])  # 親の last は本物の最終フレーム
    svc = ConcatService(dedupe_cfg, history)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == ()
    assert any("一致しないため除去しません" in w for w in st.warnings)
    assert fo.decode_probe(ffmpeg, st.output_path).frames == 112
    svc.shutdown()


def test_dedupe_partial_match_trims_only_matching_boundary(dedupe_cfg, history, ffmpeg):
    """3本中1箇所だけ一致 → その境界のみ除去する。"""
    videos = build_chain(dedupe_cfg, history, CHAIN_IDS)
    _make_first_frame_the_parent_last(ffmpeg, dedupe_cfg, CHAIN_IDS[1], videos[2])

    svc = ConcatService(dedupe_cfg, history)
    svc.start_concat(CHAIN_IDS[2])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == (2,)
    assert fo.decode_probe(ffmpeg, st.output_path).frames == 167
    svc.shutdown()


def test_dedupe_missing_last_frame_falls_back(dedupe_cfg, history, ffmpeg):
    videos = build_chain(dedupe_cfg, history, CHAIN_IDS[:2])
    _make_first_frame_the_parent_last(ffmpeg, dedupe_cfg, CHAIN_IDS[0], videos[1])
    (dedupe_cfg.outputs_dir / f"{CHAIN_IDS[0]}_last.png").unlink()

    svc = ConcatService(dedupe_cfg, history)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == ()
    assert any("最終フレーム画像がありません" in w for w in st.warnings)
    assert fo.decode_probe(ffmpeg, st.output_path).frames == 112
    svc.shutdown()


def test_dedupe_extraction_failure_falls_back(dedupe_cfg, history):
    """比較に失敗しても連結自体は成功させる（安全側のフォールバック）。"""
    build_chain(dedupe_cfg, history, CHAIN_IDS[:2])

    class BadCompareRunner(RecordingRunner):
        def extract_frame(self, video, frame_index, out_png):
            raise fo.FfmpegError("フレーム抽出に失敗しました")

    svc = ConcatService(dedupe_cfg, history, runner=BadCompareRunner())
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == ()
    assert any("通常連結します" in w for w in st.warnings)
    svc.shutdown()


def test_dedupe_size_mismatch_never_trims(dedupe_cfg, history, ffmpeg):
    """親の最終フレームPNGが別解像度なら、閾値に関係なく除去しない。"""
    from PIL import Image

    videos = build_chain(dedupe_cfg, history, CHAIN_IDS[:2])
    target = _make_first_frame_the_parent_last(
        ffmpeg, dedupe_cfg, CHAIN_IDS[0], videos[1]
    )
    with Image.open(target) as img:
        img.convert("RGB").resize((288, 160)).save(target, format="PNG")

    loose = dataclasses.replace(
        dedupe_cfg, dedupe_max_mean_diff=1e9, dedupe_max_max_diff=1e9
    )
    svc = ConcatService(loose, history)
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == ()
    assert any("サイズ不一致" in w for w in st.warnings)
    assert fo.decode_probe(ffmpeg, st.output_path).frames == 112
    svc.shutdown()


def test_dedupe_expected_duration_is_adjusted(dedupe_cfg, history, ffmpeg):
    """除去した枚数ぶんを expected_duration_sec から引く（検証が通る前提）。"""
    videos = build_chain(dedupe_cfg, history, CHAIN_IDS[:2])
    _make_first_frame_the_parent_last(ffmpeg, dedupe_cfg, CHAIN_IDS[0], videos[1])

    calls: list[dict] = []
    real = fo.concat_reencode

    class SpyRunner:
        def __init__(self):
            self.inner = None

        def concat(self, inputs, out_path, **kw):
            calls.append(kw)
            return real(fo.resolve_ffmpeg(""), inputs, out_path, **kw)

        def extract_frame(self, video, frame_index, out_png):
            return fo.extract_frame_exact(fo.resolve_ffmpeg(""), video, frame_index, out_png)

        def compare(self, png_a, png_b):
            return fo.compare_frames(png_a, png_b)

    svc = ConcatService(dedupe_cfg, history, runner=SpyRunner())
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert calls[0]["trim_first_frame_of"] == {1}
    assert calls[0]["expected_duration_sec"] == pytest.approx((112 - 1) / FPS)
    svc.shutdown()


# ================================================ P5: 長チェーンとフレーム数検証


def test_p5_expected_frames_is_passed_to_the_runner(cfg, history):
    """主検証はフレーム数。合計値がそのままランナーへ渡る（契約 §5.2）。"""
    build_chain(cfg, history, CHAIN_IDS[:3])
    runner = RecordingRunner()
    svc = ConcatService(cfg, history, runner=runner)
    svc.start_concat(CHAIN_IDS[2])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert runner.calls[0]["expected_frames"] == 56 * 3
    assert runner.calls[0]["expected_duration_sec"] == pytest.approx(56 * 3 / FPS)
    svc.shutdown()


def test_p5_expected_frames_accounts_for_dedupe_trim(cfg, history, ffmpeg):
    """重複除去を行った場合は除去枚数ぶんフレーム数からも引く。"""
    dedupe = dataclasses.replace(
        cfg, dedupe_boundary_frame=True, dedupe_max_mean_diff=1e9, dedupe_max_max_diff=1e9
    )
    build_chain(dedupe, history, CHAIN_IDS[:2])
    # 親の最終フレーム画像を子の先頭フレームと同一にして「一致」判定を作る
    child = dedupe.outputs_dir / f"{CHAIN_IDS[1]}.mp4"
    parent_png = dedupe.outputs_dir / f"{CHAIN_IDS[0]}_last.png"
    fo.extract_frame_exact(ffmpeg, child, 0, parent_png)

    calls: list[dict] = []

    class SpyRunner(RecordingRunner):
        """連結だけ捏造し、判定（抽出・比較）は本物を使うランナー。"""

        def concat(self, inputs, out_path, **kw):
            calls.append(kw)
            return super().concat(inputs, out_path, **kw)

        def extract_frame(self, video, frame_index, out_png):
            return fo.extract_frame_exact(ffmpeg, video, frame_index, out_png)

        def compare(self, png_a, png_b):
            return fo.compare_frames(png_a, png_b)

    svc = ConcatService(dedupe, history, runner=SpyRunner())
    svc.start_concat(CHAIN_IDS[1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.trimmed_boundaries == (1,)
    assert calls[0]["expected_frames"] == 112 - 1
    assert calls[0]["expected_duration_sec"] == pytest.approx((112 - 1) / FPS)
    svc.shutdown()


def _long_chain_ids(n: int) -> list[str]:
    return [f"v_20260807_1015{i:02d}_{i:04d}" for i in range(n)]


@pytest.mark.parametrize("clips", [2, 5, 10, 20])
def test_p5_real_long_chain_succeeds(cfg, history, ffmpeg, clips):
    """2/5/10/20本のいずれでも実 ffmpeg で連結・検証・原子的昇格が成立する。"""
    ids = _long_chain_ids(clips)
    videos = build_chain(cfg, history, ids)
    before = snapshot(videos)

    svc = ConcatService(cfg, history, ffmpeg_path="")
    svc.start_concat(ids[-1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    assert st.clips == clips
    assert st.sources == tuple(ids)
    out = st.output_path
    assert out is not None and out.is_file() and out.stat().st_size > 0
    assert out.name == f"c_{ids[-1]}_{clips}clips.mp4"

    # フレーム数は「合計以上・合計＋上限以下」（主検証）
    probe = fo.decode_probe(ffmpeg, out)
    total = 56 * clips
    max_extra = fo.concat_max_extra_frames(clips, FPS, cfg.audio_sample_rate)
    assert probe.frames >= total
    assert probe.frames <= total + max_extra
    # duration は補助。許容式の内側
    tol = fo.concat_duration_tolerance_sec(clips, FPS, cfg.audio_sample_rate)
    assert abs(probe.duration_sec - total / FPS) <= tol

    # 原子的昇格: partial が残らない / source が変わらない
    assert orphan_partials(cfg) == []
    assert_unchanged(before)

    # 昇格後にだけ履歴へ記録される
    record = history.get(ids[-1])
    assert record.concat_path is not None
    assert record.concat_sources == ids
    svc.shutdown()


def test_p5_long_chain_output_is_still_h264_aac(cfg, history, ffmpeg):
    ids = _long_chain_ids(20)
    build_chain(cfg, history, ids)
    svc = ConcatService(cfg, history, ffmpeg_path="")
    svc.start_concat(ids[-1])
    st = wait_finished(svc)

    assert st.state == STATE_DONE, st.message
    probe = fo.decode_probe(ffmpeg, st.output_path)
    assert "h264" in probe.video_desc.lower()
    assert "yuv420p" in probe.video_desc
    assert "576x320" in probe.video_desc
    assert "aac" in probe.audio_desc.lower()
    assert "32000 Hz" in probe.audio_desc
    svc.shutdown()


def test_p5_config_has_no_reencode_switch(cfg):
    """V1 の連結方式は1つだけ。設定から -c copy へ到達できない（契約 §5.3）。"""
    assert not hasattr(cfg, "concat_reencode")
    import inspect

    from app.core import concat_service as cs

    source = inspect.getsource(cs)
    assert "concat_copy" not in source
    assert "concat_reencode" in source  # 使うのは常に再エンコード連結


# ================================================================ 指定順連結（P5.2）
#
# チェーン連結と同じサービス・同じ排他・同じ清掃契約に載っていることと、
# **ユーザーが指定した順番がそのまま成果物になる**ことを確かめる。

from app.core.concat_manifest import ConcatManifest, ConcatManifestError  # noqa: E402
from app.core.concat_service import MODE_CHAIN, MODE_CUSTOM  # noqa: E402

SINGLE_IDS = [
    "v_20260809_210000_s001",
    "v_20260809_210001_s002",
    "v_20260809_210002_s003",
]


@pytest.fixture()
def manifest(cfg, history) -> ConcatManifest:
    store = ConcatManifest(cfg.concat_manifest_path, cfg.data_root)
    assert store.load() == []
    return store


def build_singles(
    cfg,
    history: HistoryStore,
    ids: list[str],
    *,
    num_frames: int = 56,
    source: str = "mock_56.mp4",
    last_png: str = "mock_56_last.png",
    created: dict[str, datetime] | None = None,
) -> list[Path]:
    """親子関係のない独立した SUCCESS 動画を作る（指定順連結の素材）。"""
    videos: list[Path] = []
    for job_id in ids:
        out = cfg.outputs_dir / f"{job_id}.mp4"
        last = cfg.outputs_dir / f"{job_id}_last.png"
        spec = JobSpec(
            job_id=job_id,
            prompt=f"素材 {job_id}",
            num_frames=num_frames,
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
                created_at=(created or {}).get(job_id, T0),
            )
        )
        history.mark_running(job_id, T0)
        shutil.copy(MOCK_DIR / source, out)
        shutil.copy(MOCK_DIR / last_png, last)
        history.mark_success(
            job_id,
            output_path=out,
            last_frame_path=last,
            seed_used=42,
            elapsed_sec=1.0,
            finished_at=T0,
        )
        videos.append(out)
    return videos


class ProbingRunner(RecordingRunner):
    """指定順連結用の fake。素材のデコード確認も捏造する。"""

    def probe(self, video):
        return fo.ProbeResult(
            duration_sec=56 / FPS,
            frames=56,
            has_video=True,
            has_audio=True,
            video_desc="Video: h264, 576x320",
            audio_desc="Audio: aac (LC), 32000 Hz, stereo",
        )


class ProbingBlockingRunner(ProbingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def concat(self, inputs, out_path, **kw):
        self.entered.set()
        assert self.release.wait(30), "テストが release し忘れています"
        return super().concat(inputs, out_path, **kw)


def custom_service(cfg, history, manifest, runner=None) -> ConcatService:
    return ConcatService(
        cfg, history, runner=runner or ProbingRunner(), manifest=manifest
    )


# ---------------------------------------------------------------- 配線と順序


def test_custom_concat_passes_inputs_in_the_requested_order(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS)
    runner = ProbingRunner()
    svc = custom_service(cfg, history, manifest, runner)

    requested = [SINGLE_IDS[1], SINGLE_IDS[2], SINGLE_IDS[0]]
    svc.start_custom_concat(requested)
    status = wait_finished(svc)

    assert status.state == STATE_DONE, status.message
    passed = [Path(p).stem for p in runner.calls[0]["inputs"]]
    assert passed == requested, "ffmpeg へ渡す順番が指定順と違います"


def test_custom_concat_order_differs_from_creation_order(cfg, history, manifest):
    """作成日時の逆順を指定しても、**指定順のまま**連結される。"""
    created = {
        SINGLE_IDS[0]: T0,
        SINGLE_IDS[1]: T0 + timedelta(minutes=1),
        SINGLE_IDS[2]: T0 + timedelta(minutes=2),
    }
    build_singles(cfg, history, SINGLE_IDS, created=created)
    runner = ProbingRunner()
    svc = custom_service(cfg, history, manifest, runner)

    reverse = list(reversed(SINGLE_IDS))
    svc.start_custom_concat(reverse)
    assert wait_finished(svc).state == STATE_DONE
    assert [Path(p).stem for p in runner.calls[0]["inputs"]] == reverse
    assert manifest.list_entries()[0].sources == tuple(reverse)


def test_custom_concat_status_reports_mode_and_clips(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS)
    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(SINGLE_IDS)
    status = wait_finished(svc)

    assert status.mode == MODE_CUSTOM
    assert status.mode_label == "指定順連結"
    assert status.clips == 3
    assert status.sources == tuple(SINGLE_IDS)
    assert status.concat_id and status.concat_id.startswith("cm_")


def test_chain_concat_still_reports_chain_mode(cfg, history):
    """既存のチェーン連結は mode=chain のまま（UI 非回帰）。"""
    build_chain(cfg, history, CHAIN_IDS[:2])
    svc = ConcatService(cfg, history, runner=RecordingRunner())
    svc.start_concat(CHAIN_IDS[1])
    status = wait_finished(svc)
    assert status.mode == MODE_CHAIN
    assert status.concat_id is None


def test_custom_concat_output_name_and_manifest_entry(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS)
    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(SINGLE_IDS)
    status = wait_finished(svc)

    assert status.output_path.name.startswith("cm_")
    assert status.output_path.name.endswith("_3clips.mp4")
    assert status.output_path.parent == cfg.concat_dir

    entry = manifest.list_entries()[0]
    assert entry.sources == tuple(SINGLE_IDS)
    assert entry.clips == 3
    assert entry.num_frames_total == 56 * 3
    assert entry.fps == 24 and entry.width == 576 and entry.height == 320
    assert entry.execution_engine == "mock"
    assert entry.backend_id == IDENTITY.backend_id
    assert entry.model_revision == IDENTITY.model_revision
    assert entry.upscale_path is None  # P6 で埋める欄


def test_custom_concat_does_not_touch_history(cfg, history, manifest):
    """履歴（history.json）は1バイトも変わらない（設計書 §23.1）。"""
    build_singles(cfg, history, SINGLE_IDS)
    before = cfg.history_path.read_bytes()

    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(SINGLE_IDS)
    assert wait_finished(svc).state == STATE_DONE

    assert cfg.history_path.read_bytes() == before
    for job_id in SINGLE_IDS:
        assert history.get(job_id).concat_path is None


def test_custom_concat_leaves_sources_untouched(cfg, history, manifest):
    videos = build_singles(cfg, history, SINGLE_IDS)
    before = snapshot(videos)

    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(list(reversed(SINGLE_IDS)))
    assert wait_finished(svc).state == STATE_DONE
    assert_unchanged(before)


def test_custom_concat_never_dedupes_boundaries(cfg, history, manifest):
    """隣接クリップに親子関係が無いので境界の重複除去は行わない。"""
    build_singles(cfg, history, SINGLE_IDS)
    runner = ProbingRunner()
    svc = custom_service(cfg, history, manifest, runner)
    svc.start_custom_concat(SINGLE_IDS)
    assert wait_finished(svc).state == STATE_DONE
    assert runner.calls[0]["trim_first_frame_of"] is None


def test_custom_concat_expected_frames_is_the_sum(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS)
    runner = ProbingRunner()
    svc = custom_service(cfg, history, manifest, runner)
    svc.start_custom_concat(SINGLE_IDS)
    assert wait_finished(svc).state == STATE_DONE
    assert runner.calls[0]["expected_frames"] == 56 * 3


# ---------------------------------------------------------------- 入口の検証


def test_custom_concat_rejects_fewer_than_two(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:1])
    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(SINGLE_IDS[:1])
    status = wait_finished(svc)
    assert status.state == STATE_FAILED
    assert "2本以上" in status.message


def test_custom_concat_rejects_empty_selection(cfg, history, manifest):
    svc = custom_service(cfg, history, manifest)
    with pytest.raises(ConcatError, match="選んでください"):
        svc.start_custom_concat([])


def test_custom_concat_rejects_duplicates(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])
    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat([SINGLE_IDS[0], SINGLE_IDS[0]])
    status = wait_finished(svc)
    assert status.state == STATE_FAILED
    assert "同じ動画が複数回" in status.message


def test_custom_concat_rejects_missing_source_file(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])
    (cfg.outputs_dir / f"{SINGLE_IDS[1]}.mp4").unlink()
    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)
    assert status.state == STATE_FAILED
    assert "見つかりません" in status.message
    assert manifest.list_entries() == []


def test_custom_concat_rejects_sources_without_audio(cfg, history, manifest):
    """素材のデコード検査で音声が無いものを弾く（音声仕様の確認）。"""
    build_singles(cfg, history, SINGLE_IDS[:2])

    class NoAudioRunner(ProbingRunner):
        def probe(self, video):
            return fo.ProbeResult(
                duration_sec=2.33,
                frames=56,
                has_video=True,
                has_audio=False,
                video_desc="Video: h264",
                audio_desc="",
            )

    svc = custom_service(cfg, history, manifest, NoAudioRunner())
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)
    assert status.state == STATE_FAILED
    assert "音声が入っていません" in status.message


def test_custom_concat_rejects_mismatched_sample_rate(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])

    class MixedRateRunner(ProbingRunner):
        def __init__(self):
            super().__init__()
            self.seen = 0

        def probe(self, video):
            self.seen += 1
            rate = 32000 if self.seen == 1 else 48000
            return fo.ProbeResult(
                duration_sec=2.33,
                frames=56,
                has_video=True,
                has_audio=True,
                video_desc="Video: h264",
                audio_desc=f"Audio: aac (LC), {rate} Hz, stereo",
            )

    svc = custom_service(cfg, history, manifest, MixedRateRunner())
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)
    assert status.state == STATE_FAILED
    assert "音声の形式が揃っていません" in status.message


def test_custom_concat_rejects_undecodable_source(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])

    class BrokenProbe(ProbingRunner):
        def probe(self, video):
            raise fo.FfmpegError("デコードできません（擬似）")

    svc = custom_service(cfg, history, manifest, BrokenProbe())
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)
    assert status.state == STATE_FAILED
    assert "読み込めませんでした" in status.message


def test_custom_concat_requires_a_manifest(cfg, history):
    svc = ConcatService(cfg, history, runner=ProbingRunner(), manifest=None)
    with pytest.raises(ConcatError, match="利用できません"):
        svc.start_custom_concat(SINGLE_IDS[:2])


# ---------------------------------------------------------------- 排他


def test_custom_concat_blocks_a_second_custom_concat(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS)
    runner = ProbingBlockingRunner()
    svc = custom_service(cfg, history, manifest, runner)

    svc.start_custom_concat(SINGLE_IDS[:2])
    assert runner.entered.wait(10)
    try:
        with pytest.raises(ConcatError, match="連結を実行中です"):
            svc.start_custom_concat(SINGLE_IDS[1:])
    finally:
        runner.release.set()
    assert wait_finished(svc).state == STATE_DONE


def test_custom_concat_blocks_chain_concat_and_vice_versa(cfg, history, manifest):
    """チェーン連結と指定順連結は**同じレーン**で排他される（設計書 §23.4）。"""
    build_chain(cfg, history, CHAIN_IDS[:2])
    build_singles(cfg, history, SINGLE_IDS[:2])
    runner = ProbingBlockingRunner()
    svc = custom_service(cfg, history, manifest, runner)

    svc.start_custom_concat(SINGLE_IDS[:2])
    assert runner.entered.wait(10)
    try:
        with pytest.raises(ConcatError, match="指定順連結"):
            svc.start_concat(CHAIN_IDS[1])
    finally:
        runner.release.set()
    assert wait_finished(svc).state == STATE_DONE

    # 逆向き（チェーン実行中に指定順を投げる）
    runner2 = ProbingBlockingRunner()
    svc2 = custom_service(cfg, history, manifest, runner2)
    svc2.start_concat(CHAIN_IDS[1])
    assert runner2.entered.wait(10)
    try:
        with pytest.raises(ConcatError, match="チェーン連結"):
            svc2.start_custom_concat(SINGLE_IDS[:2])
    finally:
        runner2.release.set()
    wait_finished(svc2)


def test_custom_concat_runs_sequentially(cfg, history, manifest):
    """連続実行できる（前回の結果に引きずられない）。"""
    build_singles(cfg, history, SINGLE_IDS)
    svc = custom_service(cfg, history, manifest)

    svc.start_custom_concat(SINGLE_IDS[:2])
    assert wait_finished(svc).state == STATE_DONE
    first = manifest.list_entries()[0].id

    svc.start_custom_concat(list(reversed(SINGLE_IDS)))
    assert wait_finished(svc).state == STATE_DONE

    ids = {e.id for e in manifest.list_entries()}
    assert len(ids) == 2 and first in ids


def test_custom_concat_is_blocked_after_shutdown(cfg, history, manifest):
    svc = custom_service(cfg, history, manifest)
    svc.shutdown(timeout=1.0)
    with pytest.raises(ConcatError, match="終了中"):
        svc.start_custom_concat(SINGLE_IDS[:2])


def test_custom_concat_start_does_not_block_caller(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])
    runner = ProbingBlockingRunner()
    svc = custom_service(cfg, history, manifest, runner)

    started = time.monotonic()
    svc.start_custom_concat(SINGLE_IDS[:2])
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"呼び出しがブロックしています（{elapsed:.2f}秒）"
    runner.release.set()
    wait_finished(svc)


# ---------------------------------------------------------------- 失敗と清掃


def test_custom_concat_ffmpeg_failure_leaves_no_residue(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])

    class FailingProbe(ProbingRunner):
        def concat(self, inputs, out_path, **kw):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            partial_path(out_path).write_bytes(b"broken partial")
            raise fo.FfmpegError("ffmpeg 失敗（擬似）")

    svc = custom_service(cfg, history, manifest, FailingProbe())
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)

    assert status.state == STATE_FAILED
    assert manifest.list_entries() == []
    assert orphan_partials(cfg) == []
    assert list(cfg.concat_dir.glob("cm_*.mp4")) == []


def test_custom_concat_verification_failure_leaves_no_product(cfg, history, manifest):
    build_singles(cfg, history, SINGLE_IDS[:2])

    class InvalidOutput(ProbingRunner):
        def concat(self, inputs, out_path, **kw):
            raise FileopsError("連結動画のフレーム数が足りません（擬似）")

    svc = custom_service(cfg, history, manifest, InvalidOutput())
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)

    assert status.state == STATE_FAILED
    assert manifest.list_entries() == []
    assert list(cfg.concat_dir.glob("cm_*.mp4")) == []


def test_manifest_failure_rolls_back_the_promoted_mp4(cfg, history, manifest, monkeypatch):
    """**台帳へ載せられなかった MP4 は必ず取り消す**（設計書 §23.4 手順5）。"""
    build_singles(cfg, history, SINGLE_IDS[:2])
    svc = custom_service(cfg, history, manifest)

    def boom(entry):
        raise ConcatManifestError("台帳を保存できません（擬似）")

    monkeypatch.setattr(manifest, "add", boom)
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)
    monkeypatch.undo()

    assert status.state == STATE_FAILED
    assert "台帳を保存できません" in status.message
    assert "削除しました" in status.message
    # 一覧に出ない孤児 MP4 が残っていない
    assert list(cfg.concat_dir.glob("cm_*.mp4")) == []
    assert orphan_partials(cfg) == []
    assert manifest.list_entries() == []


def test_manifest_failure_quarantines_when_delete_fails(
    cfg, history, manifest, monkeypatch
):
    """削除できない場合は隔離名へ退避する（一覧には出ない）。"""
    build_singles(cfg, history, SINGLE_IDS[:2])
    svc = custom_service(cfg, history, manifest)

    def boom(entry):
        raise ConcatManifestError("台帳を保存できません（擬似）")

    real_unlink = Path.unlink

    def refuse_unlink(self, *a, **kw):
        if self.name.startswith("cm_") and self.suffix == ".mp4":
            raise OSError("削除できません（擬似）")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(manifest, "add", boom)
    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    svc.start_custom_concat(SINGLE_IDS[:2])
    status = wait_finished(svc)
    monkeypatch.undo()

    assert status.state == STATE_FAILED
    assert "退避しました" in status.message
    assert list(cfg.concat_dir.glob("cm_*.mp4")) == []  # 正式名では残っていない
    quarantined = list(cfg.concat_dir.glob(".orphan_cm_*.mp4"))
    assert len(quarantined) == 1


def test_manifest_failure_logs_when_neither_delete_nor_move_works(
    cfg, history, manifest, monkeypatch, caplog
):
    """削除も隔離もできないときは、正確なパスと対処をログへ残す。"""
    build_singles(cfg, history, SINGLE_IDS[:2])
    svc = custom_service(cfg, history, manifest)

    def boom(entry):
        raise ConcatManifestError("台帳を保存できません（擬似）")

    real_unlink = Path.unlink
    real_replace = os.replace

    def refuse_unlink(self, *a, **kw):
        if self.name.startswith("cm_") and self.suffix == ".mp4":
            raise OSError("削除できません（擬似）")
        return real_unlink(self, *a, **kw)

    def refuse_replace(src, dst):
        if ".orphan_cm_" in str(dst):
            raise OSError("移動できません（擬似）")
        return real_replace(src, dst)

    monkeypatch.setattr(manifest, "add", boom)
    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    monkeypatch.setattr(os, "replace", refuse_replace)
    with caplog.at_level("ERROR", logger="atelier.concat"):
        svc.start_custom_concat(SINGLE_IDS[:2])
        status = wait_finished(svc)
    monkeypatch.undo()

    assert status.state == STATE_FAILED
    assert "不要なファイルが残りました" in status.message
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "手動削除" in logged
    assert "cm_" in logged and ".mp4" in logged  # 正確なパスが残っている


def test_manifest_is_recorded_only_after_promotion(cfg, history, manifest):
    """昇格が終わるまで台帳には何も載らない。"""
    build_singles(cfg, history, SINGLE_IDS[:2])
    runner = ProbingBlockingRunner()
    svc = custom_service(cfg, history, manifest, runner)

    svc.start_custom_concat(SINGLE_IDS[:2])
    assert runner.entered.wait(10)
    assert manifest.list_entries() == []  # まだ連結中
    runner.release.set()
    assert wait_finished(svc).state == STATE_DONE
    assert len(manifest.list_entries()) == 1


def test_custom_concat_does_not_overwrite_chain_products(cfg, history, manifest):
    """既存の `c_*`（チェーン連結）に一切触れない。"""
    build_chain(cfg, history, CHAIN_IDS[:2])
    build_singles(cfg, history, SINGLE_IDS[:2])

    chain_svc = ConcatService(cfg, history, runner=RecordingRunner())
    chain_svc.start_concat(CHAIN_IDS[1])
    assert wait_finished(chain_svc).state == STATE_DONE
    chain_files = snapshot(sorted(cfg.concat_dir.glob("c_*.mp4")))
    assert chain_files

    svc = custom_service(cfg, history, manifest)
    svc.start_custom_concat(SINGLE_IDS[:2])
    assert wait_finished(svc).state == STATE_DONE
    assert_unchanged(chain_files)
