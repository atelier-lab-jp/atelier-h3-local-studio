"""Execution Engine の共通契約（設計書 §16.2・§22.2・付録A）。

`RealEngine`（P2）と `MockEngine`（P1）は同じ契約を満たし、
上位層（JobQueue・履歴・UI）は**どちらが動いているか知らない**。

ここに置くのは P1 に必要な最小限の型だけであり、
プラグイン機構・動的読込・骨組みだけの抽象層は作らない（設計書 §22.2 の方針）。

イベントの向き:
- エンジン → 上位層は `contracts.EngineEvent` の一方向キューのみ（`poll_event`）。
- 上位層 → エンジンはメソッド呼び出しのみ（`start` / `submit` / `shutdown` / `restart`）。
- `poll_event` の消費者は**単一のディスパッチャ**を前提とする（複数消費者は想定しない）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.config import BackendConfig
from app.core.contracts import (
    BackendIdentity,
    Capabilities,
    EngineEvent,
    EngineState,
    JobSpec,
)

__all__ = ["Engine", "backend_identity"]


@runtime_checkable
class Engine(Protocol):
    """実行エンジンの共通インタフェース（real / mock 共通）。

    実装は「非ブロッキングで指示を受け、進捗をイベントで返す」ことだけを担い、
    ジョブの直列化・履歴更新・UI 反映は上位層（JobQueue / AppService）が行う。
    """

    @property
    def identity(self) -> BackendIdentity:
        """Generation Backend の識別情報（設計書 §22.2）。履歴へそのまま記録される。"""
        ...

    @property
    def capabilities(self) -> Capabilities:
        """バックエンドが提示する能力（V1 は minimax_h3 の固定値）。"""
        ...

    def state(self) -> EngineState:
        """現在のエンジン状態（設計書 §9.2）。"""
        ...

    def start(self) -> None:
        """初期化を開始する（非ブロッキング）。

        STAGE(loading_model) → STAGE(loading_lora) → READY の順にイベントを発行する。
        二重呼び出しは無視する。
        """
        ...

    def submit(self, spec: JobSpec) -> None:
        """1件の生成を開始する（非ブロッキング）。

        入力は下位層でも許可リスト検証し、違反は `ValidationError` を送出する。
        READY でなければ `EngineBusyError` を送出する（同時実行は常に最大1件）。
        """
        ...

    def poll_event(self, timeout: float | None = None) -> EngineEvent | None:
        """イベントを1件取り出す（消費者は単一のディスパッチャを前提）。

        timeout 経過でイベントが無ければ None を返す。ビジーウェイトはしない。
        timeout=None は次のイベントまでブロックする（`shutdown()` で解除される）。
        """
        ...

    def shutdown(self, timeout: float = 5.0) -> None:
        """内部スレッド・ワーカーを停止する。二重呼び出し安全・デッドロックしない。"""
        ...

    def restart(self) -> None:
        """エンジンを作り直す（設計書 §13.3 の fatal 後再起動）。

        real は worker プロセスの再起動、mock は再初期化にあたる。

        P3 で確定した契約（real / mock で完全に同一。上位層は両者を区別しない）:

        1. **実行中ジョブがあれば、再起動を始める前に**
           `EngineEvent(ERROR, job_id=<実行中>, fatal=True, category=WORKER_DEAD)` を発行する。
           これが無いと、ディスパッチャが終端イベントを待ち続けてキューが永久に止まる。
        2. その後に内部を停止し、再初期化して
           `STAGE(loading_model)` → `STAGE(loading_lora)` → `READY` を**再送**する。
           上位層は READY の受領をもって「再起動完了」とみなす（`restart()` は非ブロッキング）。
        3. **イベントキューを作り直さない**（1 の中断 ERROR を確実に届けるため）。
        4. **shutdown 済みのエンジンでは `EngineBusyError`**。停止済みエンジンの復活を
           許すと、アプリ終了処理と競合してワーカープロセスが孤児として残る。
        5. 二重呼び出し安全・デッドロックしない・スレッドを増殖させない。
        """
        ...


def backend_identity(backend: BackendConfig) -> BackendIdentity:
    """config の `[backends.<id>]` から BackendIdentity を作る（設計書 §22.2）。

    real / mock のどちらでも同じ identity を履歴へ記録するための唯一の変換点。
    """
    return BackendIdentity(
        backend_id=backend.backend_id,
        display_name=backend.display_name,
        model_id=backend.model_id,
        model_revision=backend.model_revision,
    )
