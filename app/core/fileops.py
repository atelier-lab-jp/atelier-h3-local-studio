"""原子的ファイル操作とパス安全性（設計書 §10.7・§15）。

すべての成果物は「partial 書き込み → 検証 → os.replace() 昇格」で作る。
検証に失敗した場合は正式名のファイルを作らず、partial を診断用に残す。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

PARTIAL_SUFFIX = ".partial"


class FileopsError(Exception):
    """ファイル操作の失敗（日本語メッセージ）。"""


def partial_path(final_path: Path) -> Path:
    """正式名に対応する partial パス（同一ディレクトリ内）。"""
    return final_path.with_name(final_path.name + PARTIAL_SUFFIX)


def verify_nonempty(path: Path) -> None:
    if not path.is_file():
        raise FileopsError(f"ファイルが作成されていません: {path.name}")
    if path.stat().st_size <= 0:
        raise FileopsError(f"ファイルサイズが0です: {path.name}")


def verify_png(path: Path) -> None:
    """PNG が画像として開けることを確認する（PIL）。"""
    verify_nonempty(path)
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            if img.width <= 0 or img.height <= 0:
                raise FileopsError(f"画像サイズが不正です: {path.name}")
    except FileopsError:
        raise
    except Exception as e:
        raise FileopsError(f"画像として開けません: {path.name}（{e}）") from e


def promote(
    partial: Path,
    final_path: Path,
    validators: tuple[Callable[[Path], None], ...] = (),
) -> Path:
    """partial を検証し、合格した場合のみ正式名へ昇格する（設計書 §10.7）。

    検証失敗時は FileopsError を送出し、partial はそのまま残す。
    """
    verify_nonempty(partial)
    for validate in validators:
        validate(partial)
    os.replace(partial, final_path)
    return final_path


def ensure_within(base: Path, target: Path) -> Path:
    """target が base 配下であることを検証して絶対パスを返す（設計書 §15）。"""
    base_resolved = base.resolve()
    resolved = target.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise FileopsError(f"許可されていないパスです: {target}")
    return resolved


def disk_free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024**3)


def disk_state(free_gb: float, warn_gb: float, stop_gb: float) -> str:
    """空き容量の状態: "ok" / "warn"（警告） / "stop"（新規受付停止）。"""
    if free_gb < stop_gb:
        return "stop"
    if free_gb < warn_gb:
        return "warn"
    return "ok"


#: ワーカーが PyAV の拡張子制約を回避するために使う中間ファイル（設計書 §10.7）。
#: 隠しファイルで `.partial` 終わりでもないため、明示的に列挙対象へ含める。
WORKER_TEMP_GLOB = ".*.tmp.mp4"


def list_orphan_partials(*dirs: Path) -> list[Path]:
    """起動時ログ用: 残存している未完了ファイルを列挙する（削除はしない）。

    対象は `*.partial` と、ワーカーの中間ファイル `.*.tmp.mp4`。
    後者は正常時は必ず消えるが、terminate/kill でエンコード中に停止した場合に
    隠しファイルとして残りうるため、気付けるように列挙する。
    """
    found: list[Path] = []
    for d in dirs:
        if d.is_dir():
            found.extend(sorted(d.glob(f"*{PARTIAL_SUFFIX}")))
            found.extend(sorted(d.glob(WORKER_TEMP_GLOB)))
    return found
