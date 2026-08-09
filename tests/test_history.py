"""HistoryStore のユニットテスト（設計書 §11・§17.1）。

すべて `tmp_path` 上で完結させる（プロジェクトの `data/` には一切書き込まない）。
"""

from __future__ import annotations

import dataclasses
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.contracts import BackendIdentity, JobSpec, JobStatus
from app.core.history import (
    HistoryError,
    HistoryRecord,
    HistoryStore,
)

IDENTITY = BackendIdentity(
    backend_id="minimax_h3",
    display_name="MiniMax-H3-NF4",
    model_id="DiffSynth-Studio/MiniMax-H3-NF4",
    model_revision="nf4-turbo4step-ckpt500",
)

T0 = datetime(2026, 8, 7, 10, 15, 30).astimezone()


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "outputs").mkdir(parents=True)
    (root / "concat").mkdir()
    return root


@pytest.fixture()
def store(data_root: Path) -> HistoryStore:
    return HistoryStore(data_root / "history.json", data_root)


def make_spec(
    data_root: Path,
    job_id: str = "v_20260807_101530_ab3f",
    *,
    prompt: str = "日本語プロンプト：小さな緑の恐竜がローカルで踊る",
    num_frames: int = 56,
    steps: int = 4,
    seed_requested: int | None = None,
    parent_id: str | None = None,
    job_type: str = "single",
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        prompt=prompt,
        num_frames=num_frames,
        steps=steps,
        seed_requested=seed_requested,
        output_path=data_root / "outputs" / f"{job_id}.mp4",
        last_frame_path=data_root / "outputs" / f"{job_id}_last.png",
        job_type=job_type,
        parent_id=parent_id,
    )


def make_record(data_root: Path, job_id: str = "v_20260807_101530_ab3f", **kw):
    spec = make_spec(data_root, job_id, **kw)
    return HistoryRecord.from_job_spec(
        spec,
        identity=IDENTITY,
        execution_engine="mock",
        app_version="1.0.0",
        data_root=data_root,
        created_at=T0,
    )


def read_doc(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 基本


def test_load_creates_history_file_when_missing(store: HistoryStore, data_root: Path):
    assert not store.path.exists()
    warnings = store.load()
    assert warnings == []
    assert store.path.is_file()
    doc = read_doc(store.path)
    assert doc == {"schema_version": HistoryStore.SCHEMA_VERSION, "records": []}
    assert store.list_records() == []


def test_add_get_and_persist(store: HistoryStore, data_root: Path):
    store.load()
    rec = make_record(data_root)
    store.add(rec)

    got = store.get(rec.id)
    assert got is not None
    assert got.id == rec.id
    assert got.status is JobStatus.QUEUED
    assert got.duration_label == "2.33秒"
    assert got.num_frames == 56 and got.fps == 24
    assert got.width == 576 and got.height == 320
    assert got.model_id == IDENTITY.model_id
    assert got.model_revision == IDENTITY.model_revision
    assert got.backend_id == "minimax_h3"
    assert got.execution_engine == "mock"
    assert got.app_version == "1.0.0"
    assert got.concat_path is None and got.concat_sources is None
    assert store.get("no_such_id") is None

    # 別インスタンスで読み直しても同じ内容
    reloaded = HistoryStore(store.path, data_root)
    assert reloaded.load() == []
    again = reloaded.get(rec.id)
    assert again is not None and again.prompt == rec.prompt
    assert again.created_at == rec.created_at


def test_add_duplicate_id_rejected(store: HistoryStore, data_root: Path):
    store.load()
    store.add(make_record(data_root))
    with pytest.raises(HistoryError, match="既に履歴に存在する"):
        store.add(make_record(data_root))


def test_list_records_order_and_filter(store: HistoryStore, data_root: Path):
    store.load()
    ids = ["v_20260807_101530_0001", "v_20260807_101531_0002", "v_20260807_101532_0003"]
    for job_id in ids:
        store.add(make_record(data_root, job_id))

    # 保存は投入順
    assert [r.id for r in store.list_records(newest_first=False)] == ids
    # 新しい順は反転
    assert [r.id for r in store.list_records(newest_first=True)] == list(reversed(ids))
    # 既定は newest_first=True
    assert [r.id for r in store.list_records()] == list(reversed(ids))

    store.mark_running(ids[1], T0)
    running = store.list_records(statuses=[JobStatus.RUNNING])
    assert [r.id for r in running] == [ids[1]]
    queued = store.list_records(newest_first=False, statuses=[JobStatus.QUEUED])
    assert [r.id for r in queued] == [ids[0], ids[2]]


def test_japanese_prompt_is_readable_in_file(store: HistoryStore, data_root: Path):
    store.load()
    prompt = "日本語のプロンプト。かわいい恐竜が「こんにちは」と言う。"
    store.add(make_record(data_root, prompt=prompt))

    raw = store.path.read_text(encoding="utf-8")
    assert prompt in raw  # ensure_ascii=False でそのまま読める
    assert "\\u" not in raw
    assert store.get("v_20260807_101530_ab3f").prompt == prompt


def test_returned_records_are_isolated_copies(store: HistoryStore, data_root: Path):
    store.load()
    rec = make_record(data_root)
    store.add(rec)
    got = store.get(rec.id)
    # frozen なので代入自体が拒否される
    with pytest.raises(Exception):
        got.status = JobStatus.SUCCESS
    assert store.get(rec.id).status is JobStatus.QUEUED


# ---------------------------------------------------------------- 状態遷移


def test_success_flow_transitions(store: HistoryStore, data_root: Path):
    store.load()
    rec = make_record(data_root)
    store.add(rec)

    started = T0 + timedelta(seconds=1)
    running = store.mark_running(rec.id, started)
    assert running.status is JobStatus.RUNNING
    assert running.started_at == started

    finished = T0 + timedelta(seconds=120)
    out = data_root / "outputs" / f"{rec.id}.mp4"
    last = data_root / "outputs" / f"{rec.id}_last.png"
    done = store.mark_success(
        rec.id,
        output_path=out,
        last_frame_path=last,
        seed_used=42,
        elapsed_sec=119.5,
        finished_at=finished,
    )
    assert done.status is JobStatus.SUCCESS
    assert done.seed_used == 42
    assert done.elapsed_sec == 119.5
    assert done.finished_at == finished
    assert done.output_path == f"outputs/{rec.id}.mp4"
    assert done.error is None


def test_failed_and_canceled_transitions(store: HistoryStore, data_root: Path):
    store.load()
    a = make_record(data_root, "v_20260807_101530_000a")
    b = make_record(data_root, "v_20260807_101531_000b")
    store.add(a)
    store.add(b)

    store.mark_running(a.id, T0)
    failed = store.mark_failed(
        a.id,
        error="モック失敗",
        category="input",
        elapsed_sec=1.0,
        finished_at=T0 + timedelta(seconds=2),
    )
    assert failed.status is JobStatus.FAILED
    assert failed.error == "モック失敗"
    assert failed.error_category == "input"
    assert failed.output_path is None  # 失敗時は成果物なし（§11.2）

    canceled = store.mark_canceled(b.id, T0 + timedelta(seconds=3))
    assert canceled.status is JobStatus.CANCELED
    assert canceled.finished_at == T0 + timedelta(seconds=3)


def test_invalid_transitions_rejected(store: HistoryStore, data_root: Path):
    store.load()
    rec = make_record(data_root)
    store.add(rec)

    # QUEUED → SUCCESS は不可（RUNNING を経由する）
    with pytest.raises(HistoryError, match="許可されていない状態遷移"):
        store.mark_success(
            rec.id,
            output_path=data_root / "outputs" / f"{rec.id}.mp4",
            last_frame_path=None,
            seed_used=1,
            elapsed_sec=1.0,
            finished_at=T0,
        )

    store.mark_running(rec.id, T0)
    store.mark_success(
        rec.id,
        output_path=data_root / "outputs" / f"{rec.id}.mp4",
        last_frame_path=None,
        seed_used=1,
        elapsed_sec=1.0,
        finished_at=T0,
    )
    # SUCCESS は終端。RUNNING へ戻せない
    with pytest.raises(HistoryError, match="許可されていない状態遷移"):
        store.mark_running(rec.id, T0)
    with pytest.raises(HistoryError, match="許可されていない状態遷移"):
        store.mark_canceled(rec.id, T0)
    assert store.get(rec.id).status is JobStatus.SUCCESS


def test_mark_unknown_job_raises(store: HistoryStore, data_root: Path):
    store.load()
    with pytest.raises(HistoryError, match="履歴に存在しないジョブID"):
        store.mark_running("v_20260807_101530_zzzz", T0)
    with pytest.raises(HistoryError, match="履歴に存在しないジョブID"):
        store.mark_canceled("v_20260807_101530_zzzz", T0)


# ---------------------------------------------------------------- パス


def test_paths_are_stored_relative(store: HistoryStore, data_root: Path):
    store.load()
    rec = make_record(data_root)
    store.add(rec)
    assert rec.output_path == f"outputs/{rec.id}.mp4"
    assert rec.last_frame_path == f"outputs/{rec.id}_last.png"

    store.mark_running(rec.id, T0)
    store.mark_success(
        rec.id,
        output_path=data_root / "outputs" / f"{rec.id}.mp4",
        last_frame_path=data_root / "outputs" / f"{rec.id}_last.png",
        seed_used=7,
        elapsed_sec=1.0,
        finished_at=T0,
    )
    doc = read_doc(store.path)
    saved = doc["records"][0]
    assert saved["output_path"] == f"outputs/{rec.id}.mp4"
    assert saved["last_frame_path"] == f"outputs/{rec.id}_last.png"
    assert not saved["output_path"].startswith("/")

    absolute = store.to_absolute(saved["output_path"])
    assert absolute == data_root / "outputs" / f"{rec.id}.mp4"
    assert store.to_absolute(None) is None
    assert store.to_relative(None) is None


def test_paths_outside_data_root_rejected(store: HistoryStore, tmp_path: Path, data_root: Path):
    store.load()
    outside = tmp_path / "elsewhere" / "x.mp4"
    with pytest.raises(HistoryError, match="データ領域の外"):
        store.to_relative(outside)

    rec = make_record(data_root)
    store.add(rec)
    store.mark_running(rec.id, T0)
    with pytest.raises(HistoryError, match="データ領域の外"):
        store.mark_success(
            rec.id,
            output_path=outside,
            last_frame_path=None,
            seed_used=1,
            elapsed_sec=1.0,
            finished_at=T0,
        )
    # 失敗した更新で状態が壊れていないこと
    assert store.get(rec.id).status is JobStatus.RUNNING

    # from_job_spec も data_root 外を拒否する
    bad_spec = JobSpec(
        job_id="v_20260807_101530_bad0",
        prompt="p",
        num_frames=56,
        steps=4,
        seed_requested=None,
        output_path=outside,
        last_frame_path=outside.with_suffix(".png"),
    )
    with pytest.raises(HistoryError, match="データ領域の外"):
        HistoryRecord.from_job_spec(
            bad_spec,
            identity=IDENTITY,
            execution_engine="mock",
            app_version="1.0.0",
            data_root=data_root,
            created_at=T0,
        )


# ---------------------------------------------------------------- 原子的保存 / .bak


def test_atomic_save_leaves_no_tmp(store: HistoryStore, data_root: Path):
    store.load()
    store.add(make_record(data_root))
    store.mark_running("v_20260807_101530_ab3f", T0)
    leftovers = sorted(p.name for p in data_root.glob("*.tmp"))
    assert leftovers == []
    assert store.path.is_file()


def test_backup_written_only_on_healthy_load(store: HistoryStore, data_root: Path):
    bak = store.backup_path
    store.load()  # 初回（ファイル無し）は .bak を作らない
    assert not bak.exists()

    store.add(make_record(data_root, "v_20260807_101530_0001"))
    assert not bak.exists()  # セッション中の更新では .bak を触らない

    # 2回目の起動: 正常に読めたので .bak が作られる
    store2 = HistoryStore(store.path, data_root)
    assert store2.load() == []
    assert bak.is_file()
    assert [r["id"] for r in read_doc(bak)["records"]] == ["v_20260807_101530_0001"]

    # セッション中の追加では .bak が更新されない
    store2.add(make_record(data_root, "v_20260807_101531_0002"))
    assert [r["id"] for r in read_doc(bak)["records"]] == ["v_20260807_101530_0001"]
    assert len(read_doc(store.path)["records"]) == 2


def test_corrupt_current_must_not_overwrite_good_backup(
    store: HistoryStore, data_root: Path
):
    """最重要: 破損した history.json が正常な .bak を上書きしないこと。"""
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))

    store2 = HistoryStore(store.path, data_root)
    store2.load()  # 正常 → .bak 作成
    good_bak_text = store.backup_path.read_text(encoding="utf-8")

    store.path.write_text("{壊れたJSON", encoding="utf-8")

    store3 = HistoryStore(store.path, data_root)
    warnings = store3.load()

    assert store.backup_path.read_text(encoding="utf-8") == good_bak_text
    assert [r.id for r in store3.list_records()] == ["v_20260807_101530_0001"]
    assert warnings and any("退避" in w for w in warnings)


def test_corrupt_current_quarantined_and_recovered_from_backup(
    store: HistoryStore, data_root: Path
):
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))
    HistoryStore(store.path, data_root).load()  # .bak を作る

    store.path.write_text("これはJSONではない", encoding="utf-8")

    fresh = HistoryStore(store.path, data_root)
    warnings = fresh.load()

    corrupts = sorted(data_root.glob("history_corrupt_*.json"))
    assert len(corrupts) == 1
    assert corrupts[0].read_text(encoding="utf-8") == "これはJSONではない"
    assert any("復旧" in w for w in warnings)
    assert [r.id for r in fresh.list_records()] == ["v_20260807_101530_0001"]
    # 復旧内容で history.json が作り直されている
    assert [r["id"] for r in read_doc(store.path)["records"]] == [
        "v_20260807_101530_0001"
    ]


def test_both_corrupt_starts_empty_with_warning(store: HistoryStore, data_root: Path):
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))
    HistoryStore(store.path, data_root).load()

    store.path.write_text("{壊れ", encoding="utf-8")
    store.backup_path.write_text("{これも壊れ", encoding="utf-8")

    fresh = HistoryStore(store.path, data_root)
    warnings = fresh.load()

    assert fresh.list_records() == []
    assert any("復旧できませんでした" in w for w in warnings)
    assert len(sorted(data_root.glob("history_corrupt_*.json"))) == 1
    assert read_doc(store.path)["records"] == []
    # 使えない .bak は残したまま（勝手に消さない）
    assert store.backup_path.exists()


def test_missing_backup_starts_empty_with_warning(store: HistoryStore, data_root: Path):
    store.path.write_text("{壊れ", encoding="utf-8")
    warnings = store.load()
    assert store.list_records() == []
    assert any("バックアップが見つからない" in w for w in warnings)
    assert len(sorted(data_root.glob("history_corrupt_*.json"))) == 1


def test_schema_version_mismatch_is_corruption(store: HistoryStore, data_root: Path):
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))
    HistoryStore(store.path, data_root).load()  # 正常な .bak

    doc = read_doc(store.path)
    doc["schema_version"] = 99
    store.path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    fresh = HistoryStore(store.path, data_root)
    warnings = fresh.load()
    assert any("schema_version" in w for w in warnings)
    # .bak から復旧される
    assert [r.id for r in fresh.list_records()] == ["v_20260807_101530_0001"]


def test_broken_record_is_corruption(store: HistoryStore, data_root: Path):
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))
    HistoryStore(store.path, data_root).load()

    doc = read_doc(store.path)
    del doc["records"][0]["status"]
    store.path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    fresh = HistoryStore(store.path, data_root)
    warnings = fresh.load()
    assert warnings
    assert [r.id for r in fresh.list_records()] == ["v_20260807_101530_0001"]


# ---------------------------------------------------------------- 起動時復元


def test_startup_recover_marks_queued_and_running_interrupted(
    store: HistoryStore, data_root: Path
):
    store.load()
    queued = make_record(data_root, "v_20260807_101530_0001")
    running = make_record(data_root, "v_20260807_101531_0002")
    done = make_record(data_root, "v_20260807_101532_0003")
    failed = make_record(data_root, "v_20260807_101533_0004")
    for r in (queued, running, done, failed):
        store.add(r)

    store.mark_running(running.id, T0)
    store.mark_running(done.id, T0)
    store.mark_success(
        done.id,
        output_path=data_root / "outputs" / f"{done.id}.mp4",
        last_frame_path=None,
        seed_used=5,
        elapsed_sec=3.0,
        finished_at=T0,
    )
    store.mark_running(failed.id, T0)
    store.mark_failed(
        failed.id, error="失敗", category=None, elapsed_sec=1.0, finished_at=T0
    )

    # 再起動をシミュレート
    fresh = HistoryStore(store.path, data_root)
    fresh.load()
    assert fresh.startup_recover() == 2

    assert fresh.get(queued.id).status is JobStatus.INTERRUPTED
    assert fresh.get(running.id).status is JobStatus.INTERRUPTED
    assert "アプリ終了により中断" in fresh.get(running.id).error
    # 終端状態は変更されない
    assert fresh.get(done.id).status is JobStatus.SUCCESS
    assert fresh.get(done.id).output_path == f"outputs/{done.id}.mp4"
    assert fresh.get(failed.id).status is JobStatus.FAILED
    assert fresh.get(failed.id).error == "失敗"

    # 自動再投入しない（QUEUED / RUNNING が残らない）
    assert fresh.list_records(statuses=[JobStatus.QUEUED, JobStatus.RUNNING]) == []
    # 2回目は対象なし
    assert fresh.startup_recover() == 0
    # 永続化されている
    again = HistoryStore(store.path, data_root)
    again.load()
    assert again.get(queued.id).status is JobStatus.INTERRUPTED


# ---------------------------------------------------------------- チェーン


def _add_chain(store: HistoryStore, data_root: Path, ids: list[str]) -> None:
    parent = None
    for job_id in ids:
        store.add(
            make_record(
                data_root,
                job_id,
                parent_id=parent,
                job_type="single" if parent is None else "continuation",
            )
        )
        parent = job_id


def test_resolve_chain_returns_root_first(store: HistoryStore, data_root: Path):
    store.load()
    ids = ["v_20260807_101530_0001", "v_20260807_101531_0002", "v_20260807_101532_0003"]
    _add_chain(store, data_root, ids)

    chain = store.resolve_chain(ids[-1])
    assert [r.id for r in chain] == ids  # root → 自分
    assert [r.id for r in store.resolve_chain(ids[0])] == [ids[0]]
    assert [r.id for r in store.resolve_chain(ids[1])] == ids[:2]


def test_resolve_chain_unknown_id(store: HistoryStore, data_root: Path):
    store.load()
    with pytest.raises(HistoryError, match="履歴に存在しないジョブID"):
        store.resolve_chain("v_20260807_101530_zzzz")


def test_resolve_chain_missing_parent(store: HistoryStore, data_root: Path):
    store.load()
    store.add(
        make_record(
            data_root,
            "v_20260807_101531_0002",
            parent_id="v_20260807_101530_0001",
            job_type="continuation",
        )
    )
    with pytest.raises(HistoryError, match="親ジョブが履歴に見つかりません"):
        store.resolve_chain("v_20260807_101531_0002")


def test_resolve_chain_detects_cycle(store: HistoryStore, data_root: Path):
    store.load()
    a = "v_20260807_101530_0001"
    b = "v_20260807_101531_0002"
    store.add(make_record(data_root, a, parent_id=b, job_type="continuation"))
    store.add(make_record(data_root, b, parent_id=a, job_type="continuation"))
    with pytest.raises(HistoryError, match="循環"):
        store.resolve_chain(a)


def test_resolve_chain_detects_self_cycle(store: HistoryStore, data_root: Path):
    store.load()
    a = "v_20260807_101530_0001"
    store.add(make_record(data_root, a, parent_id=a, job_type="continuation"))
    with pytest.raises(HistoryError, match="循環"):
        store.resolve_chain(a)


def test_resolve_chain_depth_limit(store: HistoryStore, data_root: Path):
    store.load()
    ids = [f"v_20260807_1015{i:02d}_00{i:02d}" for i in range(25)]
    _add_chain(store, data_root, ids)
    with pytest.raises(HistoryError, match="長すぎます"):
        store.resolve_chain(ids[-1])


# ---------------------------------------------------------------- 同時アクセス


def test_concurrent_add_and_mark(store: HistoryStore, data_root: Path):
    store.load()
    threads = 8
    per_thread = 10
    errors: list[BaseException] = []

    def worker(t: int) -> None:
        try:
            for i in range(per_thread):
                job_id = f"v_20260807_1015{t:02d}_{i:04d}"
                store.add(make_record(data_root, job_id))
                store.mark_running(job_id, T0)
                store.mark_success(
                    job_id,
                    output_path=data_root / "outputs" / f"{job_id}.mp4",
                    last_frame_path=None,
                    seed_used=i,
                    elapsed_sec=0.1,
                    finished_at=T0,
                )
        except BaseException as e:  # pragma: no cover - 失敗時の診断用
            errors.append(e)

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert errors == []
    records = store.list_records()
    assert len(records) == threads * per_thread
    assert all(r.status is JobStatus.SUCCESS for r in records)

    # ディスク上の内容も壊れていない
    doc = read_doc(store.path)
    assert doc["schema_version"] == HistoryStore.SCHEMA_VERSION
    assert len(doc["records"]) == threads * per_thread
    assert len({r["id"] for r in doc["records"]}) == threads * per_thread
    assert sorted(p.name for p in data_root.glob("*.tmp")) == []


# ---------------------------------------------------------------- dict 変換


def test_record_dict_roundtrip(data_root: Path):
    rec = make_record(data_root, seed_requested=42)
    d = rec.to_dict()
    assert d["status"] == "queued"
    assert d["created_at"].startswith("2026-08-07T10:15:30")
    assert "+" in d["created_at"] or "-" in d["created_at"][10:]  # オフセット付き
    assert set(d) >= {
        "id",
        "type",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "prompt",
        "duration_label",
        "num_frames",
        "fps",
        "width",
        "height",
        "steps",
        "seed_requested",
        "seed_used",
        "parent_id",
        "keyframe_path",
        "output_path",
        "last_frame_path",
        "concat_path",
        "concat_sources",
        "elapsed_sec",
        "error",
        "error_category",
        "execution_engine",
        "backend_id",
        "model_id",
        "model_revision",
        "backend_params",
        "app_version",
    }
    assert HistoryRecord.from_dict(d) == rec


def test_from_dict_rejects_unknown_status(data_root: Path):
    d = make_record(data_root).to_dict()
    d["status"] = "zombie"
    with pytest.raises(HistoryError, match="ジョブ状態が不正"):
        HistoryRecord.from_dict(d)


# ---------------------------------------------------------------- 相互レビュー由来の回帰テスト


def test_to_absolute_rejects_paths_outside_data_root(store: HistoryStore, data_root: Path):
    """履歴が手編集・部分破損していても data_root の外を指さない（下位層での境界検証）。"""
    store.load()
    assert store.to_absolute("outputs/ok.mp4") == (data_root / "outputs/ok.mp4").resolve()
    assert store.to_absolute(None) is None
    assert store.to_absolute("") is None
    # 絶対パスが記録されていた場合
    assert store.to_absolute("/etc/passwd") is None
    # data_root を脱出する相対パス
    assert store.to_absolute("../../../../etc/hosts") is None
    assert store.to_absolute("outputs/../../outside.mp4") is None


def test_absolute_path_in_file_is_not_exposed(store: HistoryStore, data_root: Path):
    """外部由来の history.json に絶対パスが入っていても UI へ渡さない。"""
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))
    doc = read_doc(store.path)
    doc["records"][0]["output_path"] = "/etc/passwd"
    store.path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    fresh = HistoryStore(store.path, data_root)
    fresh.load()
    rec = fresh.get("v_20260807_101530_0001")
    assert rec.output_path == "/etc/passwd"          # 記録は保つ（改変しない）
    assert fresh.to_absolute(rec.output_path) is None  # UI へは渡さない


def test_save_failure_rolls_back_memory(store: HistoryStore, data_root: Path, monkeypatch):
    """保存に失敗したらメモリ側も巻き戻す（メモリ＝ディスクの不変条件）。"""
    store.load()
    rec = make_record(data_root, "v_20260807_101530_0001")
    store.add(rec)
    store.mark_running(rec.id, datetime(2026, 8, 7, 10, 16, tzinfo=timezone(timedelta(hours=9))))

    real_open = open

    def _boom(path, *a, **kw):
        if str(path).endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _boom)
    with pytest.raises(HistoryError):
        store.mark_success(
            rec.id,
            output_path=data_root / "outputs" / f"{rec.id}.mp4",
            last_frame_path=None,
            seed_used=42,
            elapsed_sec=1.0,
            finished_at=datetime(2026, 8, 7, 10, 20, tzinfo=timezone(timedelta(hours=9))),
        )
    monkeypatch.undo()

    # メモリはディスクと同じ RUNNING のまま
    assert store.get(rec.id).status is JobStatus.RUNNING
    assert read_doc(store.path)["records"][0]["status"] == "running"


def test_add_failure_rolls_back_memory(store: HistoryStore, data_root: Path, monkeypatch):
    store.load()
    real_open = open

    def _boom(path, *a, **kw):
        if str(path).endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _boom)
    with pytest.raises(HistoryError):
        store.add(make_record(data_root, "v_20260807_101530_0009"))
    monkeypatch.undo()

    assert store.get("v_20260807_101530_0009") is None
    assert read_doc(store.path)["records"] == []


def test_second_load_does_not_pollute_backup(store: HistoryStore, data_root: Path):
    """セッション中に load() を再度呼んでも .bak は起動時点のスナップショットのまま。"""
    store.load()
    store.add(make_record(data_root, "v_20260807_101530_0001"))

    session = HistoryStore(store.path, data_root)
    session.load()  # 起動時: .bak 作成（1件）
    assert [r["id"] for r in read_doc(session.backup_path)["records"]] == [
        "v_20260807_101530_0001"
    ]

    session.add(make_record(data_root, "v_20260807_101531_0002"))
    session.load()  # セッション中の再読込
    assert [r["id"] for r in read_doc(session.backup_path)["records"]] == [
        "v_20260807_101530_0001"
    ]


def test_tmp_paths_are_unique_per_write(store: HistoryStore, data_root: Path):
    """tmp 名が固定だと、同一 data_root の別インスタンスと奪い合って保存が落ちる。"""
    names = {store._new_tmp_path().name for _ in range(20)}
    assert len(names) == 20
    other = HistoryStore(store.path, data_root)
    assert store._new_tmp_path().name != other._new_tmp_path().name


def test_two_instances_write_without_losing_records(store: HistoryStore, data_root: Path):
    """同一 data_root の2インスタンスが並行更新しても保存が黙って落ちない。"""
    store.load()
    other = HistoryStore(store.path, data_root)
    other.load()

    errors: list[Exception] = []

    def _writer(s: HistoryStore, prefix: str):
        for i in range(15):
            try:
                s.add(make_record(data_root, f"v_20260807_1015{prefix}_{i:04d}"))
            except HistoryError as e:  # 保存失敗は許容しない
                errors.append(e)

    threads = [
        threading.Thread(target=_writer, args=(store, "30")),
        threading.Thread(target=_writer, args=(other, "31")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"保存が失敗しました: {errors[:3]}"
    assert not list(data_root.glob("*.tmp")), "tmp が残っています"
    read_doc(store.path)  # 破損していないこと


# ------------------------------------------------ P4: 連結可能なチェーンの解決


def _success_chain(
    store: HistoryStore,
    data_root: Path,
    ids: list[str],
    *,
    create_files: bool = True,
    overrides: dict[str, dict] | None = None,
) -> None:
    """SUCCESS の親子チェーンを作る（成果物ファイルも実際に置く）。"""
    parent = None
    for job_id in ids:
        rec = make_record(
            data_root,
            job_id,
            parent_id=parent,
            job_type="single" if parent is None else "continuation",
        )
        if overrides and job_id in overrides:
            rec = dataclasses.replace(rec, **overrides[job_id])
        store.add(rec)
        store.mark_running(job_id, T0)
        out = data_root / "outputs" / f"{job_id}.mp4"
        last = data_root / "outputs" / f"{job_id}_last.png"
        if create_files:
            out.write_bytes(b"fake mp4")
            last.write_bytes(b"fake png")
        store.mark_success(
            job_id,
            output_path=out,
            last_frame_path=last,
            seed_used=42,
            elapsed_sec=1.0,
            finished_at=T0,
        )
        parent = job_id


CHAIN_IDS = [
    "v_20260807_101530_0001",
    "v_20260807_101531_0002",
    "v_20260807_101532_0003",
    "v_20260807_101533_0004",
]


def test_resolve_concat_chain_two_clips(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    chain = store.resolve_concat_chain(CHAIN_IDS[1])
    assert [r.id for r in chain] == CHAIN_IDS[:2]  # root → 選択ノード


def test_resolve_concat_chain_three_and_more(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS)
    assert [r.id for r in store.resolve_concat_chain(CHAIN_IDS[2])] == CHAIN_IDS[:3]
    assert [r.id for r in store.resolve_concat_chain(CHAIN_IDS[3])] == CHAIN_IDS


def test_resolve_concat_chain_excludes_descendants(store: HistoryStore, data_root: Path):
    """選択ノードより後の子孫は含めない（親を遡るので構造的に含まれない）。"""
    store.load()
    _success_chain(store, data_root, CHAIN_IDS)
    chain = store.resolve_concat_chain(CHAIN_IDS[1])
    assert [r.id for r in chain] == CHAIN_IDS[:2]
    assert CHAIN_IDS[2] not in {r.id for r in chain}
    assert CHAIN_IDS[3] not in {r.id for r in chain}


def test_resolve_concat_chain_branch_uses_own_ancestors(
    store: HistoryStore, data_root: Path
):
    """同じ親から2本枝分かれしていても、選んだ枝の祖先だけを返す。"""
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    # 0001 を親にする別の子（0002 の兄弟）
    sibling = "v_20260807_101540_00aa"
    rec = make_record(data_root, sibling, parent_id=CHAIN_IDS[0], job_type="continuation")
    store.add(rec)
    store.mark_running(sibling, T0)
    out = data_root / "outputs" / f"{sibling}.mp4"
    out.write_bytes(b"fake mp4")
    store.mark_success(
        sibling,
        output_path=out,
        last_frame_path=None,
        seed_used=1,
        elapsed_sec=1.0,
        finished_at=T0,
    )
    assert [r.id for r in store.resolve_concat_chain(sibling)] == [
        CHAIN_IDS[0],
        sibling,
    ]
    assert [r.id for r in store.resolve_concat_chain(CHAIN_IDS[1])] == CHAIN_IDS[:2]


def test_resolve_concat_chain_requires_two_clips(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:1])
    with pytest.raises(HistoryError, match="連結には2本以上"):
        store.resolve_concat_chain(CHAIN_IDS[0])


def test_resolve_concat_chain_rejects_non_success(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    # 3本目は RUNNING のまま
    store.add(
        make_record(data_root, CHAIN_IDS[2], parent_id=CHAIN_IDS[1], job_type="continuation")
    )
    store.mark_running(CHAIN_IDS[2], T0)
    with pytest.raises(HistoryError, match="成功していない動画") as e:
        store.resolve_concat_chain(CHAIN_IDS[2])
    assert CHAIN_IDS[2] in str(e.value)
    assert "生成中" in str(e.value)


def test_resolve_concat_chain_rejects_failed_ancestor(store: HistoryStore, data_root: Path):
    store.load()
    a, b = CHAIN_IDS[0], CHAIN_IDS[1]
    store.add(make_record(data_root, a))
    store.mark_running(a, T0)
    store.mark_failed(a, error="失敗", category="pipeline", elapsed_sec=1.0, finished_at=T0)
    rec = make_record(data_root, b, parent_id=a, job_type="continuation")
    store.add(rec)
    store.mark_running(b, T0)
    out = data_root / "outputs" / f"{b}.mp4"
    out.write_bytes(b"x")
    store.mark_success(
        b, output_path=out, last_frame_path=None, seed_used=1, elapsed_sec=1.0, finished_at=T0
    )
    with pytest.raises(HistoryError, match="成功していない動画") as e:
        store.resolve_concat_chain(b)
    assert "失敗" in str(e.value)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("backend_id", "other_backend", "生成バックエンド"),
        ("model_id", "OtherOrg/OtherModel", "モデル"),
        ("width", 1280, "幅"),
        ("height", 720, "高さ"),
        ("fps", 30, "fps"),
    ],
)
def test_resolve_concat_chain_rejects_incompatible(
    store: HistoryStore, data_root: Path, field: str, value, expected: str
):
    store.load()
    _success_chain(
        store, data_root, CHAIN_IDS[:2], overrides={CHAIN_IDS[1]: {field: value}}
    )
    with pytest.raises(HistoryError, match=expected) as e:
        store.resolve_concat_chain(CHAIN_IDS[1])
    assert "一致しません" in str(e.value)
    assert CHAIN_IDS[1] in str(e.value)


def test_resolve_concat_chain_compatible_chain_passes(store: HistoryStore, data_root: Path):
    """model_revision の違いだけでは拒否しない（同一モデルの再チェックポイント）。"""
    store.load()
    _success_chain(
        store,
        data_root,
        CHAIN_IDS[:2],
        overrides={CHAIN_IDS[1]: {"model_revision": "nf4-turbo8step-ckpt900"}},
    )
    assert len(store.resolve_concat_chain(CHAIN_IDS[1])) == 2


def test_resolve_concat_chain_rejects_missing_files(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:3])
    (data_root / "outputs" / f"{CHAIN_IDS[0]}.mp4").unlink()
    with pytest.raises(HistoryError, match="動画ファイルが見つかりません") as e:
        store.resolve_concat_chain(CHAIN_IDS[2])
    # どのIDが欠けているか明示する
    assert CHAIN_IDS[0] in str(e.value)
    assert CHAIN_IDS[1] not in str(e.value)


def test_resolve_concat_chain_lists_all_missing_ids(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:3], create_files=False)
    with pytest.raises(HistoryError) as e:
        store.resolve_concat_chain(CHAIN_IDS[2])
    for job_id in CHAIN_IDS[:3]:
        assert job_id in str(e.value)


def test_resolve_concat_chain_missing_parent(store: HistoryStore, data_root: Path):
    store.load()
    store.add(
        make_record(
            data_root,
            CHAIN_IDS[1],
            parent_id=CHAIN_IDS[0],
            job_type="continuation",
        )
    )
    with pytest.raises(HistoryError, match="親ジョブが履歴に見つかりません"):
        store.resolve_concat_chain(CHAIN_IDS[1])


def test_resolve_concat_chain_self_cycle(store: HistoryStore, data_root: Path):
    store.load()
    a = CHAIN_IDS[0]
    store.add(make_record(data_root, a, parent_id=a, job_type="continuation"))
    with pytest.raises(HistoryError, match="循環"):
        store.resolve_concat_chain(a)


def test_resolve_concat_chain_multi_node_cycle(store: HistoryStore, data_root: Path):
    store.load()
    a, b, c = CHAIN_IDS[0], CHAIN_IDS[1], CHAIN_IDS[2]
    store.add(make_record(data_root, a, parent_id=c, job_type="continuation"))
    store.add(make_record(data_root, b, parent_id=a, job_type="continuation"))
    store.add(make_record(data_root, c, parent_id=b, job_type="continuation"))
    with pytest.raises(HistoryError, match="循環"):
        store.resolve_concat_chain(c)


def test_resolve_concat_chain_depth_limit(store: HistoryStore, data_root: Path):
    store.load()
    ids = [f"v_20260807_1015{i:02d}_00{i:02d}" for i in range(25)]
    _success_chain(store, data_root, ids)
    with pytest.raises(HistoryError, match="長すぎます"):
        store.resolve_concat_chain(ids[-1])
    # 上限ちょうど（20本）は通る
    assert len(store.resolve_concat_chain(ids[19])) == 20


def test_resolve_concat_chain_unknown_id(store: HistoryStore, data_root: Path):
    store.load()
    with pytest.raises(HistoryError, match="履歴に存在しないジョブID"):
        store.resolve_concat_chain("v_20260807_101530_zzzz")


def test_resolve_concat_chain_rejects_paths_outside_data_root(
    store: HistoryStore, data_root: Path
):
    """履歴が手編集されて絶対パスが入っていても「欠損」として扱う。"""
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    doc = read_doc(store.path)
    doc["records"][0]["output_path"] = "/etc/passwd"
    store.path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    fresh = HistoryStore(store.path, data_root)
    fresh.load()
    with pytest.raises(HistoryError, match="動画ファイルが見つかりません"):
        fresh.resolve_concat_chain(CHAIN_IDS[1])


# ------------------------------------------------ P4: 連結結果の記録


def test_mark_concat_records_path_and_sources(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    concat = data_root / "concat" / f"c_{CHAIN_IDS[1]}_2clips.mp4"
    concat.write_bytes(b"concat")

    updated = store.mark_concat(
        CHAIN_IDS[1], concat_path=concat, concat_sources=CHAIN_IDS[:2]
    )
    assert updated.status is JobStatus.SUCCESS  # 状態は変えない
    assert updated.concat_path == f"concat/c_{CHAIN_IDS[1]}_2clips.mp4"
    assert updated.concat_sources == CHAIN_IDS[:2]

    # 永続化され、親側は変更されない
    fresh = HistoryStore(store.path, data_root)
    fresh.load()
    assert fresh.get(CHAIN_IDS[1]).concat_path == f"concat/c_{CHAIN_IDS[1]}_2clips.mp4"
    assert fresh.get(CHAIN_IDS[0]).concat_path is None


def test_mark_concat_keeps_schema_unchanged(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    before = set(read_doc(store.path)["records"][0])
    concat = data_root / "concat" / "c_x_2clips.mp4"
    concat.write_bytes(b"c")
    store.mark_concat(CHAIN_IDS[1], concat_path=concat, concat_sources=CHAIN_IDS[:2])
    doc = read_doc(store.path)
    assert doc["schema_version"] == HistoryStore.SCHEMA_VERSION
    assert set(doc["records"][0]) == before
    assert set(doc["records"][1]) == before


def test_mark_concat_is_repeatable(store: HistoryStore, data_root: Path):
    """再連結しても同じ内容で上書きされるだけ（履歴が壊れない）。"""
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    concat = data_root / "concat" / "c_x_2clips.mp4"
    concat.write_bytes(b"c")
    first = store.mark_concat(
        CHAIN_IDS[1], concat_path=concat, concat_sources=CHAIN_IDS[:2]
    )
    second = store.mark_concat(
        CHAIN_IDS[1], concat_path=concat, concat_sources=CHAIN_IDS[:2]
    )
    assert first == second
    assert len(read_doc(store.path)["records"]) == 2


def test_mark_concat_rejects_non_success(store: HistoryStore, data_root: Path):
    store.load()
    store.add(make_record(data_root, CHAIN_IDS[0]))
    concat = data_root / "concat" / "c_x_2clips.mp4"
    concat.write_bytes(b"c")
    with pytest.raises(HistoryError, match="成功していないジョブ"):
        store.mark_concat(
            CHAIN_IDS[0], concat_path=concat, concat_sources=CHAIN_IDS[:2]
        )


def test_mark_concat_rejects_bad_sources(store: HistoryStore, data_root: Path):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    concat = data_root / "concat" / "c_x_2clips.mp4"
    concat.write_bytes(b"c")
    with pytest.raises(HistoryError, match="2件以上"):
        store.mark_concat(
            CHAIN_IDS[1], concat_path=concat, concat_sources=[CHAIN_IDS[1]]
        )
    with pytest.raises(HistoryError, match="末尾"):
        store.mark_concat(
            CHAIN_IDS[1],
            concat_path=concat,
            concat_sources=[CHAIN_IDS[1], CHAIN_IDS[0]],
        )
    assert store.get(CHAIN_IDS[1]).concat_path is None


def test_mark_concat_rejects_path_outside_data_root(
    store: HistoryStore, data_root: Path, tmp_path: Path
):
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    outside = tmp_path / "elsewhere.mp4"
    with pytest.raises(HistoryError, match="データ領域の外"):
        store.mark_concat(
            CHAIN_IDS[1], concat_path=outside, concat_sources=CHAIN_IDS[:2]
        )
    assert store.get(CHAIN_IDS[1]).concat_path is None


def test_mark_concat_unknown_job(store: HistoryStore, data_root: Path):
    store.load()
    with pytest.raises(HistoryError, match="履歴に存在しないジョブID"):
        store.mark_concat(
            "v_20260807_101530_zzzz",
            concat_path=data_root / "concat" / "c.mp4",
            concat_sources=CHAIN_IDS[:2],
        )


def test_readonly_data_root_returns_warning_not_exception(tmp_path: Path):
    """初回作成が書けない場合も例外ではなく警告で返す（起動を止めない）。"""
    root = tmp_path / "readonly"
    root.mkdir()
    root.chmod(0o500)
    try:
        s = HistoryStore(root / "history.json", root)
        warnings = s.load()
        assert warnings and any("保存できません" in w for w in warnings)
        assert s.list_records() == []
    finally:
        root.chmod(0o700)


# ============================================== 任意順序連結の入力解決（P5.2）
#
# `resolve_custom_concat()` は親子関係を一切見ない。代わりに「ユーザーが選んだ
# 任意の並び」が連結してよいものかを確かめ、**並びをそのまま**返す。

CUSTOM_IDS = [
    "v_20260807_120000_c001",
    "v_20260807_120001_c002",
    "v_20260807_120002_c003",
]


def _success_singles(
    store: HistoryStore,
    data_root: Path,
    ids: list[str],
    *,
    create_files: bool = True,
    overrides: dict[str, dict] | None = None,
) -> None:
    """親子関係のない独立した SUCCESS 動画を作る（任意連結の素材）。"""
    for job_id in ids:
        rec = make_record(data_root, job_id, job_type="single", parent_id=None)
        if overrides and job_id in overrides:
            rec = dataclasses.replace(rec, **overrides[job_id])
        store.add(rec)
        store.mark_running(job_id, T0)
        out = data_root / "outputs" / f"{job_id}.mp4"
        last = data_root / "outputs" / f"{job_id}_last.png"
        if create_files:
            out.write_bytes(b"fake mp4")
            last.write_bytes(b"fake png")
        store.mark_success(
            job_id,
            output_path=out,
            last_frame_path=last,
            seed_used=42,
            elapsed_sec=1.0,
            finished_at=T0,
        )


def test_resolve_custom_concat_two_clips(store: HistoryStore, data_root: Path):
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    got = store.resolve_custom_concat(CUSTOM_IDS[:2])
    assert [r.id for r in got] == CUSTOM_IDS[:2]


def test_resolve_custom_concat_keeps_the_requested_order(
    store: HistoryStore, data_root: Path
):
    """作成日時やID順へ**並べ替えない**（指定順が成果物の順になる）。"""
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS)
    requested = [CUSTOM_IDS[2], CUSTOM_IDS[0], CUSTOM_IDS[1]]
    assert [r.id for r in store.resolve_custom_concat(requested)] == requested

    reverse = list(reversed(CUSTOM_IDS))
    assert [r.id for r in store.resolve_custom_concat(reverse)] == reverse


def test_resolve_custom_concat_twenty_clips(store: HistoryStore, data_root: Path):
    store.load()
    ids = [f"v_20260807_1200{i:02d}_d{i:03d}" for i in range(20)]
    _success_singles(store, data_root, ids)
    shuffled = ids[10:] + ids[:10]
    assert [r.id for r in store.resolve_custom_concat(shuffled)] == shuffled


def test_resolve_custom_concat_rejects_a_single_clip(store: HistoryStore, data_root: Path):
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:1])
    with pytest.raises(HistoryError, match="2本以上"):
        store.resolve_custom_concat(CUSTOM_IDS[:1])


def test_resolve_custom_concat_rejects_twenty_one_clips(
    store: HistoryStore, data_root: Path
):
    store.load()
    ids = [f"v_20260807_1200{i:02d}_e{i:03d}" for i in range(21)]
    _success_singles(store, data_root, ids)
    with pytest.raises(HistoryError, match="20本まで"):
        store.resolve_custom_concat(ids)


def test_resolve_custom_concat_rejects_duplicates(store: HistoryStore, data_root: Path):
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS)
    with pytest.raises(HistoryError, match="同じ動画が複数回"):
        store.resolve_custom_concat([CUSTOM_IDS[0], CUSTOM_IDS[1], CUSTOM_IDS[0]])


def test_resolve_custom_concat_rejects_unknown_ids(store: HistoryStore, data_root: Path):
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    with pytest.raises(HistoryError, match="履歴に無い"):
        store.resolve_custom_concat([CUSTOM_IDS[0], "v_20260807_999999_zzzz"])


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "   ",
        "../../etc/passwd",
        "outputs/v_x.mp4",
        "..",
        "'; DROP--",
        "v_\n20260807",
        "x" * 65,
    ],
)
def test_resolve_custom_concat_rejects_unsafe_ids(
    store: HistoryStore, data_root: Path, bad_id
):
    """パス区切り・`..`・制御文字などの危険なIDは履歴を引く前に弾く。"""
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    with pytest.raises(HistoryError, match="動画IDの形式"):
        store.resolve_custom_concat([CUSTOM_IDS[0], bad_id])


def test_resolve_custom_concat_rejects_ids_absent_from_history(
    store: HistoryStore, data_root: Path
):
    """形式上は安全でも、履歴に無いIDは通さない（本人確認は実在で行う）。"""
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    with pytest.raises(HistoryError, match="履歴に無い"):
        store.resolve_custom_concat([CUSTOM_IDS[0], "v_bad"])


def test_resolve_custom_concat_rejects_non_success(store: HistoryStore, data_root: Path):
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    failed = "v_20260807_120009_f001"
    store.add(make_record(data_root, failed))
    store.mark_running(failed, T0)
    store.mark_failed(
        failed, error="擬似失敗", category="input", elapsed_sec=1.0, finished_at=T0
    )

    with pytest.raises(HistoryError, match="成功していない動画"):
        store.resolve_custom_concat([CUSTOM_IDS[0], failed])


def test_resolve_custom_concat_rejects_a_concat_product_as_material(
    store: HistoryStore, data_root: Path
):
    """連結成果物（`cm_...`）は素材にできない（理由が分かる文言で断る）。"""
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    with pytest.raises(HistoryError, match="連結した動画は素材にできません"):
        store.resolve_custom_concat([CUSTOM_IDS[0], "cm_20260809_213000_0001"])


def test_resolve_custom_concat_rejects_missing_files(store: HistoryStore, data_root: Path):
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    (data_root / "outputs" / f"{CUSTOM_IDS[1]}.mp4").unlink()
    with pytest.raises(HistoryError, match="動画ファイルが見つかりません"):
        store.resolve_custom_concat(CUSTOM_IDS[:2])


def test_resolve_custom_concat_rejects_a_directory_instead_of_a_file(
    store: HistoryStore, data_root: Path
):
    """手編集で出力パスがディレクトリを指していても通さない。"""
    store.load()
    _success_singles(store, data_root, CUSTOM_IDS[:2])
    target = data_root / "outputs" / f"{CUSTOM_IDS[1]}.mp4"
    target.unlink()
    target.mkdir()
    with pytest.raises(HistoryError, match="動画ファイルが見つかりません"):
        store.resolve_custom_concat(CUSTOM_IDS[:2])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("backend_id", "other_backend", "生成バックエンド"),
        ("model_id", "別モデル", "モデル"),
        ("model_revision", "別の版", "モデルの版"),
        ("execution_engine", "real", "生成方法"),
        ("width", 640, "幅"),
        ("height", 480, "高さ"),
        ("fps", 30, "fps"),
    ],
)
def test_resolve_custom_concat_rejects_incompatible(
    store: HistoryStore, data_root: Path, field, value, message
):
    """互換性はチェーン連結より広い項目で見る（版・実行方式を追加）。"""
    store.load()
    _success_singles(
        store,
        data_root,
        CUSTOM_IDS[:2],
        overrides={CUSTOM_IDS[1]: {field: value}},
    )
    with pytest.raises(HistoryError, match=message):
        store.resolve_custom_concat(CUSTOM_IDS[:2])


def test_resolve_custom_concat_allows_different_seed_steps_and_prompt(
    store: HistoryStore, data_root: Path
):
    """seed・ステップ・長さ・プロンプトが違っても連結できる（仕様）。"""
    store.load()
    _success_singles(
        store,
        data_root,
        CUSTOM_IDS,
        overrides={
            CUSTOM_IDS[1]: {"steps": 8, "num_frames": 124, "duration_label": "5.17秒"},
            CUSTOM_IDS[2]: {"seed_used": 999, "prompt": "まったく別のプロンプト"},
        },
    )
    assert len(store.resolve_custom_concat(CUSTOM_IDS)) == 3


def test_resolve_custom_concat_accepts_continuation_clips(
    store: HistoryStore, data_root: Path
):
    """`type="continuation"` の動画も個別動画として素材にできる。"""
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:2])
    got = store.resolve_custom_concat([CHAIN_IDS[1], CHAIN_IDS[0]])
    assert [r.id for r in got] == [CHAIN_IDS[1], CHAIN_IDS[0]]


def test_resolve_custom_concat_does_not_change_chain_resolution(
    store: HistoryStore, data_root: Path
):
    """既存のチェーン連結の解決は非回帰（同じ素材で両方が従来どおり動く）。"""
    store.load()
    _success_chain(store, data_root, CHAIN_IDS[:3])
    assert [r.id for r in store.resolve_concat_chain(CHAIN_IDS[2])] == CHAIN_IDS[:3]
    reverse = list(reversed(CHAIN_IDS[:3]))
    assert [r.id for r in store.resolve_custom_concat(reverse)] == reverse
