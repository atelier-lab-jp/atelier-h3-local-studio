"""P1 共有契約（型・列挙・検証）。設計書 §9・§11・§16・§22・付録A。

このモジュールは HistoryStore / JobQueue / Engine（MockEngine）/ UI が共有する
**唯一の契約**であり、各層はここで定義された型だけを介してやり取りする。

責務境界（P1）:
- JobSpec を作るのは統合層（AppService）。ID採番・出力パス決定もそこで行う。
- JobQueue は直列ディスパッチと状態遷移を担い、履歴更新は JobRecorder 経由で委譲する
  （HistoryStore へ直接依存しない）。
- Engine は成果物を partial から検証・昇格し、**昇格後にのみ** DONE を発行する。
  履歴 SUCCESS は DONE 受領後に JobQueue が JobRecorder へ通知して確定する。

パスの境界:
- JobSpec / EngineEvent / UI が扱うパスは**絶対パス**。
- 履歴 JSON に保存されるパスは **data_root からの相対パス**（HistoryStore が境界で変換）。

イベント種別は設計書付録Aの範囲を超えて増やさない（STAGE / READY / PROGRESS / DONE / ERROR）。
なお付録Aの JSON Lines（P2のワーカー↔UIプロセス間）は partial パスを運ぶのに対し、
本モジュールの EngineEvent（プロセス内）は**昇格後の正式パス**を運ぶ。
RealEngine（P2）が partial の受領→検証→昇格→正式パスでの DONE 発行を担うため、
上位層（JobQueue / 履歴 / UI）の契約は real / mock で完全に同一になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------- 例外


class ValidationError(Exception):
    """入力検証エラー（日本語メッセージ）。UI・キュー・エンジンの各層で送出しうる。"""


class QueueFullError(Exception):
    """キュー上限超過（日本語メッセージ）。"""


class EngineBusyError(Exception):
    """READY でない、または実行中のエンジンへ投入した（内部エラー）。"""


# ---------------------------------------------------------------- 列挙


class JobStatus(str, Enum):
    """ジョブ状態（設計書 §9.1）。サブ状態は JobStage で表し、状態機械は増やさない。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.CANCELED, JobStatus.INTERRUPTED}
)

# 許可される状態遷移（設計書 §9.1）。これ以外は拒否する。
#
# QUEUED → FAILED を意図的に許可していない：ジョブは必ず RUNNING を経てから
# 失敗する。ディスパッチ前に投入を断るケース（入力不正・キュー上限・空き容量不足）は
# そもそも履歴レコードを作らず、エンジン停止（P3 の HALTED）で待機し続けるジョブは
# QUEUED のまま残してアプリ終了時に INTERRUPTED へ確定する。
# こうすると「失敗＝一度は実行を試みた」という不変条件が保てる。
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.RUNNING, JobStatus.CANCELED, JobStatus.INTERRUPTED}
    ),
    JobStatus.RUNNING: frozenset(
        {JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.INTERRUPTED}
    ),
    JobStatus.SUCCESS: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELED: frozenset(),
    JobStatus.INTERRUPTED: frozenset(),
}


def can_transition(current: JobStatus, new: JobStatus) -> bool:
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


class JobStage(str, Enum):
    """表示専用のサブ状態（設計書 §9.1・§9.3）。"""

    LOADING_MODEL = "loading_model"
    LOADING_LORA = "loading_lora"
    PREPARING = "preparing"
    GENERATING = "generating"
    SAVING = "saving"


class EngineState(str, Enum):
    """Execution Engine の状態（設計書 §9.2）。"""

    STARTING = "starting"
    INITIALIZING_MODEL = "initializing_model"
    INITIALIZING_LORA = "initializing_lora"
    READY = "ready"
    BUSY = "busy"
    DEAD = "dead"
    HALTED = "halted"


class RestartState(str, Enum):
    """ワーカー再起動の進行状態（P3・設計書 §13.3）。"""

    IDLE = "idle"              # 通常運転
    BACKOFF = "backoff"        # 再起動前のバックオフ待機中
    RESTARTING = "restarting"  # 再初期化中（エンジンは STARTING → READY へ）
    HALTED = "halted"          # 連続失敗が上限を超えた。手動対応待ち


class EventType(str, Enum):
    """エンジンイベント種別（設計書 付録A。これ以上増やさない）。"""

    STAGE = "stage"
    READY = "ready"
    PROGRESS = "progress"
    DONE = "done"
    ERROR = "error"


class ErrorCategory(str, Enum):
    """エラー分類（設計書 §13.3）。INPUT のみ非fatal、他は fatal。"""

    MPS = "mps"
    OOM = "oom"
    PIPELINE = "pipeline"
    MODEL_STATE = "model_state"
    WORKER_DEAD = "worker_dead"
    INPUT = "input"


FATAL_CATEGORIES = frozenset(
    {
        ErrorCategory.MPS,
        ErrorCategory.OOM,
        ErrorCategory.PIPELINE,
        ErrorCategory.MODEL_STATE,
        ErrorCategory.WORKER_DEAD,
    }
)


# ---------------------------------------------------------------- 固定仕様

FIXED_WIDTH = 576
FIXED_HEIGHT = 320
FIXED_FPS = 24
ALLOWED_NUM_FRAMES: tuple[int, ...] = (56, 124)
ALLOWED_STEPS: tuple[int, ...] = (4, 8)
SEED_MAX = 2_147_483_647
SUPPORTED_BACKENDS: tuple[str, ...] = ("minimax_h3",)
MOCK_FAIL_PREFIX = "[MOCK_FAIL]"

#: 表示用の長さラベル（履歴の duration_label）
DURATION_LABELS: dict[int, str] = {56: "2.33秒", 124: "5.17秒"}


def duration_label(num_frames: int) -> str:
    return DURATION_LABELS.get(num_frames, f"{num_frames / FIXED_FPS:.2f}秒")


# ---------------------------------------------------------------- backend identity


@dataclass(frozen=True)
class BackendIdentity:
    """Generation Backend の識別情報（設計書 §22.2）。履歴へそのまま記録される。"""

    backend_id: str
    display_name: str
    model_id: str
    model_revision: str


@dataclass(frozen=True)
class Capabilities:
    """バックエンドが提示する能力（設計書 §22.2）。V1 は minimax_h3 の固定値。"""

    audio: bool
    continuation: bool
    seed: bool
    num_frames: tuple[int, ...]
    steps: tuple[int, ...]
    width: int
    height: int
    fps: int
    last_frame_output: bool
    references: dict[str, bool] = field(
        default_factory=lambda: {"image": False, "video": False, "audio": False}
    )

    def to_dict(self) -> dict:
        return {
            "audio": self.audio,
            "continuation": self.continuation,
            "seed": self.seed,
            "num_frames": list(self.num_frames),
            "steps": list(self.steps),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "last_frame_output": self.last_frame_output,
            "references": dict(self.references),
        }


#: V1 唯一のバックエンドの capability（実機検証済み仕様。設計書 §22.2）
MINIMAX_H3_CAPABILITIES = Capabilities(
    audio=True,
    continuation=True,
    seed=True,
    num_frames=ALLOWED_NUM_FRAMES,
    steps=ALLOWED_STEPS,
    width=FIXED_WIDTH,
    height=FIXED_HEIGHT,
    fps=FIXED_FPS,
    last_frame_output=True,
)


# ---------------------------------------------------------------- ジョブ


@dataclass(frozen=True)
class JobSpec:
    """1件の生成依頼（不変）。統合層が採番・パス決定して作る。

    パスはすべて**絶対パス**。output_path / last_frame_path は昇格後の正式名で、
    エンジンは `.partial` を付けて書き込んでから昇格する（設計書 §10.7）。
    """

    job_id: str
    prompt: str
    num_frames: int
    steps: int
    seed_requested: int | None  # None = ランダム（エンジンが採番して seed_used を返す）
    output_path: Path
    last_frame_path: Path
    backend_id: str = SUPPORTED_BACKENDS[0]
    #: "single" | "continuation"（継続生成は P4）| "start_image"（開始画像は P8）
    job_type: str = "single"
    parent_id: str | None = None
    #: 第1フレームとして渡す画像（継続生成の親の最終フレーム、または P8 の開始画像）。
    #: 単発生成では常に None（P1 と同じ）。
    keyframe_path: Path | None = None
    width: int = FIXED_WIDTH
    height: int = FIXED_HEIGHT
    fps: int = FIXED_FPS
    audio_sample_rate: int = 32000

    @property
    def duration_label(self) -> str:
        return duration_label(self.num_frames)

    @property
    def prompt_head(self) -> str:
        head = self.prompt.strip().replace("\n", " ")
        return head[:80] + ("…" if len(head) > 80 else "")


@dataclass(frozen=True)
class JobView:
    """UI へ渡す不変スナップショット（内部の可変オブジェクトを露出させない）。"""

    job_id: str
    status: JobStatus
    prompt_head: str
    num_frames: int
    steps: int
    duration_label: str
    seed_requested: int | None
    seed_used: int | None = None
    stage: JobStage | None = None
    step: int | None = None
    total_steps: int | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_sec: float | None = None
    error: str | None = None
    error_category: str | None = None
    output_path: Path | None = None  # 絶対パス（昇格済みのみ）
    last_frame_path: Path | None = None
    #: 最後にエンジンからイベントが届いた時刻（UI の「最終処理中」推定に使う）
    last_event_at: datetime | None = None
    #: 停滞警告が出ているか（設計書 §13.2。自動停止はしない）
    stalled: bool = False


@dataclass(frozen=True)
class QueueSnapshot:
    """キュー全体の不変スナップショット（UI は Timer でこれを読む）。"""

    engine_state: EngineState
    current: JobView | None
    queued: tuple[JobView, ...]
    queue_size: int
    last_finished: JobView | None
    running: bool  # ディスパッチャが稼働中か
    accepted_total: int = 0
    succeeded_total: int = 0
    failed_total: int = 0
    # --- P3: 自動再起動とディスクガード（設計書 §13.2・§13.3） ---
    restart_state: RestartState = RestartState.IDLE
    #: 連続失敗回数（ジョブが1本成功した時点で 0 に戻る）
    consecutive_failures: int = 0
    #: バックオフの残り秒（restart_state=BACKOFF のときのみ意味を持つ）
    backoff_remaining_sec: float = 0.0
    #: HALTED / 受付停止の理由（日本語。UI の赤色バナー用）
    halted_reason: str | None = None
    #: 再起動の実施回数（累計。UI 表示と診断用）
    restart_total: int = 0
    #: 空き容量による受付停止中か（設計書 §13.2）
    intake_blocked_reason: str | None = None


# ---------------------------------------------------------------- イベント


@dataclass(frozen=True)
class EngineEvent:
    """エンジンが発行する不変イベント（設計書 付録A の in-process 版）。

    DONE のパスは**昇格後の正式な絶対パス**（partial ではない）。
    """

    type: EventType
    job_id: str | None = None
    # STAGE
    stage: JobStage | None = None
    # READY
    backend_id: str | None = None
    capabilities: Capabilities | None = None
    # PROGRESS
    step: int | None = None
    total: int | None = None
    # DONE
    elapsed_sec: float | None = None
    output_path: Path | None = None
    last_frame_path: Path | None = None
    seed_used: int | None = None
    model_id: str | None = None
    model_revision: str | None = None
    warnings: tuple[str, ...] = ()
    # ERROR
    fatal: bool | None = None
    category: ErrorCategory | None = None
    message: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------- プロトコル


@runtime_checkable
class JobRecorder(Protocol):
    """JobQueue が履歴を更新するための委譲先（HistoryStore への直接依存を避ける）。

    統合層（AppService）が HistoryStore へ配線する。テストではフェイクを渡す。
    すべてのメソッドは例外を投げないこと（記録失敗でキューを止めない）。
    """

    def on_queued(self, spec: JobSpec, queued_at: datetime) -> None: ...

    def on_running(self, job_id: str, started_at: datetime) -> None: ...

    def on_success(
        self,
        job_id: str,
        *,
        output_path: Path,
        last_frame_path: Path | None,
        seed_used: int | None,
        elapsed_sec: float | None,
        finished_at: datetime,
    ) -> None: ...

    def on_failed(
        self,
        job_id: str,
        *,
        error: str,
        category: str | None,
        elapsed_sec: float | None,
        finished_at: datetime,
    ) -> None: ...

    def on_canceled(self, job_id: str, canceled_at: datetime) -> None: ...


# ---------------------------------------------------------------- 入力検証


def validate_job_spec(
    spec: JobSpec,
    *,
    data_root: Path | None = None,
    allowed_num_frames: tuple[int, ...] = ALLOWED_NUM_FRAMES,
    allowed_steps: tuple[int, ...] = ALLOWED_STEPS,
    seed_max: int = SEED_MAX,
    supported_backends: tuple[str, ...] = SUPPORTED_BACKENDS,
) -> None:
    """UI を迂回しても不正値が通らないよう、下位層でも呼ぶ共通検証（日本語エラー）。

    data_root を渡した場合、出力パスが data_root 配下であることも検証する。
    """
    if not spec.prompt or not spec.prompt.strip():
        raise ValidationError("プロンプトを入力してください")

    if spec.num_frames not in allowed_num_frames:
        allowed = " / ".join(str(v) for v in allowed_num_frames)
        raise ValidationError(
            f"動画の長さが不正です（指定: {spec.num_frames}フレーム、許可: {allowed}フレーム）"
        )

    if spec.steps not in allowed_steps:
        allowed = " / ".join(str(v) for v in allowed_steps)
        raise ValidationError(
            f"ステップ数が不正です（指定: {spec.steps}、許可: {allowed}）"
        )

    if spec.width != FIXED_WIDTH or spec.height != FIXED_HEIGHT:
        raise ValidationError(
            f"解像度は {FIXED_WIDTH}×{FIXED_HEIGHT} 固定です"
            f"（指定: {spec.width}×{spec.height}）"
        )

    if spec.fps != FIXED_FPS:
        raise ValidationError(f"fps は {FIXED_FPS} 固定です（指定: {spec.fps}）")

    if spec.seed_requested is not None:
        if not isinstance(spec.seed_requested, int) or isinstance(
            spec.seed_requested, bool
        ):
            raise ValidationError("シード値は整数で指定してください")
        if not (0 <= spec.seed_requested <= seed_max):
            raise ValidationError(
                f"シード値は 0〜{seed_max} の範囲で指定してください"
                f"（指定: {spec.seed_requested}）"
            )

    if spec.backend_id not in supported_backends:
        allowed = " / ".join(supported_backends)
        raise ValidationError(
            f"未対応の生成バックエンドです（指定: {spec.backend_id}、許可: {allowed}）"
        )

    if spec.job_type not in ("single", "continuation", "start_image"):
        raise ValidationError(f"ジョブ種別が不正です: {spec.job_type}")

    # 種別ごとに必須項目は排他（P4・P8）。UI を迂回した中途半端な指定を弾く。
    #
    # **単発（single）が keyframe_path を拒否する条件は P8 でも一切緩めていない**。
    # 開始画像からの生成は job_type="start_image" という別種別として通し、
    # 「単発生成なのに画像が付いている」という不整合は今までどおり必ず失敗させる。
    if spec.job_type == "continuation":
        if not spec.parent_id:
            raise ValidationError("継続生成には継続元（親動画）のIDが必要です")
        if spec.keyframe_path is None:
            raise ValidationError("継続生成には親動画の最終フレーム画像が必要です")
        if data_root is not None:
            _require_within(data_root, spec.keyframe_path, "継続元のキーフレーム画像")
    elif spec.job_type == "start_image":
        # 開始画像からの生成（P8）。継続生成と違い親ジョブを持たない**根**になる。
        if spec.parent_id is not None:
            raise ValidationError("開始画像からの生成に継続元IDは指定できません")
        if spec.keyframe_path is None:
            raise ValidationError("開始画像からの生成には開始画像が必要です")
    else:
        if spec.parent_id is not None:
            raise ValidationError(
                "単発生成に継続元IDは指定できません（継続生成にするなら job_type を continuation に）"
            )
        if spec.keyframe_path is not None:
            raise ValidationError("単発生成にキーフレーム画像は指定できません")

    # キーフレーム画像があるなら、種別によらずアプリのデータ領域の中を要求する。
    # 継続生成は上でも同じ検査をしているが（先に固有の文言で断るため）、
    # ここを種別非依存にしておくと種別が増えても領域外パスの抜け道ができない。
    if data_root is not None and spec.keyframe_path is not None:
        _require_within(data_root, spec.keyframe_path, "開始画像")

    if data_root is not None:
        for label, path in (
            ("出力動画", spec.output_path),
            ("最終フレーム画像", spec.last_frame_path),
        ):
            _require_within(data_root, path, label)


def _require_within(data_root: Path, target: Path, label: str) -> None:
    try:
        resolved = Path(target).resolve()
        base = Path(data_root).resolve()
    except OSError as e:  # pragma: no cover - 実行環境依存
        raise ValidationError(f"{label}のパスを解決できません: {e}") from e
    if not resolved.is_relative_to(base):
        raise ValidationError(
            f"{label}の保存先がアプリのデータ領域の外です: {target}"
        )


def resolve_seed(seed_requested: int | None, rng=None, seed_max: int = SEED_MAX) -> int:
    """ランダム指定（None）なら 0〜seed_max から採番する（設計書 §10.4）。"""
    if seed_requested is not None:
        return int(seed_requested)
    if rng is None:
        import secrets

        return secrets.randbelow(seed_max + 1)
    return int(rng.randint(0, seed_max))
