"""テスト用フィクスチャ。

`fake_h3_worker.py` は **独立した実行スクリプト**であり、`app.*` を import しない
（RealEngine が本物のワーカーと同じ扱いで subprocess 起動できるようにするため）。
このパッケージからは import せず、パス指定だけに使う。
"""

from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent

#: RealEngine の worker_script へ渡す偽ワーカーの絶対パス
FAKE_WORKER = FIXTURES_DIR / "fake_h3_worker.py"
