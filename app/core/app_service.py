"""統合層（設計書 §7）。HistoryStore・Engine・JobQueue を配線し、UI へ薄いAPIを提供する。

責務:
- 起動時の履歴読込・INTERRUPTED 確定
- JobSpec の採番と出力パス決定（ID衝突回避）
- JobQueue → HistoryStore の配線（JobRecorder 実装）
- UI が読む不変スナップショットの提供

UI はこの層としか話さない。UI・キュー・履歴のいずれもモデル固有処理を持たない
（backend identity は config 由来の値をそのまま履歴へ流すだけ。設計書 §22）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core import naming
from app.core.config import AppConfig
from app.core.contracts import (
    BackendIdentity,
    JobSpec,
    JobView,
    QueueSnapshot,
    ValidationError,
    validate_job_spec,
)
from app.core.fileops import disk_free_gb, disk_state
from app.core.history import HistoryRecord, HistoryStore
from app.core.job_queue import JobQueue

log = logging.getLogger("atelier.service")

#: 二重投入をまとめる時間窓（秒。P5 §6.2）。
#:
#: スマートフォンでは「押せたか分からず続けて2回タップする」事故が起きやすい。
#: この秒数以内に**まったく同じ内容**が再投入された場合だけ、新規登録せずに
#: 直前のジョブを返す。意図的な作り直し（2秒より後の再投入）は妨げない。
SUBMIT_IDEMPOTENCY_SEC = 2.0

#: 冪等化キャッシュの上限件数（メモリ上だけの小さなキャッシュ。履歴には残さない）
_MAX_RECENT_SUBMITS = 32


def estimate_seconds(num_frames: int, steps: int, estimates: dict) -> float:
    """この設定の生成にかかる目安秒数（config の実測値から引く。設計書 §0.3）。"""
    base = float(
        estimates.get("f124_s4_sec", 810)
        if num_frames >= 124
        else estimates.get("f56_s4_sec", 390)
    )
    if steps >= 8:
        base *= float(estimates.get("step8_factor", 2.0))
    return base


def format_estimate(seconds: float) -> str:
    """「約6〜7分」のような日本語の目安表示にする。"""
    minutes = seconds / 60.0
    if minutes < 1:
        return "約1分未満"
    low = int(minutes)
    high = low + 1
    return f"約{low}〜{high}分"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def disk_block_reason(cfg: AppConfig) -> str | None:
    """空き容量が受付停止水準なら日本語の理由を返す（設計書 §13.2）。

    容量を取得できない場合は None（容量が読めないことを理由に生成を止めない）。
    """
    try:
        free = disk_free_gb(cfg.data_root)
    except OSError:
        log.warning("空き容量を取得できませんでした", exc_info=True)
        return None
    if disk_state(free, cfg.warn_free_disk_gb, cfg.stop_free_disk_gb) == "stop":
        return (
            "空き容量が不足しているため、新しい生成を受け付けられません"
            f"（残り {free:.1f}GB / 必要 {cfg.stop_free_disk_gb:.0f}GB 以上）。"
            "Finder で不要な動画を整理してください"
        )
    return None


class HistoryRecorder:
    """JobQueue の履歴通知（contracts.JobRecorder）を HistoryStore へ配線する。

    契約どおり、いかなる失敗でも例外を外へ出さない（履歴の失敗でキューを止めない）。
    """

    def __init__(
        self,
        store: HistoryStore,
        *,
        identity: BackendIdentity,
        execution_engine: str,
        app_version: str,
        data_root: Path,
    ) -> None:
        self._store = store
        self._identity = identity
        self._execution_engine = execution_engine
        self._app_version = app_version
        self._data_root = data_root

    def on_queued(self, spec: JobSpec, queued_at: datetime) -> None:
        try:
            record = HistoryRecord.from_job_spec(
                spec,
                identity=self._identity,
                execution_engine=self._execution_engine,
                app_version=self._app_version,
                data_root=self._data_root,
                created_at=queued_at,
            )
            self._store.add(record)
        except Exception:
            log.exception("履歴へのQUEUED記録に失敗しました: %s", spec.job_id)

    def on_running(self, job_id: str, started_at: datetime) -> None:
        try:
            self._store.mark_running(job_id, started_at)
        except Exception:
            log.exception("履歴のRUNNING更新に失敗しました: %s", job_id)

    def on_success(
        self,
        job_id: str,
        *,
        output_path: Path,
        last_frame_path: Path | None,
        seed_used: int | None,
        elapsed_sec: float | None,
        finished_at: datetime,
    ) -> None:
        try:
            self._store.mark_success(
                job_id,
                output_path=output_path,
                last_frame_path=last_frame_path,
                seed_used=seed_used,
                elapsed_sec=elapsed_sec,
                finished_at=finished_at,
            )
        except Exception:
            log.exception("履歴のSUCCESS更新に失敗しました: %s", job_id)

    def on_failed(
        self,
        job_id: str,
        *,
        error: str,
        category: str | None,
        elapsed_sec: float | None,
        finished_at: datetime,
    ) -> None:
        try:
            self._store.mark_failed(
                job_id,
                error=error,
                category=category,
                elapsed_sec=elapsed_sec,
                finished_at=finished_at,
            )
        except Exception:
            log.exception("履歴のFAILED更新に失敗しました: %s", job_id)

    def on_canceled(self, job_id: str, canceled_at: datetime) -> None:
        try:
            self._store.mark_canceled(job_id, canceled_at)
        except Exception:
            log.exception("履歴のCANCELED更新に失敗しました: %s", job_id)


@dataclass(frozen=True)
class CompletedVideo:
    """UI プレビュー用の完成動画情報（不変）。"""

    job_id: str
    video_path: Path
    seed_used: int | None
    elapsed_sec: float | None
    finished_at: datetime | None
    prompt_head: str


@dataclass(frozen=True)
class VideoRow:
    """③完成動画タブ・④履歴タブの1行（不変）。"""

    job_id: str
    kind: str  # "clip"（個別動画） | "concat"（連結動画）
    video_path: Path | None
    exists: bool
    created_at: datetime | None
    duration_label: str
    steps: int
    num_frames: int
    seed_requested: int | None
    seed_used: int | None
    prompt_head: str
    parent_id: str | None
    chain_length: int | None
    backend_id: str
    model_revision: str
    elapsed_sec: float | None
    concat_sources: tuple[str, ...]
    status: str
    error: str | None
    error_category: str | None
    execution_engine: str
    #: 連結行の内部種別（P5.2）: "chain"（ルートからの連結） | "manual"（指定順連結）。
    #: 個別動画では None。**表示上はどちらも「連結」だが記録先が違う**
    #: （chain＝履歴の concat_path ／ manual＝concat_manifest.json）ため内部では必ず区別する。
    concat_kind: str | None = None

    @property
    def label(self) -> str:
        mark = "🔗連結" if self.kind == "concat" else "🎬"
        missing = "" if self.exists else "（ファイルなし）"
        when = self.created_at.strftime("%m-%d %H:%M") if self.created_at else "--"
        return f"{mark} {self.job_id} / {when} / {self.duration_label}{missing}"


@dataclass(frozen=True)
class CompletedSummary:
    """③完成・編集タブの要約（P5.3-A）。長い一覧表の代わりに件数だけを出す。

    数えるのは**実際にファイルがある動画だけ**（P5.3-B）。記録だけが残った動画は
    そもそも一覧に出ないので、「欠損◯件」という欄は持たない。
    """

    total: int
    clips: int
    chain_concats: int
    manual_concats: int

    @property
    def concats(self) -> int:
        return self.chain_concats + self.manual_concats


@dataclass(frozen=True)
class ContinuationContext:
    """「この動画の続きを作る」で UI へ渡す継続元の情報（不変）。"""

    parent_id: str
    keyframe_path: Path
    seed_used: int | None
    num_frames: int
    steps: int
    prompt: str
    prompt_prefill: str
    duration_label: str
    thumbnail: Path | None
    chain_length: int = 1


#: 継続プロンプトの先頭に付ける定型（設計書 §10.5）
CONTINUATION_PREFIX = "Continue directly from the supplied first frame."


@dataclass(frozen=True)
class SubmitResult:
    """投入の結果（P5 §6.2）。二重投入だったかどうかを**戻り値で**伝える。

    例外にしないのは、二重タップが「エラー」ではなく「1件だけ登録された正常な結果」
    だからである。`duplicate=True` のとき `view` は**直前に登録済みのジョブ**を指す。
    この情報は履歴にも JobSpec にも残さない（メモリ上の小さなキャッシュだけ）。
    """

    view: JobView
    duplicate: bool = False
    message: str = ""


class AppService:
    """アプリ全体の統合サービス。UI からはこのオブジェクトだけを触る。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        history: HistoryStore,
        engine,
        queue: JobQueue,
        execution_engine: str,
    ) -> None:
        self.cfg = cfg
        self.history = history
        self.engine = engine
        self.queue = queue
        self.execution_engine = execution_engine
        self.startup_warnings: tuple[str, ...] = ()
        self._started = False
        # --- 二重投入の冪等化（P5 §6.2）。メモリ上だけ・履歴には一切残さない ---
        #: 同一内容とみなす時間窓（秒）。テストから短くできるよう公開属性にしてある
        self.submit_idempotency_sec = float(SUBMIT_IDEMPOTENCY_SEC)
        self._submit_clock = time.monotonic
        self._submit_lock = threading.Lock()
        self._recent_submits: dict[str, tuple[float, JobView]] = {}
        # 任意順序連結の台帳（P5.2）。history.json とは独立したファイル（設計書 §23）
        self.concat_manifest = self._make_concat_manifest()
        # ゴミ箱移動の直列化（P5.3-B）。二重クリックや別セッションの同時操作で
        # 同じファイルを2回動かさないための**プロセス内の小さなロック**だけ持つ
        # （永続的なロックや削除台帳は作らない。設計書 §25.4）
        self._trash_lock = threading.Lock()
        # 連結サービス（P4/P5.2）。ffmpeg レーンは生成キューと別（設計書 §7 決定D5）
        self._concat = self._make_concat_service()

    def _make_concat_manifest(self):
        """台帳を作る。作れなくても UI は起動する（任意連結だけが使えなくなる）。"""
        try:
            from app.core.concat_manifest import ConcatManifest

            return ConcatManifest(self.cfg.concat_manifest_path, self.cfg.data_root)
        except Exception:
            log.exception(
                "任意連結の記録先を初期化できませんでした（指定順の連結は無効になります）"
            )
            return None

    def _make_concat_service(self):
        """連結サービスを作る。利用できない場合も UI を起動できるよう None に落とす。"""
        try:
            from app.core.concat_service import ConcatService

            return ConcatService(
                self.cfg,
                self.history,
                ffmpeg_path=self.cfg.ffmpeg_path,
                manifest=self.concat_manifest,
            )
        except Exception:
            log.exception("連結サービスを初期化できませんでした（連結機能は無効になります）")
            return None

    # ---------------------------------------------------------------- 構築

    @classmethod
    def build(cls, cfg: AppConfig, mode: str, *, engine=None) -> "AppService":
        """config から一式を組み立てる。engine を渡さない場合はモードに応じて選ぶ。"""
        if mode not in ("mock", "real"):
            raise RuntimeError(f'engine.mode は "mock" か "real" です（指定: {mode}）')

        identity = BackendIdentity(
            backend_id=cfg.backend_id,
            display_name=cfg.backend.display_name,
            model_id=cfg.backend.model_id,
            model_revision=cfg.backend.model_revision,
        )

        if engine is None:
            if mode == "real":
                from app.engine.real_engine import RealEngine

                engine = RealEngine.from_config(cfg)
            else:
                from app.engine.mock_engine import MockEngine

                engine = MockEngine.from_config(cfg)
        else:
            # mode は履歴の execution_engine としてそのまま記録される。
            # モック成果物が real として記録されないよう、注入時も種別を照合する。
            from app.engine.mock_engine import MockEngine

            injected_is_mock = isinstance(engine, MockEngine)
            if injected_is_mock and mode != "mock":
                raise RuntimeError(
                    "MockEngine を real モードで使うことはできません"
                    "（モック生成物が実機生成として履歴に残るため）"
                )
            if not injected_is_mock and mode == "mock":
                raise RuntimeError(
                    "実機エンジンを mock モードで使うことはできません"
                )

        history = HistoryStore(cfg.history_path, cfg.data_root)
        recorder = HistoryRecorder(
            history,
            identity=identity,
            execution_engine=mode,
            app_version=cfg.version,
            data_root=cfg.data_root,
        )
        queue = JobQueue(
            engine,
            recorder,
            max_queued_jobs=cfg.max_queued_jobs,
            allow_cancel_queued=cfg.allow_cancel_queued,
            # --- P3: 自動再起動・watchdog・ディスクガード（設計書 §13.2・§13.3） ---
            max_auto_restarts=cfg.max_auto_restarts,
            restart_backoff_sec=cfg.restart_backoff_sec,
            auto_restart_worker=cfg.auto_restart_worker,
            estimate_fn=lambda nf, st: estimate_seconds(nf, st, cfg.estimates),
            stall_warn_factor=cfg.stall_warn_factor,
            stall_abort_factor=cfg.stall_abort_factor,
            intake_guard=lambda: disk_block_reason(cfg),
        )
        return cls(
            cfg, history=history, engine=engine, queue=queue, execution_engine=mode
        )

    # ---------------------------------------------------------------- 開始/停止

    def start(self) -> tuple[str, ...]:
        """履歴を読み、中断ジョブを確定し、ディスパッチャを開始する。警告一覧を返す。

        冪等。2回目以降は何もしない（再実行すると実行中ジョブを誤って
        INTERRUPTED に落としてしまうため）。
        """
        if self._started:
            return self.startup_warnings
        self._started = True

        warnings: list[str] = []
        try:
            warnings.extend(self.history.load())
            interrupted = self.history.startup_recover()
        except Exception as e:  # 履歴が読めなくてもアプリは起動する
            log.exception("履歴の読み込みに失敗しました")
            warnings.append(f"履歴を読み込めませんでした（{e}）")
            interrupted = 0
        if self.concat_manifest is not None:
            # 台帳が壊れていても起動は止めない（任意連結の一覧が空になるだけで、
            # 作成済みの MP4 は消さない。設計書 §23.5）
            try:
                warnings.extend(self.concat_manifest.load())
            except Exception as e:
                log.exception("任意連結の記録の読み込みに失敗しました")
                warnings.append(f"任意連結の記録を読み込めませんでした（{e}）")
        if interrupted:
            warnings.append(
                f"前回のアプリ終了により中断された生成が {interrupted} 件あります"
                "（自動的な再実行は行いません）"
            )
        self.startup_warnings = tuple(warnings)
        for w in warnings:
            log.warning("起動時の履歴警告: %s", w)
        self.queue.start()
        return self.startup_warnings

    def shutdown(self, timeout: float = 5.0) -> None:
        """UI 終了時の停止順序: ディスパッチャ停止 → エンジン停止（キューが両方行う）。"""
        try:
            self.queue.shutdown(timeout=timeout)
        except Exception:
            log.exception("停止処理でエラーが発生しました")
        if self._concat is not None:
            try:
                self._concat.shutdown(timeout=timeout)
            except Exception:
                log.exception("連結サービスの停止でエラーが発生しました")

    # ---------------------------------------------------------------- 投入

    def _new_job_id(self) -> str:
        """履歴・既存ファイルと衝突しないIDを採番する（設計書 §10.3）。"""
        for _ in range(20):
            job_id = naming.new_video_id()
            if self.history.get(job_id) is not None:
                continue
            if (self.cfg.outputs_dir / f"{job_id}.mp4").exists():
                continue
            return job_id
        raise RuntimeError("出力IDの採番に失敗しました（衝突が続いています）")

    def build_spec(
        self,
        *,
        prompt: str,
        num_frames: int,
        steps: int,
        seed_requested: int | None,
        parent_id: str | None = None,
        keyframe_path: Path | None = None,
    ) -> JobSpec:
        job_id = self._new_job_id()
        is_continuation = parent_id is not None or keyframe_path is not None
        spec = JobSpec(
            job_id=job_id,
            prompt=prompt,
            num_frames=num_frames,
            steps=steps,
            seed_requested=seed_requested,
            output_path=self.cfg.outputs_dir / f"{job_id}.mp4",
            last_frame_path=self.cfg.outputs_dir / f"{job_id}_last.png",
            backend_id=self.cfg.backend_id,
            job_type="continuation" if is_continuation else "single",
            parent_id=parent_id,
            keyframe_path=keyframe_path,
            # 解像度・fps は実機検証済みの固定値（config.py が変更を拒否済み）
            audio_sample_rate=self.cfg.audio_sample_rate,
        )
        validate_job_spec(
            spec,
            data_root=self.cfg.data_root,
            allowed_num_frames=self.cfg.allowed_num_frames,
            allowed_steps=self.cfg.allowed_steps,
            seed_max=self.cfg.seed_max,
        )
        return spec

    def intake_block_reason(self) -> str | None:
        """空き容量が受付停止水準なら日本語の理由を返す（設計書 §13.2）。"""
        return disk_block_reason(self.cfg)

    def check_disk_guard(self) -> None:
        """空き容量が受付停止水準なら投入を断る（設計書 §13.2）。"""
        reason = self.intake_block_reason()
        if reason:
            raise ValidationError(reason)

    def restart_worker(self, *, reason: str = "手動再起動") -> bool:
        """UI からの手動再起動（設計書 §13.3 の「唯一の詰まり解消手段」）。

        実行中ジョブは fatal 扱いで FAILED になる。HALTED からの復帰手段でもある。
        """
        try:
            return bool(self.queue.restart_worker(reason=reason))
        except Exception:
            log.exception("ワーカーの再起動に失敗しました")
            return False

    def submit_generation(
        self,
        *,
        prompt: str,
        num_frames: int,
        steps: int,
        seed_requested: int | None,
        parent_id: str | None = None,
        keyframe_path: Path | None = None,
    ) -> JobView:
        """UI からの投入口。キューへ登録して即座に戻る（生成完了を待たない）。

        `parent_id` / `keyframe_path` を渡すと継続生成になる（P4）。
        継続元の妥当性は `continuation_context()` が事前に検証している前提だが、
        ここでも `validate_job_spec` を通すので UI を迂回した指定は弾かれる。
        """
        self.check_disk_guard()
        if parent_id is not None:
            # 継続元は投入時に必ず再検証する（UI の「押した瞬間に日本語で断る」体験のため）。
            # ここを通さないとキーフレーム欠損がエンジン層まで進み FAILED になってしまう。
            ctx = self.continuation_context(parent_id)
            if keyframe_path is None:
                keyframe_path = ctx.keyframe_path
            elif Path(keyframe_path).resolve() != ctx.keyframe_path.resolve():
                raise ValidationError(
                    "継続元の最終フレーム画像が指定と一致しません（継続元を選び直してください）"
                )
        spec = self.build_spec(
            prompt=prompt,
            num_frames=num_frames,
            steps=steps,
            seed_requested=seed_requested,
            parent_id=parent_id,
            keyframe_path=keyframe_path,
        )
        view = self.queue.submit(spec)
        log.info(
            "ジョブを受け付けました: %s（%dフレーム / %dステップ）",
            spec.job_id,
            spec.num_frames,
            spec.steps,
        )
        return view

    # ------------------------------------------------ 二重投入の冪等化（P5 §6.2）

    @staticmethod
    def _idempotency_key(
        *,
        prompt: str,
        num_frames: int,
        steps: int,
        seed_requested: int | None,
        parent_id: str | None,
        keyframe_path: Path | None,
    ) -> str:
        """投入内容そのものからキーを作る（設計書 P5 §6.2 のキー定義）。

        ランダムシード（`seed_requested is None`）でも「同じ画面・同じ設定で
        続けて2回押した」ことは検出できる。時間窓を過ぎれば同じキーでも通る。
        """
        # ここでは値を変換しない（`int()` などを挟むと、不正な入力に対して
        # `validate_job_spec` の日本語エラーより先に別の例外が飛んでしまう）。
        payload = "\x1f".join(
            [
                str(prompt or ""),
                str(num_frames),
                str(steps),
                "-" if seed_requested is None else str(seed_requested),
                str(parent_id or ""),
                "" if keyframe_path is None else str(keyframe_path),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _still_pending(self, view: object) -> bool:
        """キャッシュ済みジョブが今も待機中／実行中かを履歴で確かめる。

        判定できないときは True（従来どおり「重複」とみなす）を返し、
        二重登録を防ぐ側へ倒す。
        """
        from app.core.contracts import JobStatus

        job_id = getattr(view, "job_id", None)
        if not job_id:
            return True
        try:
            rec = self.history.get(job_id)
        except Exception:
            return True
        if rec is None:
            return True
        return rec.status in (JobStatus.QUEUED, JobStatus.RUNNING)

    def _prune_recent_submits(self, now: float) -> None:
        """時間窓を過ぎたキャッシュを捨てる（呼び出し側でロック済みであること）。"""
        window = max(0.0, float(self.submit_idempotency_sec))
        expired = [k for k, (at, _v) in self._recent_submits.items() if now - at > window]
        for key in expired:
            self._recent_submits.pop(key, None)
        # 念のための上限（古い順に落とす）。通常は時間窓だけで十分小さい
        while len(self._recent_submits) > _MAX_RECENT_SUBMITS:
            oldest = min(self._recent_submits.items(), key=lambda kv: kv[1][0])[0]
            self._recent_submits.pop(oldest, None)

    def submit_generation_ex(
        self,
        *,
        prompt: str,
        num_frames: int,
        steps: int,
        seed_requested: int | None,
        parent_id: str | None = None,
        keyframe_path: Path | None = None,
    ) -> SubmitResult:
        """二重投入を短時間だけ冪等化する投入口（UI はこちらを使う）。

        `submit_idempotency_sec` 秒以内に**まったく同じ内容**が再投入された場合は
        新規登録せず、直前のジョブを `duplicate=True` で返す（例外にはしない）。
        それ以外は `submit_generation()` をそのまま呼ぶだけで、挙動は変わらない。

        ロックは投入全体を包む。ここを包まないと「ほぼ同時の2回タップ」が
        両方とも検査をすり抜けて2件登録されてしまう（生成は直列なので、
        投入自体を直列化しても待たされることはない）。
        """
        key = self._idempotency_key(
            prompt=prompt,
            num_frames=num_frames,
            steps=steps,
            seed_requested=seed_requested,
            parent_id=parent_id,
            keyframe_path=keyframe_path,
        )
        with self._submit_lock:
            now = self._submit_clock()
            self._prune_recent_submits(now)
            cached = self._recent_submits.get(key)
            if cached is not None and self._still_pending(cached[1]):
                view = cached[1]
                log.info(
                    "同一内容の連続投入を受け流しました（%.1f秒以内）: %s",
                    self.submit_idempotency_sec,
                    getattr(view, "job_id", "?"),
                )
                return SubmitResult(
                    view=view,
                    duplicate=True,
                    message="同じ内容がすでに登録されています",
                )
            if cached is not None:
                # 直前のジョブがもう待機も実行もしていない（取消・失敗・完了した）なら、
                # これは正当な「やり直し」なので通す。ここを見ないと「取消 → すぐ同じ
                # 内容で再投入」が『登録済み』と表示されるのに1件も走らない状態になる。
                self._recent_submits.pop(key, None)
            # 単発生成では parent_id / keyframe_path を**渡さない**
            # （既存の `submit_generation` 呼び出しと完全に同じ形にする）
            extra: dict = {}
            if parent_id is not None:
                extra["parent_id"] = parent_id
            if keyframe_path is not None:
                extra["keyframe_path"] = keyframe_path
            view = self.submit_generation(
                prompt=prompt,
                num_frames=num_frames,
                steps=steps,
                seed_requested=seed_requested,
                **extra,
            )
            self._recent_submits[key] = (self._submit_clock(), view)
            return SubmitResult(view=view, duplicate=False, message="")

    # ---------------------------------------------------------------- 参照

    def snapshot(self) -> QueueSnapshot:
        return self.queue.snapshot()

    def latest_completed(self) -> CompletedVideo | None:
        """最新の成功動画（実ファイルが存在するものだけ）を返す。"""
        from app.core.contracts import JobStatus

        for record in self.history.list_records(
            newest_first=True, statuses=[JobStatus.SUCCESS]
        ):
            path = self.history.to_absolute(record.output_path)
            if path is not None and path.is_file():
                head = record.prompt.strip().replace("\n", " ")
                return CompletedVideo(
                    job_id=record.id,
                    video_path=path,
                    seed_used=record.seed_used,
                    elapsed_sec=record.elapsed_sec,
                    finished_at=record.finished_at,
                    prompt_head=head[:80] + ("…" if len(head) > 80 else ""),
                )
        return None

    # ---------------------------------------------------------------- P4: 一覧

    def _chain_length(self, record) -> int | None:
        """この動画までのチェーン長（root→自分）。

        解決できない場合（親レコード欠損・循環・深さ超過）は **None** を返す。
        1 を返すと「親IDがあるのにチェーン長1」という矛盾表示になり、
        連結できない理由にもたどり着けないため。
        """
        try:
            return len(self.history.resolve_chain(record.id))
        except Exception:
            return None

    def _row(self, record, kind: str) -> VideoRow:
        rel = record.concat_path if kind == "concat" else record.output_path
        path = self.history.to_absolute(rel)
        head = (record.prompt or "").strip().replace("\n", " ")
        return VideoRow(
            job_id=record.id,
            kind=kind,
            video_path=path,
            exists=bool(path and path.is_file()),
            created_at=record.created_at,
            duration_label=record.duration_label,
            steps=record.steps,
            num_frames=record.num_frames,
            seed_requested=record.seed_requested,
            seed_used=record.seed_used,
            prompt_head=head[:80] + ("…" if len(head) > 80 else ""),
            parent_id=record.parent_id,
            chain_length=self._chain_length(record),
            backend_id=record.backend_id,
            model_revision=record.model_revision,
            elapsed_sec=record.elapsed_sec,
            concat_sources=tuple(record.concat_sources or ()),
            status=record.status.value,
            error=record.error,
            error_category=record.error_category,
            execution_engine=record.execution_engine,
            concat_kind="chain" if kind == "concat" else None,
        )

    def _manual_concat_row(self, entry) -> VideoRow:
        """任意順序連結（P5.2）の1行。履歴レコードではなく台帳から作る。

        `job_id` には成果物ID（`cm_...`）が入る。個別動画のID（`v_...`）とは
        接頭辞が違うので、`kind:job_id` の選択キーが衝突することはない。
        """
        path = self.concat_manifest.to_absolute(entry.output_path)
        return VideoRow(
            job_id=entry.id,
            kind="concat",
            video_path=path,
            exists=bool(path and path.is_file()),
            created_at=entry.created_at,
            duration_label=entry.duration_label,
            # ステップ・seed は連結成果物には無い概念なので持たせない（画面では「—」）
            steps=None,
            num_frames=entry.num_frames_total,
            seed_requested=None,
            seed_used=None,
            prompt_head="",
            parent_id=None,
            chain_length=None,
            backend_id=entry.backend_id,
            model_revision=entry.model_revision,
            elapsed_sec=None,
            concat_sources=tuple(entry.sources),
            status="success",
            error=None,
            error_category=None,
            execution_engine=entry.execution_engine,
            concat_kind="manual",
        )

    def _manual_concat_rows(self) -> list[VideoRow]:
        """台帳が壊れていても③タブを出し続ける（失敗しても空を返す）。"""
        if self.concat_manifest is None:
            return []
        try:
            return [self._manual_concat_row(e) for e in self.concat_manifest.list_entries()]
        except Exception:
            log.exception("任意連結の一覧を取得できませんでした")
            return []

    def completed_videos(self) -> list[VideoRow]:
        """③完成動画タブ用。個別動画・チェーン連結・指定順連結を新しい順で返す。

        3つの出どころを1つの表示用一覧へ合流させる唯一の場所（設計書 §23.3）。
        ここから下（UI）は VideoRow だけを見ればよく、記録先の違いを知らない。

        **表示の正本はファイルの実在**（P5.3-B・設計書 §25.1）。動画ファイルが
        無いものは、記録が残っていても完成動画として扱わない。Finder で消せば
        次の更新で消え、正式パスへ戻せば次の更新でまた出る。除外リストのような
        新しい永続データは持たない（＝ズレようがない）。
        """
        from app.core.contracts import JobStatus

        rows: list[VideoRow] = []
        for rec in self.history.list_records(newest_first=True):
            if rec.status is not JobStatus.SUCCESS:
                continue
            rows.append(self._row(rec, "clip"))
            if rec.concat_path:
                rows.append(self._row(rec, "concat"))
        rows.extend(self._manual_concat_rows())
        rows = [r for r in rows if r.exists]
        # 出どころが混ざるので、合流後に必ず新しい順へ並べ直す
        rows.sort(
            key=lambda r: (r.created_at is not None, r.created_at, r.job_id),
            reverse=True,
        )
        return rows

    def completed_summary(self) -> "CompletedSummary":
        """③完成・編集タブの要約（P5.3-A）。長い一覧表の代わりに件数だけを出す。

        `completed_videos()` と**同じ行**を数えるので、画面の件数と選択候補が
        食い違わない（別々に数え直すと必ずずれる）。数えるのは**実在する動画だけ**
        なので、欠損件数という概念そのものが無くなった（P5.3-B）。
        """
        rows = self.completed_videos()
        clips = sum(1 for r in rows if r.kind == "clip")
        chains = sum(1 for r in rows if r.concat_kind == "chain")
        manuals = sum(1 for r in rows if r.concat_kind == "manual")
        return CompletedSummary(
            total=len(rows),
            clips=clips,
            chain_concats=chains,
            manual_concats=manuals,
        )

    def concat_product_rows(self) -> list[VideoRow]:
        """連結成果物だけを新しい順で返す（④履歴タブの「連結成果物」フィルタ用）。

        ③から一覧表を無くしたため、チェーン連結（`c_*`）と指定順連結（`cm_*`）を
        表で見られる場所が④だけになる。出どころは違うが `VideoRow` に揃っている
        ので、ここでも `completed_videos()` の結果を絞るだけで済む。
        """
        return [r for r in self.completed_videos() if r.kind == "concat"]

    def concat_candidates(self) -> list[VideoRow]:
        """指定順連結（P5.2）の素材にできる動画だけを新しい順で返す。

        **成功した個別動画のみ**。連結成果物（チェーン・指定順とも）は含めない
        ので、UI の候補一覧に出た時点で「素材にできないものが選ばれる」経路が無い。
        """
        return [r for r in self.completed_videos() if r.kind == "clip"]

    def history_rows(self, status: str | None = None) -> list[VideoRow]:
        """④履歴タブ用。全状態（QUEUED/RUNNING の残存も含む）を新しい順で返す。

        **成功したジョブだけは動画の実在を条件にする**（P5.3-B・設計書 §25.1）。
        成功と書いてあるのに再生できない行を残すと、ユーザーには「消したのに
        まだ居る」としか見えないため。失敗・取消・中断・待機・実行中は
        そもそも動画を持たない記録なので、これまでどおり必ず残す。
        """
        from app.core.contracts import JobStatus

        rows: list[VideoRow] = []
        for rec in self.history.list_records(newest_first=True):
            if status and rec.status.value != status:
                continue
            row = self._row(rec, "clip")
            if rec.status is JobStatus.SUCCESS and not row.exists:
                continue
            rows.append(row)
        return rows

    def find_row(self, job_id: str, kind: str = "clip") -> VideoRow | None:
        """IDから1行を引く。`cm_...` は台帳から、それ以外は履歴から解決する。

        ブラウザから来るのは選択キー（`kind:job_id`）だけなので、
        **パスの解決はここから下（サーバ側）でしか行わない**（設計書 §15）。
        """
        from app.core.naming import is_valid_manual_concat_id

        if kind == "concat" and is_valid_manual_concat_id(str(job_id)):
            if self.concat_manifest is None:
                return None
            entry = self.concat_manifest.get(str(job_id))
            return self._manual_concat_row(entry) if entry is not None else None
        rec = self.history.get(job_id)
        return self._row(rec, kind) if rec is not None else None

    # ---------------------------------------------------------------- P4: 継続生成

    def continuation_context(self, parent_id: str) -> ContinuationContext:
        """「この動画の続きを作る」の継続元を検証して返す（設計書 §10.2・§10.5）。

        継続元にできない理由はすべて日本語の ValidationError にする。
        """
        from app.core.contracts import JobStatus

        rec = self.history.get(parent_id)
        if rec is None:
            raise ValidationError(f"継続元の動画が履歴に見つかりません: {parent_id}")
        if rec.status is not JobStatus.SUCCESS:
            raise ValidationError(
                f"成功した動画だけを継続元にできます（この動画の状態: {rec.status.value}）"
            )
        if rec.execution_engine != self.execution_engine:
            raise ValidationError(
                "継続元の実行方式が現在と異なります"
                f"（継続元: {rec.execution_engine} / 現在: {self.execution_engine}）。"
                "モックで作った動画を実機生成の継続元にはできません"
            )
        if rec.backend_id != self.cfg.backend_id:
            raise ValidationError(
                "継続元の生成バックエンドが現在の設定と異なります"
                f"（継続元: {rec.backend_id} / 現在: {self.cfg.backend_id}）"
            )
        if rec.width != 576 or rec.height != 320 or rec.fps != 24:
            raise ValidationError(
                "継続元の解像度・fps が現在の設定と異なるため継続できません"
                f"（継続元: {rec.width}×{rec.height}@{rec.fps}fps / 現在: 576×320@24fps）"
            )
        keyframe = self.history.to_absolute(rec.last_frame_path)
        if keyframe is None:
            raise ValidationError(
                "継続元の最終フレーム画像が記録されていません（古い動画の可能性があります）"
            )
        if not keyframe.is_file():
            raise ValidationError(
                f"継続元の最終フレーム画像が見つかりません: {keyframe.name}"
            )
        try:
            from PIL import Image

            with Image.open(keyframe) as img:
                img.load()
                if img.width <= 0 or img.height <= 0:
                    raise ValueError("画像サイズが不正です")
        except Exception as e:
            raise ValidationError(
                f"継続元の最終フレーム画像を読み込めません: {keyframe.name}（{e}）"
            ) from e

        # チェーンが上限に達していたら継続を断る。ここを通すと、生成には数分かかるのに
        # 以後 resolve_chain が常に失敗して「二度と連結できない動画」ができてしまう。
        from app.core.history import MAX_CHAIN_DEPTH

        try:
            chain_len = len(self.history.resolve_chain(rec.id))
        except Exception as e:
            raise ValidationError(
                f"継続元のつながりを解決できないため継続できません（{e}）"
            ) from e
        if chain_len >= MAX_CHAIN_DEPTH:
            raise ValidationError(
                f"つながりが長くなりすぎています（現在 {chain_len} 本。上限 {MAX_CHAIN_DEPTH} 本）。"
                "新しい動画として作り直してください"
            )

        prompt = rec.prompt or ""
        prefill = prompt if prompt.startswith(CONTINUATION_PREFIX) else (
            f"{CONTINUATION_PREFIX}\n{prompt}".strip()
        )
        return ContinuationContext(
            parent_id=rec.id,
            keyframe_path=keyframe,
            seed_used=rec.seed_used,
            num_frames=rec.num_frames,
            steps=rec.steps,
            prompt=prompt,
            prompt_prefill=prefill,
            duration_label=rec.duration_label,
            thumbnail=keyframe,
            chain_length=chain_len,
        )

    # ---------------------------------------------------------------- P4: 連結・Finder

    def start_concat(self, job_id: str) -> str:
        """ルートから指定動画までを連結する（バックグラウンド開始・日本語メッセージ）。"""
        if self._concat is None:
            return "連結機能を利用できません"
        try:
            self._concat.start_concat(job_id)
        except Exception as e:
            # ConcatError（2本未満・実行中など）も内部障害も、UI へは日本語で返す
            log.warning("連結を開始できませんでした: %s（%s）", job_id, e)
            return f"連結を開始できませんでした: {e}"
        return "連結を開始しました。進行状況は下に表示されます。"

    def start_custom_concat(self, job_ids) -> str:
        """指定された順番で連結する（P5.2・バックグラウンド開始・日本語メッセージ）。

        UI から受け取るのはジョブIDの並びだけ。検証もパス解決も下位層で行う。
        """
        if self._concat is None:
            return "連結機能を利用できません"
        ids = [str(v).strip() for v in (job_ids or [])]
        try:
            self._concat.start_custom_concat(ids)
        except Exception as e:
            # ConcatError（本数・実行中など）も内部障害も、UI へは日本語で返す
            log.warning("指定順の連結を開始できませんでした: %s（%s）", ids, e)
            return f"連結を開始できませんでした: {e}"
        return f"{len(ids)}本を指定した順番で連結します。進行状況は下に表示されます。"

    def concat_status(self):
        return self._concat.status() if self._concat is not None else None

    # ------------------------------------------------- P5.3-B: アプリ内ゴミ箱

    def _busy_reason(self) -> str | None:
        """生成・連結が動いていればその理由（**依存関係の検査ではない**）。

        単純なファイル競合の回避（設計書 §25.4）。実行中のジョブが書き込む
        かもしれないファイルを、同時に動かさないというだけの話。
        """
        try:
            snapshot = self.snapshot()
            if snapshot.current is not None:
                return "動画の生成または連結が実行中です。完了してから整理してください。"
        except Exception:  # pragma: no cover - スナップショットが取れない場合
            log.exception("キューの状態を取得できませんでした")
        try:
            status = self.concat_status()
            if status is not None and getattr(status, "running", False):
                return "動画の生成または連結が実行中です。完了してから整理してください。"
        except Exception:  # pragma: no cover
            log.exception("連結の状態を取得できませんでした")
        return None

    def _trash_targets(self, row: VideoRow) -> list[Path]:
        """この行に対して**移動してよいファイル**（他の動画には触れない）。

        - 個別動画: 本体の MP4 と、あれば最終フレーム PNG
        - 連結動画（チェーン・指定順とも）: 選ばれた連結 MP4 だけ

        素材・親・子・他の連結成果物は**含めない**（連動削除はしない。§25.5）。
        """
        paths: list[Path] = []
        if row.video_path is not None:
            paths.append(row.video_path)
        if row.kind == "clip":
            record = self.history.get(row.job_id)
            last_frame = (
                self.history.to_absolute(record.last_frame_path) if record else None
            )
            if last_frame is not None and last_frame.is_file():
                paths.append(last_frame)
        return paths

    def move_to_trash(self, job_id: str, kind: str = "clip") -> tuple[bool, str]:
        """選ばれた動画を `data/trash/` へ移す（成功したか, 日本語メッセージ）。

        受け取るのは**種別とID だけ**。パスはここでサーバ側のストアから引く
        （ブラウザから来たパスは一切信用しない。設計書 §15・§25.3）。
        """
        from app.core.trash_service import TrashError, move_to_trash

        kind = str(kind or "clip").strip()
        job_id = str(job_id or "").strip()
        if not job_id:
            return False, "整理する動画を選んでください。"
        if kind not in ("clip", "concat"):
            return False, f"整理できない種別です: {kind}"

        with self._trash_lock:
            busy = self._busy_reason()
            if busy:
                return False, busy

            # 実行の直前にもう一度引き直す（別のセッションが先に消しているかもしれない）
            row = self.find_row(job_id, kind)
            if row is None or not row.exists or row.video_path is None:
                return False, "動画はすでに移動されたか、見つかりません。"
            if row.job_id != job_id:  # pragma: no cover - 解決結果の取り違え防止
                return False, "動画はすでに移動されたか、見つかりません。"

            try:
                moved = move_to_trash(
                    self._trash_targets(row), data_root=self.cfg.data_root
                )
            except TrashError as e:
                log.warning("ゴミ箱へ移動できませんでした: %s:%s（%s）", kind, job_id, e)
                return False, str(e)
            except Exception as e:  # pragma: no cover - 想定外
                log.exception("ゴミ箱への移動で予期しないエラー: %s:%s", kind, job_id)
                return False, f"ゴミ箱へ移動できませんでした（{e}）"

        names = "・".join(source.name for source, _ in moved)
        log.info("ゴミ箱へ移動: %s:%s（%s）", kind, job_id, names)
        return True, "✅ 動画をアプリのゴミ箱へ移動しました。"

    def reveal_in_finder(self, target, kind: str = "clip") -> str:
        """Finder で成果物を表示する（日本語メッセージを返す）。

        `target` がジョブIDのときは `kind`（"clip" | "concat"）で
        個別動画か連結動画かを選ぶ。絶対パスならそのまま使う。
        """
        from app.core.reveal import reveal_in_finder

        path = target
        if isinstance(target, str) and not str(target).startswith("/"):
            row = self.find_row(target, kind)
            path = row.video_path if row else None
        if path is None:
            return "表示できるファイルがありません"
        try:
            reveal_in_finder(Path(path), data_root=self.cfg.data_root)
            return f"Finder で表示しました: {Path(path).name}"
        except Exception as e:
            return f"Finder で表示できませんでした: {e}"

    def estimate_text(self, num_frames: int, steps: int) -> str:
        return format_estimate(estimate_seconds(num_frames, steps, self.cfg.estimates))
