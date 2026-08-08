from datetime import datetime

from app.core.naming import is_valid_id, new_video_id


def test_id_format():
    vid = new_video_id(datetime(2026, 8, 7, 10, 15, 30))
    assert vid.startswith("v_20260807_101530_")
    assert is_valid_id(vid)


def test_id_uniqueness_same_second():
    now = datetime(2026, 8, 7, 10, 15, 30)
    ids = {new_video_id(now) for _ in range(100)}
    assert len(ids) == 100


def test_invalid_ids_rejected():
    assert not is_valid_id("v_2026_bad")
    assert not is_valid_id("../etc/passwd")
    assert not is_valid_id("v_20260807_101530_ABCD")  # 大文字は不許可
