"""アプリ内ゴミ箱への移動（P5.3-B・設計書 §25）。

**やることは1つだけ**: 選ばれた成果物ファイルを `data/trash/` へ安全に移す。

やらないこと（意図的に持たない。設計書 §25.0）:
  - 削除台帳・tombstone・復元情報の記録
  - 依存関係（親子・チェーン・連結素材）の検査や連動削除
  - 復元UI・非表示フィルタ・可視性ストア
  - `history.json` / `concat_manifest.json` の書き換え

表示から消えるのは「ファイルが無くなったから」であって、どこかに
「消した」と記録するからではない（`AppService.completed_videos()` が
実在するものだけを返す）。だから正式パスへ戻せば自然に再表示される。
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from app.core.fileops import FileopsError, ensure_within

log = logging.getLogger("atelier.trash")

#: ゴミ箱ディレクトリ名（`data_root` 直下。HTTP 配信対象には**入れない**）
TRASH_DIR_NAME = "trash"

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


class TrashError(Exception):
    """ゴミ箱移動を実行できない・失敗した（日本語メッセージ）。"""


def trash_dir(data_root: Path) -> Path:
    """`data_root/trash`。**存在確認だけで作成はしない**（副作用を持たせない）。"""
    return Path(data_root) / TRASH_DIR_NAME


def _unique_target(destination: Path, name: str) -> Path:
    """ゴミ箱内で衝突しない名前を作る（**既存ファイルを上書きしない**）。

    まず元の名前、埋まっていれば `{stem}_{日時}_{乱数4}{suffix}` を試す。
    命名は既存の ID 規則（日時＋短い乱数）に合わせてある。
    """
    candidate = destination / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for _ in range(100):
        rand = "".join(secrets.choice(_ALPHABET) for _ in range(4))
        candidate = destination / f"{stem}_{stamp}_{rand}{suffix}"
        if not candidate.exists():
            return candidate
    raise TrashError(f"ゴミ箱に置ける名前を作れませんでした: {name}")


def validate_movable(path: Path, *, data_root: Path) -> Path:
    """移動してよいファイルかを確かめて、解決済みの絶対パスを返す。

    ブラウザから来るのは選択キーだけなので、ここへ渡るパスは**サーバ側で
    正式なストアから引いたもの**である。それでも念のため次を確かめる:

      1. `data_root` の内側にある（`..` や絶対パスでの脱出を弾く）
      2. **symlink ではない**（リンク先の実体を巻き添えにしない）
      3. 通常ファイルである（ディレクトリ・特殊ファイルを動かさない）
      4. ゴミ箱の中のものではない（ゴミ箱をゴミ箱へ入れ子にしない）
    """
    base = Path(data_root)
    target = Path(path)
    if not target.is_absolute():
        target = base / target

    if target.is_symlink():
        raise TrashError(f"リンクは移動できません: {target.name}")
    try:
        resolved = ensure_within(base, target)
    except FileopsError as e:
        raise TrashError(f"アプリのデータ領域の外は移動できません: {path}") from e
    except OSError as e:  # pragma: no cover - 実行環境依存
        raise TrashError(f"パスを解決できません: {path}（{e}）") from e

    if resolved.is_symlink():  # pragma: no cover - resolve 後は通常あり得ない
        raise TrashError(f"リンクは移動できません: {resolved.name}")
    if not resolved.exists():
        raise TrashError("動画はすでに移動されたか、見つかりません。")
    if not resolved.is_file():
        raise TrashError(f"ファイルではありません: {resolved.name}")

    trash = trash_dir(base).resolve() if trash_dir(base).exists() else trash_dir(base)
    if resolved == trash or trash in resolved.parents:
        raise TrashError("ゴミ箱の中のファイルは移動できません。")
    return resolved


def move_to_trash(paths: list[Path], *, data_root: Path) -> list[tuple[Path, Path]]:
    """複数ファイルをまとめて `data/trash/` へ移す（全部成功か、全部元のままか）。

    途中で失敗したら、**すでに移した分を元の場所へ戻してから**送出する。
    戻すのにも失敗した場合は、元パスと移動先を ERROR ログへ残す
    （利用者が Finder で復旧できるようにするため）。

    戻り値は `(元のパス, ゴミ箱内のパス)` の一覧。
    """
    base = Path(data_root)
    targets = [validate_movable(p, data_root=base) for p in paths]
    if not targets:
        raise TrashError("移動するファイルがありません。")

    destination = trash_dir(base)
    try:
        # ゴミ箱は**実際に移動するときだけ**作る（一覧表示では data/ を触らない）
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TrashError(f"ゴミ箱フォルダを作れませんでした（{e}）") from e

    moved: list[tuple[Path, Path]] = []
    for source in targets:
        try:
            target = _unique_target(destination, source.name)
            os.replace(source, target)  # 同一ボリューム内なので原子的
            moved.append((source, target))
        except (OSError, TrashError) as e:
            _rollback(moved)
            raise TrashError(
                f"ゴミ箱へ移動できませんでした（{source.name}: {e}）"
            ) from e

    for source, target in moved:
        log.info("ゴミ箱へ移動しました: %s → %s", source.name, target.name)
    return moved


def _rollback(moved: list[tuple[Path, Path]]) -> None:
    """移動済みのファイルを元の場所へ戻す（失敗したら場所をログへ残す）。"""
    for source, target in reversed(moved):
        try:
            os.replace(target, source)
            log.warning("移動を取り消しました: %s ← %s", source.name, target.name)
        except OSError as e:
            log.error(
                "移動の取り消しに失敗しました。Finder で戻してください: "
                "移動先 %s → 元の場所 %s（%s）",
                target,
                source,
                e,
            )
