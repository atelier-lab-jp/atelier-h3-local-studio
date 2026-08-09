"""ID・ファイル名の採番（決定D15）。`v_YYYYMMDD_HHMMSS_xxxx` 形式。

P5.2 で任意順序連結の成果物 ID `cm_YYYYMMDD_HHMMSS_xxxx` を追加した。
生成ジョブの ID（`v_`）とは接頭辞で分かれるので、両者が混ざっても
取り違えは起きない（`is_valid_id` / `is_valid_manual_concat_id` が別々に判定する）。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_PATTERN = re.compile(r"^v_\d{8}_\d{6}_[0-9a-z]{4}$")

#: 任意順序連結（P5.2）の成果物ID。`c_*`（チェーン連結のファイル名）とも重ならない
MANUAL_CONCAT_ID_PATTERN = re.compile(r"^cm_\d{8}_\d{6}_[0-9a-z]{4}$")


def new_video_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"v_{now:%Y%m%d}_{now:%H%M%S}_{suffix}"


def is_valid_id(video_id: str) -> bool:
    return bool(ID_PATTERN.match(video_id))


def new_manual_concat_id(now: datetime | None = None) -> str:
    """任意順序連結の成果物ID（P5.2）。時刻＋乱数4桁で衝突を実用上排除する。"""
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"cm_{now:%Y%m%d}_{now:%H%M%S}_{suffix}"


def is_valid_manual_concat_id(concat_id: str) -> bool:
    return bool(MANUAL_CONCAT_ID_PATTERN.match(concat_id))


def manual_concat_filename(concat_id: str, clips: int) -> str:
    """`cm_YYYYMMDD_HHMMSS_xxxx_<n>clips.mp4`（P5.2）。"""
    if not is_valid_manual_concat_id(concat_id):
        raise ValueError(f"任意連結IDの形式が不正です: {concat_id}")
    if not isinstance(clips, int) or isinstance(clips, bool) or clips < 2:
        raise ValueError(f"連結本数は2以上の整数で指定してください: {clips!r}")
    return f"{concat_id}_{clips}clips.mp4"
