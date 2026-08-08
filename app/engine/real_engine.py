"""RealEngine: 実機ワーカー（h3_worker.py）を子プロセスとして駆動する Execution Engine。

設計書 §6.2（2プロセス分離）・§10.7（原子的保存）・§13.3（fatal と再起動）・
§16.2（エンジン共通契約）・§22（バックエンド境界）・付録A/A.1（ワーカープロトコル）。

このモジュールが担うのは「プロセス管理」「プロトコル解析」「検証と昇格」の3つだけで、
モデル固有の知識は一切持たない（それはワーカー側の責務）。

スレッド構成（すべて daemon。名前は `real-engine-` 接頭辞）:

    stdout reader ──┐
                    ├─→ wire queue ─→ handler（イベント解析・検証・昇格・EngineEvent 発行）
    process waiter ─┘
    stderr reader ────────────────→ worker.log

- reader スレッドは**行を読んで振り分けるだけ**にし、重い処理（ffmpeg デコード検証）は
  handler スレッドへ渡す。これによりパイプが詰まってワーカーが書き込みでブロックする
  デッドロックを構造的に避ける（stdout / stderr はそれぞれ独立に常時 drain される）。
- プロセス終了は専用の waiter スレッドが検知し、**reader を join してから** wire queue へ
  終了マーカーを積む。これで「死ぬ直前に出した done / error」を取りこぼさない。

イベントの意味（付録A.1）:
- ワーカー → RealEngine（ワイヤ）: `done` は **partial パス**を運ぶ。
- RealEngine → 上位層（プロセス内 EngineEvent）: `DONE` は **昇格後の正式パス**を運ぶ。
  検証・昇格が完了した後にのみ DONE を発行する（planned path へのフォールバックは禁止）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
import traceback
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core import ffmpeg_ops, fileops
from app.core.contracts import (
    FATAL_CATEGORIES,
    MINIMAX_H3_CAPABILITIES,
    BackendIdentity,
    Capabilities,
    EngineBusyError,
    EngineEvent,
    EngineState,
    ErrorCategory,
    EventType,
    JobSpec,
    JobStage,
    ValidationError,
    validate_job_spec,
    resolve_seed,
)
from app.engine.base import backend_identity

log = logging.getLogger("atelier.engine.real")

#: stdout のうちイベントとして解析する行の接頭辞（付録A）
EVENT_PREFIX = "@@EVT "

THREAD_PREFIX = "real-engine-"
STDOUT_THREAD_NAME = THREAD_PREFIX + "stdout"
STDERR_THREAD_NAME = THREAD_PREFIX + "stderr"
HANDLER_THREAD_NAME = THREAD_PREFIX + "handler"
WAITER_THREAD_NAME = THREAD_PREFIX + "waiter"
STARTUP_TIMER_NAME = THREAD_PREFIX + "startup-timeout"

WORKER_LOG_MAX_BYTES = 5 * 1024 * 1024
WORKER_LOG_BACKUP_COUNT = 3

#: terminate / kill 後にプロセス消滅を待つ上限（秒）
TERMINATE_WAIT_SEC = 5.0
KILL_WAIT_SEC = 2.0
#: reader スレッドが EOF を検出するまでの待ち上限（waiter スレッド内）
READER_JOIN_SEC = 5.0
#: restart() が内部停止に使う待ち上限の下限（秒）
RESTART_TIMEOUT_SEC = 5.0

#: 再起動で中断されたジョブへ返すメッセージ（MockEngine と同一。上位層は real/mock を区別しない）
RESTART_INTERRUPT_MESSAGE = "生成ワーカーの再起動により生成を中断しました"
RESTART_INTERRUPT_DETAIL = "このジョブは自動再実行されません"

#: 継続生成のキーフレーム検証メッセージ（MockEngine と同一文言。real/mock で拒否理由を揃える）
KEYFRAME_LABEL = "継続元のキーフレーム画像"
KEYFRAME_NOT_ABSOLUTE = KEYFRAME_LABEL + "は絶対パスで指定してください"
KEYFRAME_OUTSIDE_ROOT = KEYFRAME_LABEL + "がアプリのデータ領域の外です"
KEYFRAME_NOT_FOUND = KEYFRAME_LABEL + "が見つかりません"

_STAGE_BY_NAME: dict[str, JobStage] = {stage.value: stage for stage in JobStage}
_CATEGORY_BY_NAME: dict[str, ErrorCategory] = {c.value: c for c in ErrorCategory}

#: 初期化中にだけ意味を持つステージ（job_id を持たない）
_INIT_STAGES = (JobStage.LOADING_MODEL, JobStage.LOADING_LORA)
_INIT_STATE_BY_STAGE = {
    JobStage.LOADING_MODEL: EngineState.INITIALIZING_MODEL,
    JobStage.LOADING_LORA: EngineState.INITIALIZING_LORA,
}


def _one_line(e: BaseException) -> str:
    text = str(e).strip() or e.__class__.__name__
    return " ".join(text.split())


def validate_keyframe(data_root: Path, keyframe_path: Path) -> Path:
    """継続生成のキーフレームを**送信前に**検証する（契約 §2・設計書 §10.2）。

    `validate_job_spec` は「継続生成なら keyframe が data_root 配下であること」までを
    見るが、実在までは見ない。存在しない画像をワーカーへ送ってもワーカーが input
    エラーを返すだけなので、ここで同期例外にして 1 往復を省き、失敗理由を投入時点で
    利用者へ返す。

    **`app/engine/mock_engine.py` の同名関数と同一の判定・同一の日本語文言**にすること
    （上位層から見た real / mock の契約を完全に一致させる。§16.2）。
    """
    path = Path(keyframe_path)
    if not path.is_absolute():
        raise ValidationError(f"{KEYFRAME_NOT_ABSOLUTE}: {path}")
    try:
        resolved = fileops.ensure_within(data_root, path)
    except fileops.FileopsError as e:
        raise ValidationError(f"{KEYFRAME_OUTSIDE_ROOT}: {path}") from e
    if not resolved.is_file():
        raise ValidationError(f"{KEYFRAME_NOT_FOUND}: {path}")
    return path


def _tail_traceback(lines: int = 5) -> str:
    return "\n".join(traceback.format_exc().strip().splitlines()[-lines:])


# ---------------------------------------------------------------- worker.log


class _WorkerLog:
    """ワーカーの生出力（`@@EVT` でない stdout と stderr）を worker.log へ書く。

    アプリ本体のロガー階層には載せない（数千行の出力で app.log を溢れさせないため）。
    書き込み失敗は握り潰す（ログ経路でアプリを落とさない）。
    """

    def __init__(
        self,
        path: Path,
        max_bytes: int = WORKER_LOG_MAX_BYTES,
        backup_count: int = WORKER_LOG_BACKUP_COUNT,
    ) -> None:
        self._lock = threading.Lock()
        self._handler: RotatingFileHandler | None = None
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._handler = handler
        except Exception:  # pragma: no cover - 権限・容量などの環境依存
            log.warning("worker.log を開けませんでした: %s", path, exc_info=True)

    def write(self, channel: str, text: str) -> None:
        handler = self._handler
        if handler is None:
            return
        record = logging.LogRecord(
            name="atelier.engine.worker",
            level=logging.INFO,
            pathname="(worker)",
            lineno=0,
            msg="[%s] %s",
            args=(channel, text.rstrip()),
            exc_info=None,
        )
        try:
            with self._lock:
                handler.handle(record)
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        handler = self._handler
        self._handler = None
        if handler is None:
            return
        try:
            with self._lock:
                handler.close()
        except Exception:  # pragma: no cover
            pass


@dataclass
class _RunningJob:
    spec: JobSpec
    started_monotonic: float
    #: 実際にワーカーへ渡した seed（ランダム指定はエンジン層で採番する）
    seed_used: int = 0


# ---------------------------------------------------------------- エンジン


class RealEngine:
    """`app.engine.base.Engine` 契約を満たす実機実装（設計書 §16.2・§22.2）。

    上位層（JobQueue / 履歴 / UI）から見た契約は MockEngine と完全に同一であり、
    「昇格が終わってから DONE、DONE を見てから履歴 SUCCESS」（§10.7）を守る。
    """

    def __init__(
        self,
        *,
        identity: BackendIdentity,
        worker_python: Path,
        worker_script: Path,
        working_directory: Path,
        data_root: Path,
        model_id: str,
        model_revision: str,
        processor_id: str,
        lora_path: Path,
        lora_alpha: float,
        capabilities: Capabilities = MINIMAX_H3_CAPABILITIES,
        ffmpeg_path: str = "",
        worker_log_path: Path | None = None,
        startup_timeout: float = 1800.0,
        shutdown_grace: float = 10.0,
    ) -> None:
        self._identity = identity
        self._capabilities = capabilities
        self._worker_python = Path(worker_python)
        self._worker_script = Path(worker_script)
        self._working_directory = Path(working_directory)
        self._data_root = Path(data_root)
        self._model_id = str(model_id)
        self._model_revision = str(model_revision)
        self._processor_id = str(processor_id)
        self._lora_path = Path(lora_path)
        self._lora_alpha = float(lora_alpha)
        self._ffmpeg_path = ffmpeg_path
        self._startup_timeout = float(startup_timeout)
        self._shutdown_grace = max(float(shutdown_grace), 0.0)
        self._worker_log_path = (
            Path(worker_log_path)
            if worker_log_path is not None
            else self._data_root / "logs" / "worker.log"
        )

        if identity.model_id != self._model_id or (
            identity.model_revision != self._model_revision
        ):
            # identity は履歴へ、model_id/model_revision は環境変数と handshake 照合へ使う。
            # 食い違ったまま動かすと履歴とワーカーの申告がずれるため警告する。
            log.warning(
                "identity とモデル識別子が一致しません（identity=%s/%s, 照合=%s/%s）",
                identity.model_id,
                identity.model_revision,
                self._model_id,
                self._model_revision,
            )

        self._lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        #: restart() を直列化する専用ロック（join 中に `_lock` を保持しないため別にする）
        self._restart_lock = threading.Lock()
        # None はシャットダウン時にブロック中の poll_event を起こすための番兵
        self._events: queue.Queue[EngineEvent | None] = queue.Queue()
        # ("line", str) / ("exit", int|None) / None（停止）
        self._wire: queue.Queue[tuple[str, object] | None] = queue.Queue()
        self._state = EngineState.STARTING
        self._closed = False
        #: 利用者が shutdown() を呼んだか（restart() 内部の一時停止と区別する。一度立てたら戻さない）
        self._terminated = False
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._current_job: _RunningJob | None = None
        self._ffmpeg: str | None = None
        self._worker_log: _WorkerLog | None = None
        self._startup_timer: threading.Timer | None = None
        self._ready_seen = False
        self._last_pong: float | None = None

    # ------------------------------------------------------------ 生成

    @classmethod
    def from_config(cls, cfg) -> RealEngine:
        """AppConfig から生成する（identity は `[backends.<id>]` から作る。§22.2）。

        - `worker_script` は**プロジェクト相対**（config の記述どおり）
        - `lora_relpath` は **working_directory 相対**（preflight と同じ解決）
        """
        backend = cfg.backend
        worker_script = Path(backend.worker_script)
        if not worker_script.is_absolute():
            worker_script = Path(cfg.project_root) / worker_script
        lora_path = Path(backend.lora_relpath)
        if not lora_path.is_absolute():
            lora_path = Path(backend.working_directory) / lora_path
        return cls(
            identity=backend_identity(backend),
            worker_python=backend.worker_python,
            worker_script=worker_script,
            working_directory=backend.working_directory,
            data_root=cfg.data_root,
            model_id=backend.model_id,
            model_revision=backend.model_revision,
            processor_id=backend.processor_id,
            lora_path=lora_path,
            lora_alpha=backend.lora_alpha,
            ffmpeg_path=cfg.ffmpeg_path,
            worker_log_path=cfg.logs_dir / "worker.log",
            # 実機ワーカーは生成中 stdin を読まない（pipe() 実行中）ため、
            # shutdown コマンドの猶予は必ず満了して terminate 経路になる。
            # ワーカーは SIGTERM ハンドラを持たず即座に終了するので猶予は短くてよい。
            # （長いと AppService.shutdown の 5 秒予算を超え、Ctrl+C の応答が悪化する）
            shutdown_grace=2.0,
        )

    # ------------------------------------------------------------ 契約

    @property
    def identity(self) -> BackendIdentity:
        return self._identity

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def state(self) -> EngineState:
        with self._lock:
            return self._state

    def start(self) -> None:
        """ワーカープロセスを起動する（非ブロッキング。二重呼び出しは無視）。

        起動そのものに失敗した場合は例外を投げず、DEAD へ落として
        ERROR(fatal, worker_dead) を発行する（上位層はイベント経路で一様に扱える）。
        """
        with self._lock:
            if self._closed or self._terminated:
                raise EngineBusyError(
                    "停止済みのエンジンは start() できません"
                    "（新しいエンジンを作り直してください）"
                )
            if self._proc is not None:
                return  # 二重呼び出しは無視する
            self._state = EngineState.STARTING
            self._ready_seen = False
            self._worker_log = _WorkerLog(self._worker_log_path)
            # wire はスレッド起動時のものを引き渡す（restart() が差し替えても
            # 旧世代のスレッドが新世代のキューへ書き込まないようにする）
            wire = self._wire
            argv = [str(self._worker_python), str(self._worker_script)]
            try:
                proc = subprocess.Popen(  # noqa: S603 - 引数リスト形式・shell 非経由
                    argv,
                    cwd=str(self._working_directory),
                    env=self._build_env(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    shell=False,
                )
            except OSError as e:
                self._state = EngineState.DEAD
                message = f"生成ワーカーを起動できませんでした: {_one_line(e)}"
                log.error("%s（argv=%s）", message, argv)
                self._emit(
                    EngineEvent(
                        type=EventType.ERROR,
                        fatal=True,
                        category=ErrorCategory.WORKER_DEAD,
                        message=message,
                        detail=f"python={self._worker_python} script={self._worker_script}",
                    )
                )
                return
            self._proc = proc

            readers = [
                threading.Thread(
                    target=self._read_stdout,
                    args=(proc, wire),
                    name=STDOUT_THREAD_NAME,
                    daemon=True,
                ),
                threading.Thread(
                    target=self._read_stderr,
                    args=(proc,),
                    name=STDERR_THREAD_NAME,
                    daemon=True,
                ),
            ]
            handler = threading.Thread(
                target=self._run_handler,
                args=(wire,),
                name=HANDLER_THREAD_NAME,
                daemon=True,
            )
            waiter = threading.Thread(
                target=self._wait_worker,
                args=(proc, tuple(readers), wire),
                name=WAITER_THREAD_NAME,
                daemon=True,
            )
            self._threads = [*readers, handler, waiter]
            timer = threading.Timer(self._startup_timeout, self._on_startup_timeout)
            timer.name = STARTUP_TIMER_NAME
            timer.daemon = True
            self._startup_timer = timer
            threads = list(self._threads)

        for thread in threads:
            thread.start()
        timer.start()
        log.info(
            "生成ワーカーを起動しました（pid=%s, script=%s, cwd=%s）",
            proc.pid,
            self._worker_script,
            self._working_directory,
        )

    def submit(self, spec: JobSpec) -> None:
        """1件の生成をワーカーへ渡す（非ブロッキング）。

        プロンプトは stdin の JSON だけで渡す（コマンドライン引数へは載せない。§15）。
        """
        # UI を迂回した不正値を下位層でも止める（設計書 §15・CLAUDE.md）
        validate_job_spec(spec, data_root=self._data_root)
        if spec.backend_id != self._identity.backend_id:
            raise ValidationError(
                "このエンジンでは扱えない生成バックエンドです"
                f"（指定: {spec.backend_id}、このエンジン: {self._identity.backend_id}）"
            )
        # 継続生成（P4）: 絶対パス・data_root 配下・実在をワイヤへ載せる前に確かめる。
        # 単発生成（keyframe_path=None）はここを素通りし、挙動は P2 と変わらない。
        keyframe_path = (
            validate_keyframe(self._data_root, spec.keyframe_path)
            if spec.keyframe_path is not None
            else None
        )

        try:
            spec.output_path.parent.mkdir(parents=True, exist_ok=True)
            spec.last_frame_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValidationError(
                f"出力先ディレクトリを作成できません: {_one_line(e)}"
            ) from e

        with self._lock:
            if (
                self._closed
                or self._terminated
                or self._state in (EngineState.DEAD, EngineState.HALTED)
            ):
                raise EngineBusyError("エンジンが停止しています")
            if self._current_job is not None:
                raise EngineBusyError(
                    "すでに生成を実行中です（同時に実行できる生成は1件だけです）"
                )
            if self._state is not EngineState.READY:
                raise EngineBusyError(
                    "エンジンが生成を受け付けられる状態ではありません"
                    f"（現在: {self._state.value}）"
                )
            # ランダム指定（seed_requested=None）はエンジン層で採番する。
            # ワイヤ上の seed は int 固定（契約 §2・§8）で、ワーカーは None を拒否する。
            # MockEngine も同じ位置で resolve_seed している（real/mock の対称性）。
            seed_used = resolve_seed(spec.seed_requested)
            self._current_job = _RunningJob(
                spec=spec, started_monotonic=time.monotonic(), seed_used=seed_used
            )
            self._state = EngineState.BUSY

        command = {
            "cmd": "generate",
            "job_id": spec.job_id,
            "backend_id": spec.backend_id,
            "params": {
                "prompt": spec.prompt,
                "num_frames": spec.num_frames,
                "num_inference_steps": spec.steps,
                "seed": seed_used,
                "width": spec.width,
                "height": spec.height,
                "fps": spec.fps,
                "audio_sample_rate": spec.audio_sample_rate,
                # 継続生成のみ非 null（P4）。単発生成では従来どおり null を送る。
                "keyframe_path": (
                    str(keyframe_path) if keyframe_path is not None else None
                ),
                "output_partial_path": str(fileops.partial_path(spec.output_path)),
                "last_frame_partial_path": str(
                    fileops.partial_path(spec.last_frame_path)
                ),
            },
        }
        try:
            self._send(command)
        except Exception as e:
            # 送信できなかったジョブは「開始していない」ので ERROR イベントにはせず、
            # 同期例外で呼び出し元へ返す（JobQueue は submit 失敗を FAILED にする）。
            self._take_current_job(spec.job_id)
            with self._lock:
                if self._state is EngineState.BUSY:
                    self._state = EngineState.DEAD
            message = f"生成ワーカーへ指示を送れませんでした: {_one_line(e)}"
            log.error(message)
            raise EngineBusyError(message) from e

    def poll_event(self, timeout: float | None = None) -> EngineEvent | None:
        if timeout is not None and timeout < 0:
            timeout = 0.0
        try:
            return self._events.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self, timeout: float = 5.0) -> None:
        """ワーカーと内部スレッドを恒久的に停止する（二重呼び出し安全・デッドロックしない）。

        `{"cmd":"shutdown"}` → 猶予待ち → `terminate()` → 待ち → `kill()` の順に
        エスカレーションし、orphan worker を残さない（契約 §6）。

        `_terminated` を立てるのはここだけで、以後 `start()` / `restart()` は拒否される。
        """
        with self._lock:
            self._terminated = True
        self._teardown(timeout=timeout, wake_pollers=True)

    def _teardown(self, *, timeout: float, wake_pollers: bool) -> None:
        """ワーカーと内部スレッドを止める（shutdown と restart で共有する停止処理）。

        `wake_pollers=False` は restart 経由の一時停止で使う。番兵 `None` は
        「エンジンが終わった」の合図（`base.Engine` の契約）なので、
        再初期化して続く再起動では流さない。
        """
        with self._lock:
            already_closed = self._closed
            self._closed = True
            self._state = EngineState.HALTED
            proc = self._proc
            threads = list(self._threads)
            timer = self._startup_timer
            self._startup_timer = None
            events = self._events
            wire = self._wire

        if timer is not None:
            timer.cancel()
        if wake_pollers and not already_closed:
            events.put(None)  # ブロック中の poll_event を起こす
        wire.put(None)  # handler スレッドを終わらせる

        if proc is not None:
            self._stop_process(proc, timeout=timeout)

        # ロックを保持したまま join しない（handler スレッドが状態更新でロックを取るため）
        deadline = time.monotonic() + max(float(timeout), 0.0)
        current = threading.current_thread()
        for thread in threads:
            if thread is current or not thread.is_alive():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():  # pragma: no cover - 通常は起きない
                log.warning("エンジンのスレッドが停止しません: %s", thread.name)

        self._close_pipes(proc)
        with self._lock:
            worker_log = self._worker_log
            self._worker_log = None
        if worker_log is not None:
            worker_log.close()

    def restart(self) -> None:
        """ワーカープロセスを作り直す（設計書 §13.3 の fatal 後再起動。MockEngine と同一契約）。

        1. 実行中ジョブがあれば、**停止する前に**
           ERROR(fatal=True, category=worker_dead) を発行する。無言で終端イベントを
           失わせると、ディスパッチャが DONE / ERROR を待ち続けてキューが永久停止する。
        2. ワーカーを停止して作り直し、STAGE(loading_model) → STAGE(loading_lora)
           → READY をワーカーが改めて発行する。
        3. **イベントキューは作り直さない。** 1 の中断 ERROR と未消費のイベントを
           確実に上位層へ届けるため（ワイヤキュー `_wire` は旧ワーカーの残骸を
           持ち越さないよう作り直す。こちらは上位層へ出ない内部キュー）。
        4. `shutdown()` 済みなら `EngineBusyError`。no-op にしない理由は、
           停止済みエンジンの復活を許すとアプリ終了処理と競合してワーカープロセスが
           孤児として残りうるため（16GB 規模のプロセスが放置される）。
           呼び出し側の不具合を握り潰さない（復活はインスタンス作り直しで行う）。
        5. `_restart_lock` で直列化するため二重呼び出し・並行呼び出しでも安全。
           join はロック外の `_teardown` が行うのでデッドロックしない。

        バックオフ・連続失敗カウント・UI の再起動ボタンは JobQueue 側の責務（§13.3）。
        """
        timeout = max(self._shutdown_grace, RESTART_TIMEOUT_SEC)
        with self._restart_lock:
            if self._terminated:
                raise EngineBusyError(
                    "停止済みのエンジンは restart() できません"
                    "（新しいエンジンを作り直してください）"
                )

            # 停止より先に中断を通知する（イベントキューは作り直さないので確実に届く）
            job = self._take_current_job(None)
            if job is not None:
                log.warning(
                    "再起動のため実行中ジョブを中断します: job=%s", job.spec.job_id
                )
                self._emit(
                    EngineEvent(
                        type=EventType.ERROR,
                        job_id=job.spec.job_id,
                        fatal=True,
                        category=ErrorCategory.WORKER_DEAD,
                        message=RESTART_INTERRUPT_MESSAGE,
                        detail=RESTART_INTERRUPT_DETAIL,
                    )
                )

            self._teardown(timeout=timeout, wake_pollers=False)

            with self._lock:
                self._closed = False
                self._state = EngineState.STARTING
                self._proc = None
                self._threads = []
                self._current_job = None
                self._ready_seen = False
                self._wire = queue.Queue()
                self._ffmpeg = None
                terminated = self._terminated
            if terminated:  # 再起動中に shutdown() が入った
                self._teardown(timeout=timeout, wake_pollers=True)
                return
            self.start()

        if self._terminated:
            # start() と shutdown() が競合した場合の後始末（orphan worker を残さない）
            self._teardown(timeout=timeout, wake_pollers=True)

    # ------------------------------------------------------------ 補助（公開）

    def ping(self) -> None:
        """生存確認を送る（応答 `pong` は内部で消費する。P3 の停滞検知用の土台）。"""
        self._send({"cmd": "ping"})

    @property
    def last_pong_monotonic(self) -> float | None:
        with self._lock:
            return self._last_pong

    @property
    def worker_pid(self) -> int | None:
        with self._lock:
            return self._proc.pid if self._proc is not None else None

    # ------------------------------------------------------------ 内部：起動

    def _build_env(self) -> dict[str, str]:
        """契約 §1 の環境変数（既存 env を継承しつつ上書き）。"""
        env = dict(os.environ)
        env.update(
            {
                "DIFFSYNTH_SKIP_DOWNLOAD": "True",
                "DIFFSYNTH_MODEL_BASE_PATH": str(self._working_directory / "models"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                "PYTHONUNBUFFERED": "1",
                "ATELIER_DATA_ROOT": str(self._data_root.resolve()),
                "ATELIER_BACKEND_ID": self._identity.backend_id,
                "ATELIER_MODEL_ID": self._model_id,
                "ATELIER_MODEL_REVISION": self._model_revision,
                "ATELIER_PROCESSOR_ID": self._processor_id,
                "ATELIER_LORA_PATH": str(self._lora_path),
                "ATELIER_LORA_ALPHA": str(self._lora_alpha),
            }
        )
        return env

    def _on_startup_timeout(self) -> None:
        with self._lock:
            if self._closed or self._ready_seen:
                return
            if self._state in (
                EngineState.READY,
                EngineState.BUSY,
                EngineState.DEAD,
                EngineState.HALTED,
            ):
                return
            self._state = EngineState.HALTED
            proc = self._proc
        message = (
            f"モデルの初期化が {int(self._startup_timeout)} 秒以内に完了しませんでした"
        )
        log.error(message)
        self._emit(
            EngineEvent(
                type=EventType.ERROR,
                fatal=True,
                category=ErrorCategory.MODEL_STATE,
                message=message,
                detail="data/logs/worker.log を確認してください",
            )
        )
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------ 内部：I/O

    def _log_worker(self, channel: str, text: str) -> None:
        worker_log = self._worker_log
        if worker_log is not None:
            worker_log.write(channel, text)

    def _read_stdout(self, proc: subprocess.Popen, wire: queue.Queue) -> None:
        """stdout を常時 drain し、`@@EVT ` 行だけを handler へ渡す。"""
        stream = proc.stdout
        if stream is None:  # pragma: no cover
            return
        try:
            for line in stream:
                text = line.rstrip("\r\n")
                if text.startswith(EVENT_PREFIX):
                    wire.put(("line", text[len(EVENT_PREFIX) :]))
                elif text.strip():
                    self._log_worker("stdout", text)
        except Exception:  # pragma: no cover - パイプ破損時
            log.debug("stdout の読み取りを終了します", exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover
                pass

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        """stderr を常時 drain する（大量出力でワーカーを止めないため）。"""
        stream = proc.stderr
        if stream is None:  # pragma: no cover
            return
        try:
            for line in stream:
                text = line.rstrip("\r\n")
                if text.strip():
                    self._log_worker("stderr", text)
        except Exception:  # pragma: no cover
            log.debug("stderr の読み取りを終了します", exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:  # pragma: no cover
                pass

    def _wait_worker(
        self,
        proc: subprocess.Popen,
        readers: tuple[threading.Thread, ...],
        wire: queue.Queue,
    ) -> None:
        """プロセス終了を検知する。reader が EOF に達してから終了マーカーを積む。"""
        try:
            returncode = proc.wait()
        except Exception:  # pragma: no cover
            returncode = proc.poll()
        for reader in readers:
            reader.join(READER_JOIN_SEC)
        wire.put(("exit", returncode))

    def _send(self, command: dict) -> None:
        payload = json.dumps(command, ensure_ascii=False) + "\n"
        with self._stdin_lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise EngineBusyError("生成ワーカーが起動していません")
            if proc.poll() is not None:
                raise EngineBusyError("生成ワーカーはすでに終了しています")
            proc.stdin.write(payload)
            proc.stdin.flush()

    # ------------------------------------------------------------ 内部：状態

    def _emit(self, event: EngineEvent) -> None:
        self._events.put(event)

    def _set_state(self, new_state: EngineState) -> None:
        with self._lock:
            if self._closed:
                return
            self._state = new_state

    def _release_busy(self, fatal: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            if fatal:
                self._state = EngineState.HALTED
            elif self._state is EngineState.BUSY:
                self._state = EngineState.READY

    def _peek_current_job(self, job_id, *, what: str) -> _RunningJob | None:
        """実行中ジョブを取得する（job_id 不一致・実行中なしはログ警告して None）。"""
        with self._lock:
            job = self._current_job
        if job is None:
            log.warning("実行中ジョブが無いのに %s を受信しました（無視）: %s", what, job_id)
            return None
        if job_id is not None and job.spec.job_id != job_id:
            log.warning(
                "job_id が実行中ジョブと一致しない %s を無視します（受信=%s / 実行中=%s）",
                what,
                job_id,
                job.spec.job_id,
            )
            return None
        return job

    def _take_current_job(self, job_id) -> _RunningJob | None:
        """実行中ジョブを取り出して確定させる（二重 done を構造的に防ぐ）。"""
        with self._lock:
            job = self._current_job
            if job is None:
                return None
            if job_id is not None and job.spec.job_id != job_id:
                return None
            self._current_job = None
            return job

    # ------------------------------------------------------------ 内部：解析

    def _run_handler(self, wire: queue.Queue) -> None:
        while True:
            item = wire.get()
            if item is None:
                return
            kind, payload = item
            try:
                if kind == "line":
                    self._handle_wire_line(str(payload))
                elif kind == "exit":
                    self._handle_worker_exit(payload)
                    return
            except Exception:  # noqa: BLE001 - 解析失敗でエンジンを壊さない
                log.exception("ワーカーイベントの処理に失敗しました")

    def _handle_wire_line(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            # 不正 JSON はログ化して無視する（アプリを落とさない。契約 §3）
            log.warning("ワーカーから不正な JSON を受信しました（無視）: %s", raw[:200])
            self._log_worker("stdout", EVENT_PREFIX + raw)
            return
        if not isinstance(payload, dict):
            log.warning("ワーカーのイベントが JSON オブジェクトではありません（無視）")
            return

        etype = payload.get("type")
        if etype == "pong":
            # 生存確認用。EngineEvent には変換しない（契約 §3）
            with self._lock:
                self._last_pong = time.monotonic()
            return
        if etype == "stage":
            self._handle_stage(payload)
        elif etype == "ready":
            self._handle_ready(payload)
        elif etype == "progress":
            self._handle_progress(payload)
        elif etype == "done":
            self._handle_done(payload)
        elif etype == "error":
            self._handle_error(payload)
        else:
            log.warning("未知のイベント種別を無視します: %r", etype)

    def _handle_stage(self, payload: dict) -> None:
        stage = _STAGE_BY_NAME.get(str(payload.get("stage") or ""))
        if stage is None:
            log.warning("未知のステージを無視します: %r", payload.get("stage"))
            return
        if stage in _INIT_STAGES:
            with self._lock:
                if not self._ready_seen and not self._closed:
                    self._state = _INIT_STATE_BY_STAGE[stage]
            self._emit(EngineEvent(type=EventType.STAGE, stage=stage))
            return
        job = self._peek_current_job(payload.get("job_id"), what="stage")
        if job is None:
            return
        self._emit(
            EngineEvent(type=EventType.STAGE, job_id=job.spec.job_id, stage=stage)
        )

    def _handle_ready(self, payload: dict) -> None:
        with self._lock:
            self._ready_seen = True
            timer = self._startup_timer
            self._startup_timer = None
        if timer is not None:
            timer.cancel()

        problems = self._handshake_problems(payload)
        if problems:
            message = "生成バックエンドが設定と一致しません: " + " / ".join(problems)
            log.error(message)
            # READY にせず HALTED で止める（壊れた組合せで生成させない）。
            # ワーカーは殺さない（shutdown() が確実に片付ける）。
            self._set_state(EngineState.HALTED)
            self._emit(
                EngineEvent(
                    type=EventType.ERROR,
                    fatal=True,
                    category=ErrorCategory.MODEL_STATE,
                    message=message,
                    detail="\n".join(problems),
                )
            )
            return

        self._set_state(EngineState.READY)
        self._emit(
            EngineEvent(
                type=EventType.READY,
                backend_id=self._identity.backend_id,
                capabilities=self._capabilities,
            )
        )
        log.info(
            "生成ワーカーが準備完了になりました（%s / %s / %s）",
            self._identity.backend_id,
            self._model_id,
            self._model_revision,
        )

    def _handshake_problems(self, payload: dict) -> list[str]:
        """ready の申告を config・P1契約（MINIMAX_H3_CAPABILITIES）と照合する（契約 §5）。"""
        problems: list[str] = []

        def check(label: str, actual, expected) -> None:
            if actual != expected:
                problems.append(f"{label}（ワーカー: {actual!r} / 設定: {expected!r}）")

        check("backend_id", payload.get("backend_id"), self._identity.backend_id)
        check("model_id", payload.get("model_id"), self._model_id)
        check("model_revision", payload.get("model_revision"), self._model_revision)

        caps = payload.get("capabilities")
        if not isinstance(caps, dict):
            problems.append("capabilities が報告されていません")
            return problems

        expected = self._capabilities
        for key, exp in (
            ("audio", expected.audio),
            ("seed", expected.seed),
            ("last_frame_output", expected.last_frame_output),
        ):
            if caps.get(key) is not exp:
                problems.append(
                    f"capabilities.{key}（ワーカー: {caps.get(key)!r} / 設定: {exp!r}）"
                )
        for key, exp_seq in (
            ("num_frames", list(expected.num_frames)),
            ("steps", list(expected.steps)),
        ):
            got = caps.get(key)
            try:
                got_list = [int(v) for v in got]  # type: ignore[union-attr]
            except Exception:
                got_list = None
            if got_list != exp_seq:
                problems.append(
                    f"capabilities.{key}（ワーカー: {got!r} / 設定: {exp_seq!r}）"
                )
        for key, exp_int in (
            ("width", expected.width),
            ("height", expected.height),
            ("fps", expected.fps),
        ):
            if caps.get(key) != exp_int:
                problems.append(
                    f"capabilities.{key}（ワーカー: {caps.get(key)!r} / 設定: {exp_int!r}）"
                )

        # continuation / references は V1 の UI が消費しないため、
        # 不一致でも READY を止めずログ警告に留める（§22.2）。
        if caps.get("continuation") != expected.continuation:
            log.warning(
                "capabilities.continuation が設定と一致しません（ワーカー: %r / 設定: %r）",
                caps.get("continuation"),
                expected.continuation,
            )
        if caps.get("references") != expected.references:
            log.warning(
                "capabilities.references が設定と一致しません（ワーカー: %r / 設定: %r）",
                caps.get("references"),
                expected.references,
            )
        return problems

    def _handle_progress(self, payload: dict) -> None:
        job_id = payload.get("job_id")
        if job_id is None:
            log.warning("job_id の無い progress を無視します")
            return
        job = self._peek_current_job(job_id, what="progress")
        if job is None:
            return
        try:
            step = int(payload.get("step"))
            total = int(payload.get("total"))
        except (TypeError, ValueError):
            log.warning("progress の step/total が不正です（無視）: %r", payload)
            return
        self._emit(
            EngineEvent(
                type=EventType.PROGRESS,
                job_id=job.spec.job_id,
                step=step,
                total=total,
            )
        )

    def _handle_done(self, payload: dict) -> None:
        """done を検証し、昇格が完了した後にのみ DONE を発行する（契約 §4）。"""
        job_id = payload.get("job_id")
        if job_id is None:
            log.warning("job_id の無い done を無視します")
            return
        with self._lock:
            current = self._current_job
        if current is None:
            log.warning(
                "実行中ジョブが無い done を無視します（二重 done の可能性）: job_id=%s",
                job_id,
            )
            return
        if current.spec.job_id != job_id:
            log.warning(
                "job_id が実行中ジョブと一致しない done を無視します（受信=%s / 実行中=%s）",
                job_id,
                current.spec.job_id,
            )
            return
        job = self._take_current_job(job_id)
        if job is None:  # pragma: no cover - 直前に他スレッドが確定させた場合
            log.warning("done の処理中にジョブが確定済みになりました: %s", job_id)
            return

        spec = job.spec
        warnings: list[str] = [
            str(w) for w in (payload.get("warnings") or []) if str(w).strip()
        ]
        try:
            output_path, last_frame_path = self._verify_and_promote(
                spec, payload, warnings
            )
        except Exception as e:  # noqa: BLE001 - 検証失敗はジョブ単位の失敗
            message = f"生成結果を検証できませんでした: {_one_line(e)}"
            log.error("%s（job=%s）", message, spec.job_id)
            self._emit(
                EngineEvent(
                    type=EventType.ERROR,
                    job_id=spec.job_id,
                    fatal=False,
                    category=ErrorCategory.PIPELINE,
                    message=message,
                    detail=_tail_traceback(),
                )
            )
            self._release_busy()
            return

        elapsed = payload.get("elapsed_sec")
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
            elapsed = time.monotonic() - job.started_monotonic
        seed_used = payload.get("seed_used")
        if not isinstance(seed_used, int) or isinstance(seed_used, bool):
            # ワーカーが返さなかった場合は、実際に渡した値を使う
            # （spec.seed_requested はランダム指定時 None なので使えない）
            seed_used = job.seed_used

        self._emit(
            EngineEvent(
                type=EventType.DONE,
                job_id=spec.job_id,
                elapsed_sec=round(float(elapsed), 3),
                output_path=output_path,
                last_frame_path=last_frame_path,
                seed_used=seed_used,
                backend_id=self._identity.backend_id,
                model_id=self._identity.model_id,
                model_revision=self._identity.model_revision,
                warnings=tuple(warnings),
            )
        )
        self._release_busy()
        log.info(
            "生成が完了しました: job=%s output=%s", spec.job_id, output_path.name
        )

    def _handle_error(self, payload: dict) -> None:
        category = _CATEGORY_BY_NAME.get(
            str(payload.get("category") or ""), ErrorCategory.PIPELINE
        )
        fatal = payload.get("fatal")
        if not isinstance(fatal, bool):
            fatal = category in FATAL_CATEGORIES
        message = str(payload.get("message") or "").strip() or (
            "生成ワーカーがエラーを報告しました"
        )
        detail = str(payload.get("detail") or "")
        job_id = payload.get("job_id")

        target_job_id: str | None = None
        if job_id is not None:
            with self._lock:
                current = self._current_job
            if current is None or current.spec.job_id != job_id:
                log.warning(
                    "job_id が実行中ジョブと一致しない error を無視します"
                    "（受信=%s / 実行中=%s）",
                    job_id,
                    current.spec.job_id if current else None,
                )
                return
            job = self._take_current_job(job_id)
            target_job_id = job.spec.job_id if job is not None else None
        else:
            job = self._take_current_job(None)
            target_job_id = job.spec.job_id if job is not None else None

        log.error(
            "ワーカーがエラーを報告しました（job=%s, fatal=%s, category=%s）: %s",
            target_job_id,
            fatal,
            category.value,
            message,
        )
        self._emit(
            EngineEvent(
                type=EventType.ERROR,
                job_id=target_job_id,
                fatal=fatal,
                category=category,
                message=message,
                detail=detail,
            )
        )
        self._release_busy(fatal=fatal)

    def _handle_worker_exit(self, returncode) -> None:
        """ワーカー終了を状態へ反映する（契約 §6）。P2 では自動再起動しない。"""
        with self._lock:
            if self._closed:
                return  # shutdown() による正常終了
            job = self._current_job
            self._current_job = None
            self._state = EngineState.DEAD
            timer = self._startup_timer
            self._startup_timer = None
        if timer is not None:
            timer.cancel()

        log.error("生成ワーカーが終了しました（終了コード: %s）", returncode)
        if job is None:
            return
        # 実行中に死んだ場合はイベントを合成する（キューが待ち続けないこと）
        self._emit(
            EngineEvent(
                type=EventType.ERROR,
                job_id=job.spec.job_id,
                fatal=True,
                category=ErrorCategory.WORKER_DEAD,
                message=f"生成ワーカーが異常終了しました（終了コード: {returncode}）",
                detail="data/logs/worker.log を確認してください",
            )
        )

    # ------------------------------------------------------------ 内部：昇格

    def _resolve_ffmpeg(self) -> str:
        with self._lock:
            if self._ffmpeg is None:
                self._ffmpeg = ffmpeg_ops.resolve_ffmpeg(self._ffmpeg_path)
            return self._ffmpeg

    def _require_partial(
        self, reported, expected: Path, label: str, warnings: list[str]
    ) -> Path:
        if not isinstance(reported, str) or not reported.strip():
            raise fileops.FileopsError(
                f"{label}の保存先がワーカーから報告されませんでした"
            )
        path = Path(reported)
        if not path.is_absolute():
            raise fileops.FileopsError(
                f"{label}の保存先が絶対パスではありません: {reported}"
            )
        # data_root 配下であること（§15。ここを通さない限り昇格しない）
        fileops.ensure_within(self._data_root, path)
        # ワーカーは resolve() 済みのパスを返すため、data_root への経路に
        # シンボリックリンクがあると素の比較では毎回不一致になる。解決後で比べる。
        if path.resolve() != expected.resolve():
            # 指示した partial 以外は受理しない。data_root 配下でありさえすれば
            # 通してしまうと、promote() の os.replace が既存の完成動画を
            # 別ジョブの成果物で上書きしうる（多重防御の要）。
            raise fileops.FileopsError(
                f"{label}の保存先が指示と異なります"
                f"（ワーカー: {path} / 指示: {expected}）"
            )
        if not path.is_file():
            raise fileops.FileopsError(f"{label}が作成されていません: {path.name}")
        return path

    def _verify_and_promote(
        self, spec: JobSpec, payload: dict, warnings: list[str]
    ) -> tuple[Path, Path]:
        """partial を検証して正式名へ昇格する（設計書 §10.7・契約 §4）。

        MP4 → PNG の順に昇格し、PNG の昇格に失敗した場合は昇格済みの MP4 を撤去する
        （履歴に載らない孤児 MP4 を残さない）。planned path へのフォールバックはしない。
        """
        video_partial = self._require_partial(
            payload.get("output_partial_path"),
            fileops.partial_path(spec.output_path),
            "出力動画",
            warnings,
        )
        png_partial = self._require_partial(
            payload.get("last_frame_partial_path"),
            fileops.partial_path(spec.last_frame_path),
            "最終フレーム画像",
            warnings,
        )
        # 昇格先も data_root 配下であることを下位層で再検証する
        fileops.ensure_within(self._data_root, spec.output_path)
        fileops.ensure_within(self._data_root, spec.last_frame_path)

        fileops.verify_nonempty(video_partial)  # 手順3: サイズ>0
        # PNG を先に検証しておき、「動画だけ昇格して PNG で失敗」を減らす
        fileops.verify_png(png_partial)

        # 手順4: ffmpeg デコードで映像＋音声と再生時間を確認する
        ffmpeg = self._resolve_ffmpeg()
        validate_video = ffmpeg_ops.video_validator(ffmpeg, spec.num_frames / spec.fps)

        spec.output_path.parent.mkdir(parents=True, exist_ok=True)
        spec.last_frame_path.parent.mkdir(parents=True, exist_ok=True)

        fileops.promote(video_partial, spec.output_path, (validate_video,))
        try:
            fileops.promote(png_partial, spec.last_frame_path, (fileops.verify_png,))
        except Exception:
            spec.output_path.unlink(missing_ok=True)  # ロールバック
            raise
        return spec.output_path, spec.last_frame_path

    # ------------------------------------------------------------ 内部：停止

    @staticmethod
    def _wait_process(proc: subprocess.Popen, seconds: float) -> bool:
        try:
            proc.wait(timeout=max(float(seconds), 0.0))
            return True
        except subprocess.TimeoutExpired:
            return False
        except Exception:  # pragma: no cover
            return proc.poll() is not None

    def _stop_process(self, proc: subprocess.Popen, timeout: float) -> None:
        if proc.poll() is not None:
            return
        try:
            self._send({"cmd": "shutdown"})
        except Exception:
            log.debug("shutdown コマンドを送れませんでした", exc_info=True)
        if self._wait_process(proc, self._shutdown_grace):
            return

        log.warning(
            "ワーカーが shutdown に応答しないため terminate します（pid=%s）", proc.pid
        )
        try:
            proc.terminate()
        except Exception:  # pragma: no cover
            pass
        if self._wait_process(
            proc, min(max(float(timeout), 0.5), TERMINATE_WAIT_SEC)
        ):
            return

        log.warning(
            "ワーカーが terminate に応答しないため kill します（pid=%s）", proc.pid
        )
        try:
            proc.kill()
        except Exception:  # pragma: no cover
            pass
        if not self._wait_process(proc, KILL_WAIT_SEC):  # pragma: no cover
            log.error("生成ワーカーを終了できませんでした（pid=%s）", proc.pid)

    @staticmethod
    def _close_pipes(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:  # pragma: no cover
                pass
