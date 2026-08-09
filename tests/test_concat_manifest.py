"""任意順序連結の台帳 `concat_manifest.json` のテスト（P5.2・設計書 §23.5）。

確認したいことは2つ:

1. **同じ素材の別の組み合わせを、上書きせずに何件でも持てること**
   （履歴の `concat_path` 方式では持てなかったのが、この台帳を作った理由）
2. **壊れても MP4 を巻き添えにしないこと**
   （primary 破損 → 隔離 → 検証済み `.bak` から復旧。両方壊れても空で起動）

書き込み先はすべて `tmp_path`。プロジェクトの `data/` には一切触れない。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.concat_manifest import (
    MAX_MANUAL_CLIPS,
    ConcatManifest,
    ConcatManifestError,
    ManualConcatEntry,
)

T0 = datetime(2026, 8, 9, 21, 30, 0).astimezone()

A = "v_20260809_210000_aaaa"
B = "v_20260809_210001_bbbb"
C = "v_20260809_210002_cccc"
D = "v_20260809_210003_dddd"


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "concat").mkdir(parents=True)
    return root


@pytest.fixture()
def store(data_root: Path) -> ConcatManifest:
    manifest = ConcatManifest(data_root / "concat_manifest.json", data_root)
    assert manifest.load() == []
    return manifest


def make_entry(
    concat_id: str,
    sources: list[str],
    *,
    created_at: datetime | None = None,
    num_frames_total: int | None = None,
    output_path: str | None = None,
) -> ManualConcatEntry:
    clips = len(sources)
    return ManualConcatEntry(
        id=concat_id,
        created_at=created_at or T0,
        output_path=output_path or f"concat/{concat_id}_{clips}clips.mp4",
        sources=tuple(sources),
        clips=clips,
        num_frames_total=num_frames_total or (56 * clips),
        fps=24,
        width=576,
        height=320,
        backend_id="minimax_h3",
        model_id="DiffSynth-Studio/MiniMax-H3-NF4",
        model_revision="nf4-turbo4step-ckpt500",
        execution_engine="mock",
        app_version="1.0.0",
    )


def write_raw(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ================================================================ 基本


def test_missing_manifest_starts_empty_without_warning(data_root: Path):
    """既存環境（任意連結を作ったことがない）はそのまま起動できる。"""
    manifest = ConcatManifest(data_root / "concat_manifest.json", data_root)
    assert manifest.load() == []
    assert manifest.list_entries() == []
    # ファイルを勝手に作らない（履歴と違い、無い＝0件が正常）
    assert not (data_root / "concat_manifest.json").exists()


def test_add_creates_the_file_and_is_readable_again(store: ConcatManifest, data_root: Path):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))

    reopened = ConcatManifest(data_root / "concat_manifest.json", data_root)
    assert reopened.load() == []
    entries = reopened.list_entries()
    assert [e.id for e in entries] == ["cm_20260809_213000_0001"]
    assert entries[0].sources == (A, B)


def test_multiple_entries_coexist(store: ConcatManifest):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    store.add(make_entry("cm_20260809_213001_0002", [B, C, A]))
    assert len(store.list_entries()) == 2


def test_same_sources_in_different_orders_are_separate_products(store: ConcatManifest):
    """A→B→C と A→D→C が**両方**残る（履歴方式では上書きされていた組み合わせ）。"""
    store.add(make_entry("cm_20260809_213000_0001", [A, B, C]))
    store.add(make_entry("cm_20260809_213001_0002", [A, D, C]))
    store.add(make_entry("cm_20260809_213002_0003", [B, A]))

    by_id = {e.id: e for e in store.list_entries()}
    assert len(by_id) == 3
    assert by_id["cm_20260809_213000_0001"].sources == (A, B, C)
    assert by_id["cm_20260809_213001_0002"].sources == (A, D, C)
    assert by_id["cm_20260809_213002_0003"].sources == (B, A)
    # 出力ファイル名もそれぞれ別
    assert len({e.output_path for e in by_id.values()}) == 3


def test_sources_order_is_preserved_exactly(store: ConcatManifest):
    """作成日時順・ID順へ並べ替えない（並べ替えると成果物と食い違う）。"""
    store.add(make_entry("cm_20260809_213000_0001", [C, A, B]))
    assert store.get("cm_20260809_213000_0001").sources == (C, A, B)


def test_list_entries_is_newest_first(store: ConcatManifest):
    store.add(make_entry("cm_20260809_213000_0001", [A, B], created_at=T0))
    store.add(
        make_entry(
            "cm_20260809_213001_0002", [C, D], created_at=T0 + timedelta(minutes=5)
        )
    )
    assert [e.id for e in store.list_entries()][0] == "cm_20260809_213001_0002"
    assert [e.id for e in store.list_entries(newest_first=False)][0] == (
        "cm_20260809_213000_0001"
    )


def test_duration_is_derived_from_frames_and_fps(store: ConcatManifest):
    entry = make_entry("cm_20260809_213000_0001", [A, B], num_frames_total=248)
    assert entry.duration_sec == pytest.approx(248 / 24)
    assert entry.duration_label == "10.33秒"


def test_entries_are_immutable_snapshots(store: ConcatManifest):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    got = store.get("cm_20260809_213000_0001")
    assert isinstance(got.sources, tuple)  # 可変コンテナを共有しない
    with pytest.raises(Exception):
        got.id = "書き換え"  # frozen


def test_duplicate_id_is_rejected(store: ConcatManifest):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    with pytest.raises(ConcatManifestError, match="すでに記録されています"):
        store.add(make_entry("cm_20260809_213000_0001", [C, D]))


# ================================================================ 入力検証


@pytest.mark.parametrize(
    "concat_id",
    ["", "cm_bad", "v_20260809_210000_aaaa", "cm_20260809_210000_AAAA", "../escape"],
)
def test_invalid_concat_id_is_rejected(concat_id):
    with pytest.raises(ConcatManifestError, match="任意連結IDの形式"):
        make_entry(concat_id, [A, B])


def test_duplicate_sources_are_rejected():
    with pytest.raises(ConcatManifestError, match="重複"):
        make_entry("cm_20260809_213000_0001", [A, A])


def test_too_few_sources_are_rejected():
    with pytest.raises(ConcatManifestError, match="2件以上"):
        make_entry("cm_20260809_213000_0001", [A])


def test_too_many_sources_are_rejected():
    ids = [f"v_20260809_2100{i:02d}_x{i:03d}" for i in range(MAX_MANUAL_CLIPS + 1)]
    with pytest.raises(ConcatManifestError, match="20件まで"):
        make_entry("cm_20260809_213000_0001", ids)


def test_malformed_source_id_is_rejected():
    with pytest.raises(ConcatManifestError, match="ジョブID形式"):
        make_entry("cm_20260809_213000_0001", [A, "../../etc/passwd"])


def test_absolute_output_path_is_rejected():
    with pytest.raises(ConcatManifestError, match="相対パス"):
        make_entry("cm_20260809_213000_0001", [A, B], output_path="/tmp/evil.mp4")


def test_escaping_output_path_is_rejected():
    with pytest.raises(ConcatManifestError, match="データ領域の外"):
        make_entry(
            "cm_20260809_213000_0001", [A, B], output_path="concat/../../evil.mp4"
        )


def test_output_outside_data_root_is_rejected_on_add(store: ConcatManifest, tmp_path):
    """相対表記でも、解決すると data_root の外に出るものは記録しない。"""
    entry = make_entry("cm_20260809_213000_0001", [A, B])
    object.__setattr__(entry, "output_path", "concat/../../outside.mp4")
    with pytest.raises(ConcatManifestError, match="データ領域の外"):
        store.add(entry)


@pytest.mark.parametrize("field", ["num_frames_total", "fps", "width", "height"])
def test_non_positive_numbers_are_rejected(field):
    kwargs = dict(
        id="cm_20260809_213000_0001",
        created_at=T0,
        output_path="concat/cm_20260809_213000_0001_2clips.mp4",
        sources=(A, B),
        clips=2,
        num_frames_total=112,
        fps=24,
        width=576,
        height=320,
        backend_id="minimax_h3",
        model_id="m",
        model_revision="r",
        execution_engine="mock",
        app_version="1.0.0",
    )
    kwargs[field] = 0
    with pytest.raises(ConcatManifestError, match="1以上の整数"):
        ManualConcatEntry(**kwargs)


def test_clips_must_match_sources_length():
    with pytest.raises(ConcatManifestError, match="件数が一致しません"):
        ManualConcatEntry(
            id="cm_20260809_213000_0001",
            created_at=T0,
            output_path="concat/x_3clips.mp4",
            sources=(A, B),
            clips=3,
            num_frames_total=112,
            fps=24,
            width=576,
            height=320,
            backend_id="minimax_h3",
            model_id="m",
            model_revision="r",
            execution_engine="mock",
            app_version="1.0.0",
        )


def test_wrong_type_is_rejected(store: ConcatManifest):
    with pytest.raises(ConcatManifestError, match="型が不正"):
        store.add({"id": "cm_20260809_213000_0001"})


def test_missing_required_field_is_rejected_on_load(store: ConcatManifest, data_root):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    del doc["entries"][0]["fps"]
    write_raw(store.path, json.dumps(doc, ensure_ascii=False))

    reopened = ConcatManifest(store.path, data_root)
    warnings = reopened.load()
    assert any("読み込めませんでした" in w for w in warnings)


def test_unknown_schema_version_is_rejected(store: ConcatManifest, data_root):
    write_raw(store.path, json.dumps({"schema_version": 99, "entries": []}))
    reopened = ConcatManifest(store.path, data_root)
    assert any("schema_version" in w for w in reopened.load())


def test_upscale_path_is_accepted_for_future_use(store: ConcatManifest, data_root):
    """P6（1080p高品質化）が後から埋められる欄。今は None のまま素通しする。"""
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    assert doc["entries"][0]["upscale_path"] is None
    doc["entries"][0]["upscale_path"] = "concat/cm_20260809_213000_0001_1080p.mp4"
    write_raw(store.path, json.dumps(doc, ensure_ascii=False))

    reopened = ConcatManifest(store.path, data_root)
    assert reopened.load() == []
    assert reopened.get("cm_20260809_213000_0001").upscale_path.endswith("_1080p.mp4")


# ================================================================ 原子的保存


def test_save_is_atomic_and_leaves_no_tmp(store: ConcatManifest):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    assert list(store.path.parent.glob("*.tmp")) == []
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_tmp_names_do_not_collide_between_instances(data_root: Path):
    """同じ data_root を複数インスタンスが使っても tmp を奪い合わない。"""
    a = ConcatManifest(data_root / "concat_manifest.json", data_root)
    b = ConcatManifest(data_root / "concat_manifest.json", data_root)
    names = {a._new_tmp_path().name for _ in range(20)} | {
        b._new_tmp_path().name for _ in range(20)
    }
    assert len(names) == 40


def test_concurrent_adds_are_all_persisted(store: ConcatManifest, data_root: Path):
    def add(i: int) -> None:
        store.add(make_entry(f"cm_20260809_2130{i:02d}_{i:04d}", [A, B]))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    reopened = ConcatManifest(data_root / "concat_manifest.json", data_root)
    reopened.load()
    assert len(reopened.list_entries()) == 8


def test_save_failure_rolls_memory_back(store: ConcatManifest, monkeypatch):
    """保存に失敗したら、メモリ上も追加前へ戻す（ディスクとの乖離を作らない）。"""
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    before = [e.id for e in store.list_entries()]

    def boom(*_a, **_kw):
        raise OSError("ディスク満杯（擬似）")

    monkeypatch.setattr("builtins.open", boom)
    with pytest.raises(ConcatManifestError, match="保存できません"):
        store.add(make_entry("cm_20260809_213001_0002", [C, D]))
    monkeypatch.undo()

    assert [e.id for e in store.list_entries()] == before
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert [e["id"] for e in on_disk["entries"]] == before


def test_save_failure_leaves_no_orphan_tmp(store: ConcatManifest, monkeypatch):
    real_replace = os.replace

    def fail_replace(src, dst):
        if str(dst).endswith("concat_manifest.json"):
            raise OSError("replace 失敗（擬似）")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConcatManifestError):
        store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    monkeypatch.undo()
    assert list(store.path.parent.glob("*.tmp")) == []


# ================================================================ .bak と復旧


def test_backup_is_written_only_from_a_valid_primary(store: ConcatManifest, data_root):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))

    reopened = ConcatManifest(data_root / "concat_manifest.json", data_root)
    assert reopened.load() == []
    assert reopened.backup_path.exists()
    assert json.loads(reopened.backup_path.read_text(encoding="utf-8"))["entries"]


def test_backup_is_not_updated_during_the_session(store: ConcatManifest, data_root):
    """`.bak` は「起動時点の検証済みスナップショット」。セッション中は汚さない。"""
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    reopened = ConcatManifest(data_root / "concat_manifest.json", data_root)
    reopened.load()
    snapshot = reopened.backup_path.read_text(encoding="utf-8")

    reopened.add(make_entry("cm_20260809_213001_0002", [C, D]))
    assert reopened.backup_path.read_text(encoding="utf-8") == snapshot


def test_corrupt_primary_is_quarantined_and_recovered_from_backup(
    store: ConcatManifest, data_root
):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    # 正常な内容で .bak を作る
    first = ConcatManifest(data_root / "concat_manifest.json", data_root)
    first.load()
    assert first.backup_path.exists()

    write_raw(store.path, "{壊れたJSON")

    recovered = ConcatManifest(data_root / "concat_manifest.json", data_root)
    warnings = recovered.load()

    assert any("退避しました" in w for w in warnings)
    assert any("復旧しました" in w for w in warnings)
    assert [e.id for e in recovered.list_entries()] == ["cm_20260809_213000_0001"]
    quarantined = list(data_root.glob("concat_manifest_corrupt_*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{壊れたJSON"


def test_corrupt_primary_does_not_overwrite_a_good_backup(store: ConcatManifest, data_root):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    first = ConcatManifest(data_root / "concat_manifest.json", data_root)
    first.load()
    good_backup = first.backup_path.read_text(encoding="utf-8")

    write_raw(store.path, "壊れています")
    ConcatManifest(data_root / "concat_manifest.json", data_root).load()

    assert first.backup_path.read_text(encoding="utf-8") == good_backup


def test_recovery_rejects_a_corrupt_backup_too(store: ConcatManifest, data_root):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    first = ConcatManifest(data_root / "concat_manifest.json", data_root)
    first.load()

    write_raw(store.path, "壊れています")
    write_raw(first.backup_path, "こちらも壊れています")

    recovered = ConcatManifest(data_root / "concat_manifest.json", data_root)
    warnings = recovered.load()
    assert any("バックアップも読み込めませんでした" in w for w in warnings)
    assert recovered.list_entries() == []


def test_both_broken_starts_empty_and_keeps_the_mp4_files(store: ConcatManifest, data_root):
    """台帳が全滅しても**MP4 は削除しない**（一覧に出なくなるだけ）。"""
    mp4 = data_root / "concat" / "cm_20260809_213000_0001_2clips.mp4"
    mp4.write_bytes(b"pretend mp4")
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    first = ConcatManifest(data_root / "concat_manifest.json", data_root)
    first.load()

    write_raw(store.path, "壊れています")
    write_raw(first.backup_path, "壊れています")

    recovered = ConcatManifest(data_root / "concat_manifest.json", data_root)
    warnings = recovered.load()
    assert recovered.list_entries() == []
    assert any("削除していません" in w for w in warnings)
    assert mp4.exists() and mp4.read_bytes() == b"pretend mp4"


def test_missing_backup_starts_empty_with_a_japanese_warning(store: ConcatManifest, data_root):
    store.add(make_entry("cm_20260809_213000_0001", [A, B]))
    write_raw(store.path, "壊れています")

    recovered = ConcatManifest(data_root / "concat_manifest.json", data_root)
    warnings = recovered.load()
    assert any("バックアップが見つからない" in w for w in warnings)
    assert recovered.list_entries() == []


# ================================================================ ID 採番


def test_new_id_has_the_expected_shape(store: ConcatManifest):
    concat_id = store.new_id(T0)
    assert concat_id.startswith("cm_20260809_213000_")
    assert len(concat_id) == len("cm_20260809_213000_abcd")


def test_new_id_avoids_recorded_ids(store: ConcatManifest, monkeypatch):
    taken = "cm_20260809_213000_0001"
    store.add(make_entry(taken, [A, B]))
    seq = iter([taken, taken, "cm_20260809_213000_0009"])
    monkeypatch.setattr(
        "app.core.naming.new_manual_concat_id", lambda now=None: next(seq)
    )
    assert store.new_id(T0) == "cm_20260809_213000_0009"


def test_new_id_avoids_existing_files(store: ConcatManifest, data_root, monkeypatch):
    """台帳に無くても、同じ名前の MP4 が実在すれば採番し直す（上書き防止）。"""
    (data_root / "concat" / "cm_20260809_213000_0001_3clips.mp4").write_bytes(b"x")
    seq = iter(["cm_20260809_213000_0001", "cm_20260809_213000_0002"])
    monkeypatch.setattr(
        "app.core.naming.new_manual_concat_id", lambda now=None: next(seq)
    )
    assert store.new_id(T0) == "cm_20260809_213000_0002"


def test_to_absolute_rejects_paths_outside_data_root(store: ConcatManifest):
    assert store.to_absolute("concat/x.mp4") is not None
    assert store.to_absolute("../outside.mp4") is None
    assert store.to_absolute("/etc/hosts") is None
    assert store.to_absolute(None) is None
