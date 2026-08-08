"""ログ管理: 回転ファイルログ＋UI表示用リングバッファ。設計書 §7。"""

from __future__ import annotations

import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ring: deque[str] = deque(maxlen=400)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(self.format(record))
        except Exception:  # ログ経路でアプリを落とさない
            pass


def setup_logging(
    logs_dir: Path,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    logger = logging.getLogger("atelier")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        logs_dir / "app.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    ring = _RingHandler()
    ring.setFormatter(fmt)
    logger.addHandler(ring)
    return logger


def recent_logs(n: int = 100) -> str:
    """UIの折りたたみ表示用に直近ログを返す。"""
    lines = list(_ring)[-n:]
    return "\n".join(lines) if lines else "（ログはまだありません）"
