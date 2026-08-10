"""アプリ内ゴミ箱（P5.3-B・設計書 §25）のテスト。

この機能が**やらないこと**を確かめるテストも含む。依存関係の検査・削除台帳・
復元情報・カスケード削除はいずれも「作らない」ことが仕様なので、
「親を消しても子は残る」「元動画を消しても連結動画は残る」を明示的に固定する。

書き込み先は `tmp_path` のみ（プロジェクトの `data/` には一切触れない）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from app.core.app_service import AppService
from app.core.concat_manifest import ConcatManifest, ManualConcatEntry
from app.core.config import load_config
from app.core.contracts import BackendIdentity, JobSpec
from app.core.history import HistoryRecord, HistoryStore
from app.core.trash_service import (
    TrashError,
    move_to_trash,
    trash_dir,
    validate_movable,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = PROJECT_ROOT / "app" / "assets" / "mock"
T0 = datetime(2026, 8, 10, 10, 0, 0).astimezone()

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax-H3-NF4",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def cfg(tmp_path: Path):
    base = load_config(PROJECT_ROOT)
    return dataclasses.replace(base, data_root=tmp_path / "data")


@pytest.fixture()
def app(cfg):
    """ディスパッチャを起動しない AppService（実モデルは使わない）。"""
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()
    if service.concat_manifest is not None:
        service.concat_manifest.load()
    return service


def add_clip(
    cfg,
    service: AppService,
    job_id: str,
    *,
    parent: str | None = None,
    with_png: bool = True,
    created_at: datetime | None = None,
) -> Path:
    """SUCCESS の個別動画を1本作る（実ファイルも置く）。"""
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
        job_type="single" if parent is None else "continuation",
        parent_id=parent,
        keyframe_path=None if parent is None else cfg.outputs_dir / f"{parent}_last.png",
    )
    service.history.add(
        HistoryRecord.from_job_spec(
            spec,
            identity=IDENTITY,
            execution_engine="mock",
            app_version=cfg.version,
            data_root=cfg.data_root,
            created_at=created_at or T0,
        )
    )
    service.history.mark_running(job_id, T0)
    shutil.copy(MOCK_DIR / "mock_56.mp4", out)
    if with_png:
        shutil.copy(MOCK_DIR / "mock_56_last.png", last)
    service.history.mark_success(
        job_id,
        output_path=out,
        last_frame_path=last if with_png else None,
        seed_used=42,
        elapsed_sec=1.0,
        finished_at=T0,
    )
    return out


def add_chain_concat(cfg, service: AppService, job_id: str, sources: list[str]) -> Path:
    """チェーン連結の成果物を作り、終端レコードへ記録する。"""
    path = cfg.concat_dir / f"c_{job_id}_{len(sources)}clips.mp4"
    shutil.copy(MOCK_DIR / "mock_56.mp4", path)
    service.history.mark_concat(job_id, concat_path=path, concat_sources=sources)
    return path


def add_manual_concat(cfg, service: AppService, concat_id: str, sources: list[str]) -> Path:
    """指定順連結の成果物を作り、台帳へ記録する。"""
    path = cfg.concat_dir / f"{concat_id}_{len(sources)}clips.mp4"
    shutil.copy(MOCK_DIR / "mock_56.mp4", path)
    service.concat_manifest.add(
        ManualConcatEntry(
            id=concat_id,
            created_at=T0,
            output_path=service.concat_manifest.to_relative(path),
            sources=tuple(sources),
            clips=len(sources),
            num_frames_total=56 * len(sources),
            fps=24,
            width=576,
            height=320,
            backend_id=IDENTITY.backend_id,
            model_id=IDENTITY.model_id,
            model_revision=IDENTITY.model_revision,
            execution_engine="mock",
            app_version=cfg.version,
        )
    )
    return path


def snapshot(paths: list[Path]) -> dict[Path, tuple[int, int]]:
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}


def assert_unchanged(before: dict[Path, tuple[int, int]]) -> None:
    for path, (size, mtime_ns) in before.items():
        assert path.is_file(), f"消えてはいけないファイルが消えました: {path.name}"
        assert path.stat().st_size == size, f"変更されました: {path.name}"
        assert path.stat().st_mtime_ns == mtime_ns, f"mtime が変わりました: {path.name}"


def trash_files(cfg) -> list[str]:
    d = trash_dir(cfg.data_root)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ================================================== 表示フィルタ（実在が正本）


def test_missing_clip_disappears_from_the_completed_list(cfg, app):
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    assert [r.job_id for r in app.completed_videos()] == ["v_20260810_100000_aaaa"]

    out.unlink()  # Finder で消した状況
    assert app.completed_videos() == []
    assert app.concat_candidates() == []
    assert app.completed_summary().total == 0


def test_missing_clip_disappears_from_the_success_history(cfg, app):
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    assert [r.job_id for r in app.history_rows("success")] == ["v_20260810_100000_aaaa"]

    out.unlink()
    assert app.history_rows("success") == []
    assert app.history_rows() == []  # 「すべて」からも消える
    # 履歴レコード自体は残す（内部の記録は削除しない）
    assert app.history.get("v_20260810_100000_aaaa") is not None


@pytest.mark.parametrize("status", ["failed", "canceled", "interrupted"])
def test_non_success_records_stay_without_any_file(cfg, app, status):
    """失敗・取消・中断は動画を持たない記録なので、これまでどおり残す。"""
    job_id = "v_20260810_100001_bbbb"
    spec = JobSpec(
        job_id=job_id,
        prompt="失敗する予定",
        num_frames=56,
        steps=4,
        seed_requested=None,
        output_path=cfg.outputs_dir / f"{job_id}.mp4",
        last_frame_path=cfg.outputs_dir / f"{job_id}_last.png",
        job_type="single",
        parent_id=None,
        keyframe_path=None,
    )
    app.history.add(
        HistoryRecord.from_job_spec(
            spec,
            identity=IDENTITY,
            execution_engine="mock",
            app_version=cfg.version,
            data_root=cfg.data_root,
            created_at=T0,
        )
    )
    if status == "canceled":
        app.history.mark_canceled(job_id, T0)
    else:
        app.history.mark_running(job_id, T0)
        app.history.mark_failed(
            job_id, error="擬似失敗", category="input", elapsed_sec=1.0, finished_at=T0
        )
        if status == "interrupted":
            pytest.skip("interrupted は起動時復元でのみ発生する")

    rows = app.history_rows()
    assert job_id in [r.job_id for r in rows], f"{status} が履歴から消えました"
    assert app.completed_videos() == []  # ③には出ない


def test_missing_chain_concat_disappears_but_record_stays(cfg, app):
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb", parent="v_20260810_100000_aaaa")
    concat = add_chain_concat(
        cfg, app, "v_20260810_100001_bbbb",
        ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"],
    )
    assert any(r.concat_kind == "chain" for r in app.concat_product_rows())

    concat.unlink()
    assert app.concat_product_rows() == []
    # 履歴の concat_path は消していない（内部記録は残す）
    assert app.history.get("v_20260810_100001_bbbb").concat_path is not None


def test_missing_manual_concat_disappears_but_entry_stays(cfg, app):
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb")
    concat = add_manual_concat(
        cfg, app, "cm_20260810_100002_0001",
        ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"],
    )
    assert any(r.concat_kind == "manual" for r in app.concat_product_rows())

    concat.unlink()
    assert app.concat_product_rows() == []
    assert len(app.concat_manifest.list_entries()) == 1  # 台帳は消さない


def test_restoring_the_file_brings_the_video_back(cfg, app, tmp_path):
    """正式パスへ戻すだけで再表示される（除外リストを持たないので当然そうなる）。"""
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    stash = tmp_path / out.name
    out.rename(stash)
    assert app.completed_videos() == []

    stash.rename(out)
    assert [r.job_id for r in app.completed_videos()] == ["v_20260810_100000_aaaa"]
    assert [r.job_id for r in app.concat_candidates()] == ["v_20260810_100000_aaaa"]


def test_reconcatenating_makes_the_chain_product_visible_again(cfg, app):
    """同じチェーンを作り直して正式パスにファイルができれば自然に再表示される。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb", parent="v_20260810_100000_aaaa")
    sources = ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"]
    concat = add_chain_concat(cfg, app, "v_20260810_100001_bbbb", sources)
    concat.unlink()
    assert app.concat_product_rows() == []

    shutil.copy(MOCK_DIR / "mock_56.mp4", concat)  # 連結し直した状態
    assert [r.job_id for r in app.concat_product_rows()] == ["v_20260810_100001_bbbb"]


# ================================================== ゴミ箱移動（正常系）


def test_moving_a_clip_moves_the_mp4_and_the_png(cfg, app):
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    png = cfg.outputs_dir / "v_20260810_100000_aaaa_last.png"
    assert png.is_file()

    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    assert ok, message
    assert "ゴミ箱へ移動しました" in message
    assert not out.exists() and not png.exists()
    assert trash_files(cfg) == [out.name, png.name]
    assert app.completed_videos() == []


def test_moving_a_clip_without_a_png_still_works(cfg, app):
    out = add_clip(cfg, app, "v_20260810_100000_aaaa", with_png=False)
    (cfg.outputs_dir / "v_20260810_100000_aaaa_last.png").unlink(missing_ok=True)

    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    assert ok, message
    assert trash_files(cfg) == [out.name]


def test_moving_a_chain_concat_moves_only_that_file(cfg, app):
    clip_a = add_clip(cfg, app, "v_20260810_100000_aaaa")
    clip_b = add_clip(cfg, app, "v_20260810_100001_bbbb", parent="v_20260810_100000_aaaa")
    concat = add_chain_concat(
        cfg, app, "v_20260810_100001_bbbb",
        ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"],
    )
    before = snapshot([clip_a, clip_b])

    ok, message = app.move_to_trash("v_20260810_100001_bbbb", "concat")
    assert ok, message
    assert not concat.exists()
    assert trash_files(cfg) == [concat.name]
    assert_unchanged(before)  # 素材には触れない


def test_moving_a_manual_concat_moves_only_that_file(cfg, app):
    clip_a = add_clip(cfg, app, "v_20260810_100000_aaaa")
    clip_b = add_clip(cfg, app, "v_20260810_100001_bbbb")
    concat = add_manual_concat(
        cfg, app, "cm_20260810_100002_0001",
        ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"],
    )
    before = snapshot([clip_a, clip_b])

    ok, message = app.move_to_trash("cm_20260810_100002_0001", "concat")
    assert ok, message
    assert not concat.exists()
    assert trash_files(cfg) == [concat.name]
    assert_unchanged(before)


def test_json_stores_are_untouched_by_a_move(cfg, app):
    """`history.json` と `concat_manifest.json` を**バイト単位で**変更しない。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb")
    add_manual_concat(
        cfg, app, "cm_20260810_100002_0001",
        ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"],
    )
    history_before = cfg.history_path.read_bytes()
    manifest_before = cfg.concat_manifest_path.read_bytes()

    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]
    assert app.move_to_trash("cm_20260810_100002_0001", "concat")[0]

    assert cfg.history_path.read_bytes() == history_before
    assert cfg.concat_manifest_path.read_bytes() == manifest_before


def test_trash_dir_is_created_only_when_moving(cfg, app):
    """一覧表示だけでは `data/trash/` を作らない（通常起動で data を触らない）。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    app.completed_videos()
    app.completed_summary()
    app.concat_product_rows()
    assert not trash_dir(cfg.data_root).exists()

    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]
    assert trash_dir(cfg.data_root).is_dir()


def test_name_collision_does_not_overwrite(cfg, app):
    """ゴミ箱に同名があっても上書きしない（別名で置く）。"""
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    destination = trash_dir(cfg.data_root)
    destination.mkdir(parents=True)
    existing = destination / out.name
    existing.write_bytes(b"before")

    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]
    assert existing.read_bytes() == b"before", "既存ファイルを上書きしました"
    names = trash_files(cfg)
    assert len(names) == 3  # 既存1 ＋ 別名の mp4 ＋ png
    assert any(n != out.name and n.endswith(".mp4") for n in names)


# ================================================== 入力検証・境界


def test_validate_rejects_paths_outside_data_root(cfg, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(TrashError, match="データ領域の外"):
        validate_movable(outside, data_root=cfg.data_root)


def test_validate_rejects_path_traversal(cfg):
    cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(TrashError):
        validate_movable(Path("outputs/../../escape.mp4"), data_root=cfg.data_root)


def test_validate_rejects_symlinks(cfg, tmp_path):
    cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real.mp4"
    real.write_bytes(b"x")
    link = cfg.outputs_dir / "link.mp4"
    link.symlink_to(real)

    with pytest.raises(TrashError, match="リンク"):
        validate_movable(link, data_root=cfg.data_root)
    assert real.is_file(), "リンク先が巻き添えになりました"


def test_validate_rejects_directories(cfg):
    cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(TrashError, match="ファイルではありません"):
        validate_movable(cfg.outputs_dir, data_root=cfg.data_root)


def test_validate_rejects_files_already_in_the_trash(cfg):
    destination = trash_dir(cfg.data_root)
    destination.mkdir(parents=True)
    already = destination / "old.mp4"
    already.write_bytes(b"x")
    with pytest.raises(TrashError, match="ゴミ箱の中"):
        validate_movable(already, data_root=cfg.data_root)


def test_validate_rejects_missing_files(cfg):
    cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(TrashError, match="すでに移動されたか"):
        validate_movable(cfg.outputs_dir / "nope.mp4", data_root=cfg.data_root)


def test_service_takes_ids_not_paths(cfg, app):
    """UI から来るのは種別とIDだけ。パス文字列を渡しても弾かれる。"""
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    ok, message = app.move_to_trash(str(out), "clip")
    assert not ok
    assert "見つかりません" in message
    assert out.is_file()


@pytest.mark.parametrize("kind", ["bogus", "trash", "outputs"])
def test_service_rejects_unknown_kinds(cfg, app, kind):
    """種別は clip / concat だけ（未知の種別でファイルを動かさない）。"""
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    ok, message = app.move_to_trash("v_20260810_100000_aaaa", kind)
    assert not ok
    assert "整理できない種別" in message
    assert out.is_file()


def test_service_defaults_to_clip_when_kind_is_empty(cfg, app):
    """種別が空なら個別動画として扱う（既存APIの既定値と揃える）。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "")
    assert ok, message


# ================================================== 失敗・ロールバック


def test_failed_move_leaves_everything_in_place(cfg, app, monkeypatch):
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    png = cfg.outputs_dir / "v_20260810_100000_aaaa_last.png"
    before = snapshot([out, png])

    def boom(src, dst):
        raise OSError("移動できません（擬似）")

    monkeypatch.setattr(os, "replace", boom)
    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    monkeypatch.undo()

    assert not ok
    assert "ゴミ箱へ移動できませんでした" in message
    assert_unchanged(before)
    assert [r.job_id for r in app.completed_videos()] == ["v_20260810_100000_aaaa"]


def test_second_file_failure_rolls_the_first_one_back(cfg, app, monkeypatch):
    """2ファイル目で失敗したら、1ファイル目を元の場所へ戻す。"""
    out = add_clip(cfg, app, "v_20260810_100000_aaaa")
    png = cfg.outputs_dir / "v_20260810_100000_aaaa_last.png"
    before = snapshot([out, png])
    real_replace = os.replace
    calls = {"n": 0}

    def fail_second(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # 2ファイル目の移動だけ失敗させる
            raise OSError("2つ目で失敗（擬似）")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_second)
    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    monkeypatch.undo()

    assert not ok, message
    assert out.is_file() and png.is_file(), "1ファイル目が戻っていません"
    assert_unchanged(before)
    assert trash_files(cfg) == []
    assert [r.job_id for r in app.completed_videos()] == ["v_20260810_100000_aaaa"]


def test_rollback_failure_logs_both_paths(cfg, app, monkeypatch, caplog):
    """戻すのにも失敗したら、元パスと移動先を ERROR ログへ残す。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    real_replace = os.replace
    calls = {"n": 0}

    def fail_second_and_rollback(src, dst):
        calls["n"] += 1
        if calls["n"] >= 2:  # 2ファイル目もロールバックも失敗させる
            raise OSError("失敗（擬似）")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_second_and_rollback)
    with caplog.at_level("ERROR", logger="atelier.trash"):
        ok, _message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    monkeypatch.undo()

    assert not ok
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Finder" in logged
    assert "v_20260810_100000_aaaa.mp4" in logged
    assert "trash" in logged


# ================================================== 二重操作・実行中


def test_moving_twice_is_reported_in_japanese(cfg, app):
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]

    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    assert not ok
    assert message == "動画はすでに移動されたか、見つかりません。"


def test_concurrent_moves_do_not_crash(cfg, app):
    """同時に押しても壊れない（成功は1回だけ）。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    results: list[bool] = []

    def attempt():
        results.append(app.move_to_trash("v_20260810_100000_aaaa", "clip")[0])

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sum(results) == 1, f"成功が1回ではありません: {results}"
    assert len(trash_files(cfg)) == 2  # mp4 と png


def test_move_is_refused_while_a_job_is_running(cfg, app, monkeypatch):
    """依存関係の検査ではなく、単純なファイル競合の回避（設計書 §25.4）。"""
    from app.core.contracts import JobStage, JobStatus, JobView, QueueSnapshot

    add_clip(cfg, app, "v_20260810_100000_aaaa")
    running = JobView(
        job_id="v_20260810_100009_zzzz",
        status=JobStatus.RUNNING,
        prompt_head="生成中",
        num_frames=56,
        steps=4,
        duration_label="2.33秒",
        seed_requested=None,
        seed_used=None,
        stage=JobStage.GENERATING,
        step=1,
        total_steps=4,
        queued_at=T0,
        started_at=T0,
    )
    engine_state = app.snapshot().engine_state  # 差し替える前に読んでおく
    busy = QueueSnapshot(
        engine_state=engine_state, current=running,
        queued=(), queue_size=0, last_finished=None, running=True,
    )
    monkeypatch.setattr(app, "snapshot", lambda: busy)
    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    monkeypatch.undo()

    assert not ok
    assert "実行中" in message and "完了してから" in message
    assert (cfg.outputs_dir / "v_20260810_100000_aaaa.mp4").is_file()


def test_move_is_refused_while_a_concat_is_running(cfg, app, monkeypatch):
    from types import SimpleNamespace

    add_clip(cfg, app, "v_20260810_100000_aaaa")
    monkeypatch.setattr(app, "concat_status", lambda: SimpleNamespace(running=True))
    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    monkeypatch.undo()

    assert not ok
    assert "実行中" in message


# ============================== 依存関係を管理しないことの証明（設計書 §25.5）


def test_a_parent_can_be_trashed_even_with_children(cfg, app):
    """子がいても親を移動できる（削除拒否の依存検査は**持たない**）。"""
    parent = add_clip(cfg, app, "v_20260810_100000_aaaa")
    child = add_clip(cfg, app, "v_20260810_100001_bbbb", parent="v_20260810_100000_aaaa")
    child_before = snapshot([child])

    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    assert ok, message
    assert not parent.exists()
    # 子は**そのまま残る**（カスケード削除しない）
    assert_unchanged(child_before)
    assert [r.job_id for r in app.completed_videos()] == ["v_20260810_100001_bbbb"]
    # 親子関係の記録も書き換えない
    assert app.history.get("v_20260810_100001_bbbb").parent_id == "v_20260810_100000_aaaa"


def test_trashing_a_source_keeps_existing_concat_products(cfg, app):
    """素材を消しても、できあがった連結動画は残って再生できる。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb")
    manual = add_manual_concat(
        cfg, app, "cm_20260810_100002_0001",
        ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"],
    )
    concat_before = snapshot([manual])

    ok, message = app.move_to_trash("v_20260810_100000_aaaa", "clip")
    assert ok, message

    assert_unchanged(concat_before)
    products = app.concat_product_rows()
    assert [r.job_id for r in products] == ["cm_20260810_100002_0001"]
    assert products[0].exists
    # sources の記録も書き換えない
    entry = app.concat_manifest.get("cm_20260810_100002_0001")
    assert entry.sources == ("v_20260810_100000_aaaa", "v_20260810_100001_bbbb")


def test_trashing_a_source_keeps_the_chain_concat_product(cfg, app):
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb", parent="v_20260810_100000_aaaa")
    sources = ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"]
    concat = add_chain_concat(cfg, app, "v_20260810_100001_bbbb", sources)
    concat_before = snapshot([concat])

    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]

    assert_unchanged(concat_before)
    assert any(r.concat_kind == "chain" for r in app.concat_product_rows())
    assert app.history.get("v_20260810_100001_bbbb").concat_sources == sources


def test_no_cascade_deletion_happens(cfg, app):
    """1本移動しても、ゴミ箱へ入るのはその動画の分だけ。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb", parent="v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100002_cccc")
    add_manual_concat(
        cfg, app, "cm_20260810_100003_0001",
        ["v_20260810_100000_aaaa", "v_20260810_100002_cccc"],
    )

    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]
    assert trash_files(cfg) == [
        "v_20260810_100000_aaaa.mp4",
        "v_20260810_100000_aaaa_last.png",
    ]
    remaining = {r.job_id for r in app.completed_videos()}
    assert remaining == {
        "v_20260810_100001_bbbb", "v_20260810_100002_cccc", "cm_20260810_100003_0001"
    }


def test_operations_needing_a_missing_source_fail_at_the_existing_boundary(cfg, app):
    """素材を消した後の連結は、既存の検証境界がきちんと断る。"""
    from app.core.history import HistoryError

    add_clip(cfg, app, "v_20260810_100000_aaaa")
    add_clip(cfg, app, "v_20260810_100001_bbbb")
    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]

    with pytest.raises(HistoryError, match="動画ファイルが見つかりません"):
        app.history.resolve_custom_concat(
            ["v_20260810_100000_aaaa", "v_20260810_100001_bbbb"]
        )


# ================================================== 配信境界


def test_trash_is_not_in_the_served_paths(cfg, app):
    """`data/trash/` を HTTP 配信対象へ入れない（`app/main.py` の設定と同じ形）。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]

    # main.py の allowed_paths と同じ並び（P6 で 1080p 成果物が加わった）
    allowed = [str(cfg.outputs_dir), str(cfg.concat_dir), str(cfg.upscaled_dir)]
    moved = trash_dir(cfg.data_root) / "v_20260810_100000_aaaa.mp4"
    assert moved.is_file()
    assert not any(str(moved).startswith(a) for a in allowed)

    source = (PROJECT_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "trash" not in source, "main.py が trash を配信対象にしています"


def test_finder_cannot_reach_the_trash(cfg, app):
    """Finder 表示は一覧から解決した動画だけ。ゴミ箱の中へは到達できない。"""
    add_clip(cfg, app, "v_20260810_100000_aaaa")
    assert app.move_to_trash("v_20260810_100000_aaaa", "clip")[0]
    moved = trash_dir(cfg.data_root) / "v_20260810_100000_aaaa.mp4"

    # 一覧から消えているので、行の解決自体ができない
    assert app.find_row("v_20260810_100000_aaaa", "clip").exists is False
    # 絶対パスを直接渡しても、UI はこの経路を使わない（サーバ側で行から解決する）
    assert moved.is_file()
    row = app.find_row("v_20260810_100000_aaaa", "clip")
    assert row.video_path != moved
