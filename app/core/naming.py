"""ID・ファイル名の採番（決定D15）。`v_YYYYMMDD_HHMMSS_xxxx` 形式。"""

from __future__ import annotations

import re
import secrets
from datetime import datetime

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_PATTERN = re.compile(r"^v_\d{8}_\d{6}_[0-9a-z]{4}$")


def new_video_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"v_{now:%Y%m%d}_{now:%H%M%S}_{suffix}"


def is_valid_id(video_id: str) -> bool:
    return bool(ID_PATTERN.match(video_id))
