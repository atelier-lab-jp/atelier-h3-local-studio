"""1080p高品質化の統合（P6・設計書 §26）。

AppService から見た約束を確かめる:
- 成果物の名前が**入力から決まる**（台帳を持たない・同じ入力なら同じ名前）
- 一覧は**ファイルが在るか**だけで決まる
- 生成・連結・整理との排他（同じGPUを奪い合わせない）
- 1080p成果物は元動画と**独立して**整理できる
- 危ない入力（パス・種別・二重実行）を下位層でも断る

実モデルは起動しない（MockEngine ＋ 高品質化サービスの差し替え）。
"""

from __future__ import annotations

import dataclasses
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from app.core.app_service import AppService
from app.core.config import load_config
from app.core.contracts import BackendIdentity, JobSpec, JobStatus
from app.core.history import HistoryRecord
from app.core.naming import upscaled_filename
from app.core.upscale_service import STATE_RUNNING, UpscaleStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = PROJECT_ROOT / "app" / "assets" / "mock"
T0 = datetime(2026, 8, 10, 13, 0, 0).astimezone()

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax-H3-NF4",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)


class FakeUpscale:
    """UpscaleService の差し替え。**プロセスも ffmpeg も起動しない**。"""

    def __init__(self):
        self.started: list = []
        self._status = UpscaleStatus()
        self.available = (True, "利用できます")
        self.cancelled = False

    def availability(self):
        return self.available

    def start_upscale(self, request):
        self.started.append(request)
        self._status = UpscaleStatus(
            state=STATE_RUNNING,
            key=f"upscale-{len(self.started)}",
            source_key=request.source_key,
            source_label=request.label,
            total=request.num_frames,
        )
        return self._status.key

    def status(self):
        return self._status

    def cancel(self):
        self.cancelled = True
        return "高品質化を中止しています…"

    def shutdown(self, timeout: float = 5.0):
        self._status = UpscaleStatus()

    def finish(self):
        """実行が終わった状態にする（排他の解除を再現する）。"""
        self._status = UpscaleStatus()


@pytest.fixture
def cfg(tmp_path):
    base = dataclasses.replace(load_config(PROJECT_ROOT), data_root=tmp_path)
    for d in (base.outputs_dir, base.concat_dir, base.tmp_dir, base.logs_dir,
              base.upscaled_dir):
        d.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def app(cfg):
    """ディスパッチャを起動しない AppService（実モデルも実ワーカーも使わない）。"""
    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()
    if service.concat_manifest is not None:
        service.concat_manifest.load()
    service._upscale = FakeUpscale()
    yield service
    service.shutdown(timeout=5)


def add_clip(cfg, service: AppService, job_id: str) -> Path:
    """SUCCESS の個別動画を1本作る（実ファイルも置く。モック素材を使う）。"""
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
            identity=IDENTITY,
            execution_engine="mock",
            app_version=cfg.version,
            data_root=cfg.data_root,
            created_at=T0,
        )
    )
    service.history.mark_running(job_id, T0)
    shutil.copy(MOCK_DIR / "mock_56.mp4", out)
    shutil.copy(MOCK_DIR / "mock_56_last.png", last)
    service.history.mark_success(
        job_id,
        output_path=out,
        last_frame_path=last,
        seed_used=42,
        elapsed_sec=1.0,
        finished_at=T0,
    )
    return out


def make_upscaled(cfg, name: str) -> Path:
    path = cfg.upscaled_dir / name
    path.write_bytes(b"\x00" * 4096)
    return path


# ============================================================ 命名


def test_the_output_name_is_decided_by_the_input(cfg, app):
    """同じ動画からは必ず同じ名前になる（台帳が無くても迷子にならない）。"""
    add_clip(cfg, app, "v_20260810_130000_aaaa")
    first = app.upscaled_path_for("v_20260810_130000_aaaa", "clip")
    second = app.upscaled_path_for("v_20260810_130000_aaaa", "clip")
    assert first == second
    assert first.name == "u_clip_v_20260810_130000_aaaa_1080p.mp4"


def test_clip_and_chain_with_the_same_id_do_not_collide(cfg, app):
    """個別とチェーン連結は同じ job_id を使うので、**種類を名前に残す**。"""
    job_id = "v_20260810_130001_bbbb"
    clip = app.upscaled_path_for(job_id, "clip")
    chain = app.upscaled_path_for(job_id, "concat")
    assert clip != chain
    assert clip.name.startswith("u_clip_")
    assert chain.name.startswith("u_chain_")


def test_manual_concat_gets_its_own_prefix(cfg, app):
    """指定順連結（`cm_...`）は manual として区別する。"""
    path = app.upscaled_path_for("cm_20260810_130002_cccc", "concat")
    assert path.name.startswith("u_manual_")


@pytest.mark.parametrize(
    "bad_id",
    ["../etc/passwd", "a/b", "..", "", "x" * 100, "a\nb", "a\x00b"],
)
def test_dangerous_ids_never_produce_a_path(cfg, app, bad_id):
    """パス区切り・`..`・制御文字・長すぎる値は名前にしない（§26.3）。"""
    assert app.upscaled_path_for(bad_id, "clip") is None


def test_a_long_but_safe_id_is_shortened_deterministically():
    """長いIDは短縮＋短いハッシュ。別のIDが同じ名前にならない。"""
    long_a = "v_" + "a" * 58
    long_b = "v_" + "a" * 57 + "b"
    assert upscaled_filename("clip", long_a) == upscaled_filename("clip", long_a)
    assert upscaled_filename("clip", long_a) != upscaled_filename("clip", long_b)


# ============================================================ 実在方式


def test_the_listing_comes_from_the_files_on_disk(cfg, app):
    """一覧は台帳ではなく**実在するファイル**から作る（§26.4）。"""
    assert app.upscaled_rows() == []

    make_upscaled(cfg, "u_clip_v_20260810_130003_dddd_1080p.mp4")
    rows = app.upscaled_rows()
    assert len(rows) == 1
    assert rows[0].kind == "upscaled"
    assert rows[0].upscale_source_kind == "clip"
    assert rows[0].upscale_source_id == "v_20260810_130003_dddd"


def test_deleting_the_file_removes_it_from_the_listing(cfg, app):
    """Finder で消せば次の更新で一覧から消える（記録を残さない）。"""
    path = make_upscaled(cfg, "u_clip_v_20260810_130004_eeee_1080p.mp4")
    assert len(app.upscaled_rows()) == 1
    path.unlink()
    assert app.upscaled_rows() == []


def test_unrelated_files_are_ignored(cfg, app):
    """名前の形が違うファイルは拾わない（人が置いた物を巻き込まない）。"""
    (cfg.upscaled_dir / "メモ.txt").write_text("これは動画ではありません")
    (cfg.upscaled_dir / "random.mp4").write_bytes(b"\x00")
    (cfg.upscaled_dir / "u_bogus_x_1080p.mp4").write_bytes(b"\x00")
    assert app.upscaled_rows() == []


def test_a_symlink_is_not_listed(cfg, app, tmp_path):
    """シンボリックリンクは一覧に出さない（data の外を指せるため）。"""
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"\x00" * 1024)
    link = cfg.upscaled_dir / "u_clip_v_20260810_130005_ffff_1080p.mp4"
    link.symlink_to(outside)
    assert app.upscaled_rows() == []


def test_the_1080p_row_survives_the_source_being_trashed(cfg, app):
    """元動画を整理しても、1080p版は独立して残る（連動削除しない。§26.12）。"""
    add_clip(cfg, app, "v_20260810_130006_gggg")
    make_upscaled(cfg, "u_clip_v_20260810_130006_gggg_1080p.mp4")

    assert app.move_to_trash("v_20260810_130006_gggg", "clip")[0]
    rows = app.upscaled_rows()
    assert len(rows) == 1, "元動画と一緒に消えている"
    assert rows[0].upscale_source_id == "v_20260810_130006_gggg"


# ============================================================ 開始と拒否


def test_start_uses_the_id_only_and_resolves_paths_on_the_server(cfg, app):
    """UI から来るのは種別とIDだけ。元動画も出力先もサーバ側で決める（§26.3）。"""
    source = add_clip(cfg, app, "v_20260810_130007_hhhh")
    ok, message = app.start_upscale("v_20260810_130007_hhhh", "clip")

    assert ok, message
    request = app._upscale.started[-1]
    assert request.source_path == source
    assert request.output_path == cfg.upscaled_dir / (
        "u_clip_v_20260810_130007_hhhh_1080p.mp4"
    )
    assert request.output_path.parent == cfg.upscaled_dir


def test_an_existing_output_is_not_regenerated(cfg, app):
    """すでに正しい成果物があれば作り直さない（§26.9）。"""
    add_clip(cfg, app, "v_20260810_130008_iiii")
    make_upscaled(cfg, "u_clip_v_20260810_130008_iiii_1080p.mp4")

    ok, message = app.start_upscale("v_20260810_130008_iiii", "clip")
    assert ok is False
    assert "すでにあります" in message
    assert app._upscale.started == []


def test_an_empty_leftover_file_does_not_block_regeneration(cfg, app):
    """中身が空のファイルは「ある」とみなさない（作り直せる）。"""
    add_clip(cfg, app, "v_20260810_130009_jjjj")
    (cfg.upscaled_dir / "u_clip_v_20260810_130009_jjjj_1080p.mp4").write_bytes(b"")

    ok, _message = app.start_upscale("v_20260810_130009_jjjj", "clip")
    assert ok is True


def test_a_1080p_artifact_cannot_be_upscaled_again(cfg, app):
    """1080p成果物をさらに高品質化はしない（§26.13）。"""
    make_upscaled(cfg, "u_clip_v_20260810_130010_kkkk_1080p.mp4")
    ok, message = app.start_upscale("u_clip_v_20260810_130010_kkkk_1080p", "upscaled")

    assert ok is False
    assert "すでに1080p" in message
    assert app._upscale.started == []


def test_a_missing_video_is_refused(cfg, app):
    """記録にも無い動画は開始しない。"""
    ok, message = app.start_upscale("v_20260810_999999_zzzz", "clip")
    assert ok is False
    assert "見つかりません" in message


def test_unknown_kinds_are_refused(cfg, app):
    """種別の許可リストは下位層でも確かめる（UI 任せにしない）。"""
    add_clip(cfg, app, "v_20260810_130011_llll")
    ok, message = app.start_upscale("v_20260810_130011_llll", "../../etc")
    assert ok is False
    assert "高品質化できない種別" in message


def test_an_unavailable_service_explains_itself(cfg, app):
    """モデルが無いときは、開始せずに取得方法を伝える。"""
    add_clip(cfg, app, "v_20260810_130012_mmmm")
    app._upscale.available = (False, "高品質化のモデルファイルがありません。")

    ok, message = app.start_upscale("v_20260810_130012_mmmm", "clip")
    assert ok is False
    assert "モデルファイル" in message
    assert app._upscale.started == []


# ============================================================ 排他（§26.6）


def test_generation_waits_while_upscaling_but_keeps_the_queue(cfg, app):
    """高品質化中は生成を**開始しない**。待機ジョブは捨てずに保持する。"""
    add_clip(cfg, app, "v_20260810_130013_nnnn")
    app.start_upscale("v_20260810_130013_nnnn", "clip")

    assert app._generation_hold_reason() is not None
    assert "高品質化" in app._generation_hold_reason()

    # 受付そのものは止めない（投入はできて、開始だけ待つ）
    spec = app.build_spec(
        prompt="テスト用のプロンプト", num_frames=56, steps=4, seed_requested=1
    )
    view = app.queue.submit(spec)
    assert view.status is JobStatus.QUEUED
    assert app.queue._take_next_ready() is None, "高品質化中なのに生成が始まった"
    assert len(app.queue.queued_jobs()) == 1, "待機ジョブが捨てられている"

    app._upscale.finish()
    assert app._generation_hold_reason() is None


def test_concat_is_refused_while_upscaling(cfg, app):
    """高品質化中は連結を始めない（ffmpeg を奪い合わせない）。"""
    add_clip(cfg, app, "v_20260810_130014_oooo")
    app.start_upscale("v_20260810_130014_oooo", "clip")

    message = app.start_concat("v_20260810_130014_oooo")
    assert "高品質化" in message

    custom = app.start_custom_concat(
        ["v_20260810_130014_oooo", "v_20260810_130014_oooo"]
    )
    assert "高品質化" in custom


def test_trash_is_refused_while_upscaling(cfg, app):
    """高品質化中は整理しない（読んでいる最中のファイルを動かさない）。"""
    add_clip(cfg, app, "v_20260810_130015_pppp")
    add_clip(cfg, app, "v_20260810_130016_qqqq")
    app.start_upscale("v_20260810_130015_pppp", "clip")

    ok, message = app.move_to_trash("v_20260810_130016_qqqq", "clip")
    assert ok is False
    assert "高品質化" in message


def test_upscale_is_refused_while_generating(cfg, app, monkeypatch):
    """逆向きも同じ。生成中は高品質化を始めない。"""
    add_clip(cfg, app, "v_20260810_130017_rrrr")
    snapshot = app.snapshot()
    monkeypatch.setattr(
        app, "snapshot",
        lambda: dataclasses.replace(snapshot, current=object()),
    )

    ok, message = app.start_upscale("v_20260810_130017_rrrr", "clip")
    assert ok is False
    assert "実行中" in message
    assert app._upscale.started == []


def test_only_one_upscale_at_a_time_through_the_service(cfg, app):
    """2件目は下位層（UpscaleService）が断る。"""
    add_clip(cfg, app, "v_20260810_130018_ssss")
    add_clip(cfg, app, "v_20260810_130019_tttt")

    assert app.start_upscale("v_20260810_130018_ssss", "clip")[0]
    # 実行中なので、2件目は排他の理由で断られる
    ok, message = app.start_upscale("v_20260810_130019_tttt", "clip")
    assert ok is False
    assert "高品質化" in message


def test_concurrent_starts_only_launch_one(cfg, app):
    """同時に押されても開始は1件だけ（プロセス内ロック）。"""
    for i in range(4):
        add_clip(cfg, app, f"v_20260810_1400{i:02d}_aaaa")

    results = []
    barrier = threading.Barrier(4)

    def press(i):
        barrier.wait()
        results.append(app.start_upscale(f"v_20260810_1400{i:02d}_aaaa", "clip"))

    threads = [threading.Thread(target=press, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sum(1 for ok, _ in results if ok) == 1, results
    assert len(app._upscale.started) == 1


# ============================================================ 整理


def test_a_1080p_artifact_can_be_trashed_on_its_own(cfg, app):
    """1080p成果物だけを整理でき、元動画は残る（§26.12）。"""
    source = add_clip(cfg, app, "v_20260810_130020_uuuu")
    artifact = make_upscaled(cfg, "u_clip_v_20260810_130020_uuuu_1080p.mp4")

    ok, message = app.move_to_trash("u_clip_v_20260810_130020_uuuu_1080p", "upscaled")

    assert ok, message
    assert not artifact.exists()
    assert source.is_file(), "元動画まで消えている"
    assert (cfg.trash_dir / artifact.name).is_file()


def test_trashing_a_1080p_artifact_touches_nothing_else(cfg, app):
    """他の1080p成果物は巻き込まない。"""
    keep = make_upscaled(cfg, "u_clip_v_20260810_130021_vvvv_1080p.mp4")
    drop = make_upscaled(cfg, "u_chain_v_20260810_130021_vvvv_1080p.mp4")

    assert app.move_to_trash("u_chain_v_20260810_130021_vvvv_1080p", "upscaled")[0]
    assert keep.is_file()
    assert not drop.exists()


def test_a_bogus_artifact_name_cannot_be_trashed(cfg, app):
    """一覧に出ない名前は整理の対象にもならない（パスをでっち上げられない）。"""
    ok, message = app.move_to_trash("../../etc/passwd", "upscaled")
    assert ok is False
    assert "見つかりません" in message or "種別" in message
