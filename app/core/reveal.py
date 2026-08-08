"""Finder でファイルを表示する（設計書 §15、P4契約 §6）。

安全規律（この3つを崩さない）:
  1. `shell=True` を使わない。必ず `["open", "-R", <path>]` の**引数配列**で起動する
     （パス名に `;` や `$(...)` が含まれていてもシェルに解釈されない）
  2. `data_root` 配下の**実在するファイル**だけを許可する（`fileops.ensure_within`）
  3. 失敗はすべて日本語の RevealError にする（UI はそのまま表示できる）

`runner` を差し替えられるようにしてあるのは、テストで実際に Finder を開かないため。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.core.fileops import FileopsError, ensure_within

log = logging.getLogger("atelier.reveal")

#: macOS の Finder で「対象を選択した状態で」フォルダを開くコマンド
REVEAL_COMMAND = ("open", "-R")


class RevealError(Exception):
    """Finder 表示の失敗（日本語メッセージ）。"""


def reveal_in_finder(
    path: Path | str | None,
    *,
    data_root: Path,
    runner=subprocess.run,
) -> None:
    """`path` を Finder で表示する。data_root 配下・実在のファイルのみ許可する。"""
    if path is None or str(path).strip() == "":
        raise RevealError("表示するファイルが指定されていません")

    base = Path(data_root)
    target = Path(path)
    if not target.is_absolute():
        # 履歴由来の相対パスは data_root 基準で解釈する（CWD 依存にしない）
        target = base / target

    try:
        resolved = ensure_within(base, target)
    except FileopsError as e:
        raise RevealError(
            f"アプリのデータ領域の外は表示できません: {path}"
        ) from e
    except OSError as e:  # pragma: no cover - 実行環境依存
        raise RevealError(f"パスを解決できません: {path}（{e}）") from e

    if not resolved.exists():
        raise RevealError(f"ファイルが見つかりません: {resolved.name}")
    if not resolved.is_file():
        raise RevealError(f"ファイルではありません: {resolved.name}")

    args = [*REVEAL_COMMAND, str(resolved)]
    log.debug("Finder 表示: %s", args)
    try:
        # shell=True は使わない（引数配列のまま渡す）
        result = runner(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise RevealError(
            "Finder 表示コマンド（open）が見つかりません（macOS 以外では使えません）"
        ) from e
    except OSError as e:
        raise RevealError(f"Finder を開けませんでした（{e}）") from e

    code = getattr(result, "returncode", 0)
    if isinstance(code, int) and code != 0:
        stderr = (getattr(result, "stderr", "") or "").strip().splitlines()
        tail = stderr[-1] if stderr else ""
        suffix = f": {tail}" if tail else ""
        raise RevealError(
            f"Finder を開けませんでした（終了コード {code}）{suffix}"
        )
