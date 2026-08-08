import pytest

from app.core.fileops import (
    FileopsError,
    disk_state,
    ensure_within,
    list_orphan_partials,
    partial_path,
    promote,
)


def test_partial_path_same_dir(tmp_path):
    final = tmp_path / "v_x.mp4"
    p = partial_path(final)
    assert p.parent == final.parent
    assert p.name == "v_x.mp4.partial"


def test_promote_success(tmp_path):
    final = tmp_path / "out.bin"
    p = partial_path(final)
    p.write_bytes(b"data")
    promote(p, final)
    assert final.read_bytes() == b"data"
    assert not p.exists()


def test_promote_rejects_empty_and_keeps_partial(tmp_path):
    final = tmp_path / "out.bin"
    p = partial_path(final)
    p.write_bytes(b"")
    with pytest.raises(FileopsError, match="サイズが0"):
        promote(p, final)
    assert not final.exists()  # 正式名のファイルは作られない
    assert p.exists()  # partial は診断用に残る


def test_promote_validator_failure_keeps_partial(tmp_path):
    final = tmp_path / "out.bin"
    p = partial_path(final)
    p.write_bytes(b"broken")

    def _always_fail(path):
        raise FileopsError("検証に失敗しました")

    with pytest.raises(FileopsError, match="検証に失敗"):
        promote(p, final, (_always_fail,))
    assert not final.exists()
    assert p.exists()


def test_ensure_within_accepts_inside(tmp_path):
    inner = tmp_path / "outputs" / "a.mp4"
    inner.parent.mkdir()
    inner.write_bytes(b"x")
    assert ensure_within(tmp_path, inner) == inner.resolve()


def test_ensure_within_rejects_traversal(tmp_path):
    with pytest.raises(FileopsError, match="許可されていないパス"):
        ensure_within(tmp_path, tmp_path / ".." / "etc" / "passwd")


def test_disk_state_thresholds():
    # §21.1-3 確定: 20GB未満で警告、5GB未満で受付停止
    assert disk_state(100.0, 20, 5) == "ok"
    assert disk_state(20.0, 20, 5) == "ok"
    assert disk_state(19.9, 20, 5) == "warn"
    assert disk_state(5.0, 20, 5) == "warn"
    assert disk_state(4.9, 20, 5) == "stop"
    assert disk_state(0.0, 20, 5) == "stop"


def test_list_orphan_partials(tmp_path):
    (tmp_path / "a.mp4.partial").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    found = list_orphan_partials(tmp_path, tmp_path / "nonexistent")
    assert [p.name for p in found] == ["a.mp4.partial"]


def test_list_orphan_partials_includes_worker_temp_files(tmp_path):
    """ワーカーの中間ファイル（.xxx.tmp.mp4）も孤児として列挙する。"""
    (tmp_path / "a.mp4.partial").write_bytes(b"x")
    (tmp_path / ".b.mp4.partial.tmp.mp4").write_bytes(b"x")  # 隠し中間ファイル
    (tmp_path / "c.mp4").write_bytes(b"x")  # 正式ファイルは対象外
    found = sorted(p.name for p in list_orphan_partials(tmp_path))
    assert found == [".b.mp4.partial.tmp.mp4", "a.mp4.partial"]
