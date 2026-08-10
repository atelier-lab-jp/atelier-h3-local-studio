"""ID・ファイル名の採番（決定D15）。`v_YYYYMMDD_HHMMSS_xxxx` 形式。

P5.2 で任意順序連結の成果物 ID `cm_YYYYMMDD_HHMMSS_xxxx` を追加した。
生成ジョブの ID（`v_`）とは接頭辞で分かれるので、両者が混ざっても
取り違えは起きない（`is_valid_id` / `is_valid_manual_concat_id` が別々に判定する）。
"""

from __future__ import annotations

import hashlib
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


#: 1080p高品質版（P6）の元成果物の種類。**種類を省略しない**
#: （個別動画とチェーン連結は同じ job_id を使うため、種類が無いと衝突する）。
UPSCALE_SOURCE_KINDS = ("clip", "chain", "manual")

#: ファイル名に使ってよい文字（安全な短縮の判定に使う）
_SAFE_ID = re.compile(r"^[0-9A-Za-z_.-]{1,64}$")

#: 出力名が長くなりすぎないための上限（短縮時はハッシュを足して一意にする）
_MAX_ID_IN_NAME = 48


def is_safe_artifact_id(value: str) -> bool:
    """成果物IDとして安全か（パス区切り・`..`・制御文字・長すぎる値を弾く）。"""
    text = str(value)
    return bool(_SAFE_ID.match(text)) and ".." not in text


def upscaled_filename(source_kind: str, source_id: str) -> str:
    """`u_{種類}_{元ID}_1080p.mp4`（P6）。**同じ入力なら必ず同じ名前**になる。

    長いIDは短縮したうえで短いハッシュを足す（別のIDが同じ名前にならないように）。
    """
    if source_kind not in UPSCALE_SOURCE_KINDS:
        raise ValueError(f"高品質化できない種類です: {source_kind!r}")
    if not is_safe_artifact_id(source_id):
        raise ValueError(f"動画IDの形式が正しくありません: {source_id!r}")

    safe_id = source_id
    if len(safe_id) > _MAX_ID_IN_NAME:
        digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:8]
        safe_id = f"{source_id[: _MAX_ID_IN_NAME - 9]}_{digest}"
    return f"u_{source_kind}_{safe_id}_1080p.mp4"
