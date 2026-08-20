#!/usr/bin/env python3
"""ATELIER H3 Local Studio / MiniMax-H3 ワーカープロセス（設計書 §22・付録A、P2固定契約）。

このスクリプトは **DiffSynth-Studio の venv** で、cwd=DiffSynth-Studio ルートとして
起動される（RealEngine が起動する。契約 §1）。したがって:

- アプリ側パッケージ（``app.*``）を import しない。標準ライブラリ ＋ DiffSynth venv 内の
  パッケージ（torch / diffsynth / PIL）だけで自己完結する。
- torch / diffsynth の import は**モジュールトップレベルで行わない**（関数内の遅延 import）。
  これにより純粋関数（検証・イベント整形・エラー分類・進捗ラッパ）を、torch の無い
  アプリ venv 上のユニットテストから検証できる。

P4 で `params.keyframe_path` が null 以外を取りうるようになった（継続生成。設計書 §10.2）。
イベント種別は増やさず、ワーカーは keyframe を **data_root 配下・実在・PNG・PIL で
デコード可・576×320 ちょうど**として再検証したうえで
`pipe(..., keyframes=[画像], keyframe_indices=[0])` を呼ぶ（P5契約 §5.1）。
違反はすべて **input 系の非 fatal**（`fatal=false`）で報告し、ワーカーは生存し続ける
（モデル・LoRA を捨てて5分の再初期化を強いない）。
**null のとき（単発生成）の呼び出し形は P2 と一字も変わらない。**

プロトコル（1行1 JSON）:
  stdin  : {"cmd":"generate"|"ping"|"shutdown", ...}
  stdout : ``@@EVT `` 接頭辞つきの JSON（stage / ready / progress / done / error ＋ 制御応答 pong）
  stderr : 人間向けログ（traceback 全文を含む）。プロトコルには載せない。

成果物は ``*.partial`` パスへ**だけ**書き、正式名への昇格は一切行わない（設計書 §10.7）。
（MP4 だけは PyAV の仕様上 ``.mp4`` 拡張子の隠し一時ファイルへ書いてから ``os.replace()`` で
partial へ移す。詳細は ``temp_encode_path`` の docstring。正式名は決して作らない。）

終了コード:
  0 = 正常終了（shutdown / stdin EOF）
  2 = 起動時の環境変数・資産検証エラー
  3 = モデル / LoRA 初期化エラー
  4 = 生成中の fatal エラー（P2 では自動再起動しないのでプロセスを終える）

パイプライン呼び出し（MiniMaxH3Pipeline / ModelConfig / write_video_audio の用法）は
DiffSynth-Studio（Apache-2.0, https://github.com/modelscope/DiffSynth-Studio）の
利用例に基づく。帰属の詳細は THIRD-PARTY-NOTICES.md。
"""

from __future__ import annotations

import gc
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence

# cwd は DiffSynth-Studio（読み取り専用）。環境変数が未設定で手動起動された
# 場合でも、そこへ __pycache__ を作らないよう実行時フラグでも防ぐ。
sys.dont_write_bytecode = True

# --------------------------------------------------------------------------- 定数

EVENT_PREFIX = "@@EVT "

#: V1 の固定生成パラメータ（実機検証済み。勝手に広げない。設計書 §14・契約 §8）
FIXED_WIDTH = 576
FIXED_HEIGHT = 320
FIXED_FPS = 24
FIXED_AUDIO_SAMPLE_RATE = 32000
ALLOWED_NUM_FRAMES: tuple[int, ...] = (56, 124)
ALLOWED_STEPS: tuple[int, ...] = (4, 8)
SEED_MIN = 0
SEED_MAX = 2_147_483_647

PARTIAL_SUFFIX = ".partial"

#: 継続生成のキーフレーム位置（実証スクリプトと同じ「先頭固定」。契約 §1）。
#: ワイヤには載せない（UI からは渡させない）。ワーカーがこの値を使う。
KEYFRAME_INDICES: tuple[int, ...] = (0,)

#: 継続生成のキーフレーム検証メッセージ。
#: `app/engine/real_engine.py` / `app/engine/mock_engine.py` の KEYFRAME_LABEL と
#: 同じ語彙を使い、real / mock / worker のどこで弾かれても利用者に同じ言葉で伝える。
KEYFRAME_LABEL = "継続元のキーフレーム画像"

#: キーフレームとして受け付ける画像形式（親動画の最終フレームは必ず PNG）
KEYFRAME_FORMAT = "PNG"

#: ready で提示する capabilities（app/core/contracts.py の MINIMAX_H3_CAPABILITIES と同値）
CAPABILITIES: dict[str, Any] = {
    "audio": True,
    "continuation": True,
    "seed": True,
    "num_frames": list(ALLOWED_NUM_FRAMES),
    "steps": list(ALLOWED_STEPS),
    "width": FIXED_WIDTH,
    "height": FIXED_HEIGHT,
    "fps": FIXED_FPS,
    "last_frame_output": True,
    "references": {"image": False, "video": False, "audio": False},
}

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "ATELIER_DATA_ROOT",
    "ATELIER_BACKEND_ID",
    "ATELIER_MODEL_ID",
    "ATELIER_MODEL_REVISION",
    "ATELIER_PROCESSOR_ID",
    "ATELIER_LORA_PATH",
    "ATELIER_LORA_ALPHA",
)

#: 実証済みスクリプト（reference_scripts/run_h3_mac_turbo_4step_promptcheck_5sec.py）の
#: モデル構成。**一字一句変えない**（設計書 §14・契約 §7）。
MODEL_FILE_PATTERNS: tuple[str, ...] = (
    "minimax-h3-fl2va-nf4.safetensors",
    "minimax-h3-text-encoder-nf4.safetensors",
    "video_vae_nf4.safetensors",
    "audio_vae_nf4.safetensors",
)
PROCESSOR_FILE_PATTERN = "FL2VA/processor/"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_INIT_ERROR = 3
EXIT_FATAL_ERROR = 4

_OOM_PATTERNS = (
    "out of memory",
    "can't allocate",
    "cannot allocate",
    "insufficient memory",
    "not enough memory",
)
_MPS_PATTERN = re.compile(r"\b(mps|metal)\b|metal performance shaders|mtlbuffer", re.IGNORECASE)

DETAIL_TAIL_LINES = 5
DETAIL_MAX_CHARS = 800
MESSAGE_MAX_CHARS = 300


# --------------------------------------------------------------------------- 例外


class WorkerConfigError(Exception):
    """起動時の環境変数・資産の検証エラー（fatal / model_state）。"""


class InputValidationError(Exception):
    """generate コマンドの入力検証エラー（非 fatal / input）。"""


class ArtifactError(Exception):
    """成果物（partial ファイル）の自己確認に失敗した。"""


class CommandError(Exception):
    """stdin から受けた1行を解釈できなかった（非 fatal / input）。"""


# --------------------------------------------------------------------------- ログ


def log(message: str, stream: Any = None) -> None:
    """人間向けログを stderr へ出す。プロトコルを壊さないよう例外は握り潰す。"""
    target = stream if stream is not None else sys.stderr
    try:
        target.write(f"[h3_worker] {message}\n")
        target.flush()
    except Exception:  # pragma: no cover - ログ失敗で本処理を止めない
        pass


def read_max_rss_bytes() -> Optional[int]:
    """自プロセスの最大常駐セット（macOS はバイト、Linux は KiB）。取得できなければ None。"""
    try:
        import resource  # 標準ライブラリ（Windows には無いので遅延 import）

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except Exception:
        return None


def format_max_rss(raw_value: Optional[int], platform: str = sys.platform) -> str:
    """ru_maxrss を人間向け文字列にする（macOS はバイト単位、その他は KiB 単位）。"""
    if raw_value is None:
        return "rss=unavailable"
    mib = raw_value / (1024 * 1024) if platform == "darwin" else raw_value / 1024
    return f"rss={raw_value} (~{mib:.1f} MiB)"


# --------------------------------------------------------------------------- イベント出力


def _json_default(value: Any) -> str:
    try:
        return repr(value)
    except Exception:  # pragma: no cover - repr が壊れている場合の最後の砦
        return "<unserializable>"


def format_event(payload: Mapping[str, Any]) -> str:
    """イベント辞書を ``@@EVT {json}`` の1行へ整形する。

    - UTF-8 のまま出す（``ensure_ascii=False``）ので日本語がエスケープされない
    - シリアライズ不能な値があっても例外を投げず、安全な代替を返す
    - 改行は空白へ潰す（1イベント＝1行の不変条件を守る）
    """
    try:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default)
    except Exception:
        safe_type = "error"
        try:
            raw_type = payload.get("type")  # type: ignore[union-attr]
            if isinstance(raw_type, str):
                safe_type = raw_type
        except Exception:
            pass
        body = json.dumps(
            {"type": safe_type, "serialization_error": True},
            ensure_ascii=False,
        )
    return EVENT_PREFIX + body.replace("\r", " ").replace("\n", " ")


class EventEmitter:
    """stdout へのイベント出力を直列化する（契約 §3）。"""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def emit(self, payload: Mapping[str, Any]) -> bool:
        """1イベントを書き出す。成功したら True。失敗しても例外を投げない。"""
        line = format_event(payload)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
                return True
            except Exception:
                return False

    # -- 便宜メソッド ------------------------------------------------------

    def stage(self, stage: str, job_id: Optional[str] = None) -> bool:
        payload: dict[str, Any] = {"type": "stage", "stage": stage}
        if job_id is not None:
            payload["job_id"] = job_id
        return self.emit(payload)

    def progress(self, job_id: Optional[str], step: int, total: int) -> bool:
        return self.emit(
            {"type": "progress", "job_id": job_id, "step": step, "total": total}
        )

    def error(
        self,
        message: str,
        category: str,
        fatal: bool,
        job_id: Optional[str] = None,
        detail: str = "",
    ) -> bool:
        return self.emit(
            {
                "type": "error",
                "job_id": job_id,
                "fatal": bool(fatal),
                "category": category,
                "message": truncate_text(message, MESSAGE_MAX_CHARS),
                "detail": detail,
            }
        )


def truncate_text(text: str, limit: int) -> str:
    """1行要約用に文字列を切り詰める（巨大な文字列をプロトコルへ流さない）。"""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"


def tail_detail(text: str, lines: int = DETAIL_TAIL_LINES) -> str:
    """traceback の末尾数行だけを取り出す（契約 §3）。"""
    stripped = [line for line in str(text).splitlines() if line.strip()]
    tail = stripped[-lines:] if lines > 0 else []
    detail = "\n".join(tail)
    if len(detail) > DETAIL_MAX_CHARS:
        detail = detail[-DETAIL_MAX_CHARS:]
    return detail


# --------------------------------------------------------------------------- エラー分類


def classify_exception(exc: BaseException, default_category: str = "pipeline") -> tuple[str, bool]:
    """例外を (category, fatal) へ分類する（設計書 §13.3）。

    - 入力起因（InputValidationError / CommandError） → ("input", False)
    - メモリ不足（MemoryError・OOM を示す文言）        → ("oom", True)
    - MPS / Metal 由来                                 → ("mps", True)
    - それ以外は呼び出し文脈の既定カテゴリ             → (default_category, True)

    OOM を MPS より先に判定する。MPS の OOM メッセージは両方の語を含むが、
    利用者にとって有用なのは「メモリ不足」だからである（どちらも fatal で挙動は同じ）。
    """
    if isinstance(exc, (InputValidationError, CommandError)):
        return "input", False
    if isinstance(exc, (ArtifactError, OSError)):
        # 保存段階の失敗（ディスク満杯・権限・サイズ0など）。
        # pipe() は成功しておりモデル・LoRA の内部状態は健全なので、
        # ワーカーを終了させて5分の再初期化を強いる必要はない。
        return "pipeline", False

    text = f"{type(exc).__name__}: {exc}".lower()

    if isinstance(exc, MemoryError) or any(marker in text for marker in _OOM_PATTERNS):
        return "oom", True
    if _MPS_PATTERN.search(text):
        return "mps", True
    return default_category, True


# --------------------------------------------------------------------------- 設定


@dataclass(frozen=True)
class WorkerConfig:
    """環境変数から読み取った起動時設定（契約 §1）。"""

    data_root: Path
    backend_id: str
    model_id: str
    model_revision: str
    processor_id: str
    lora_path: Path
    lora_alpha: float


def load_worker_config(env: Mapping[str, str]) -> WorkerConfig:
    """必須環境変数と LoRA ファイルを検証して WorkerConfig を作る。

    不足・不正があれば WorkerConfigError（呼び出し側が fatal / model_state で報告する）。
    """
    missing = [name for name in REQUIRED_ENV_VARS if not str(env.get(name, "")).strip()]
    if missing:
        raise WorkerConfigError(
            "必須の環境変数が設定されていません: " + ", ".join(missing)
        )

    data_root = Path(str(env["ATELIER_DATA_ROOT"]).strip()).resolve()
    if not data_root.is_dir():
        raise WorkerConfigError(
            f"ATELIER_DATA_ROOT がディレクトリとして存在しません: {data_root}"
        )

    lora_path = Path(str(env["ATELIER_LORA_PATH"]).strip()).resolve()
    if not lora_path.is_file():
        raise WorkerConfigError(f"LoRA ファイルが見つかりません: {lora_path}")
    try:
        if lora_path.stat().st_size <= 0:
            raise WorkerConfigError(f"LoRA ファイルが空です: {lora_path}")
    except OSError as exc:
        raise WorkerConfigError(f"LoRA ファイルを読めません: {lora_path} ({exc})") from exc

    raw_alpha = str(env["ATELIER_LORA_ALPHA"]).strip()
    try:
        lora_alpha = float(raw_alpha)
    except (TypeError, ValueError) as exc:
        raise WorkerConfigError(
            f"ATELIER_LORA_ALPHA を数値として解釈できません: {raw_alpha!r}"
        ) from exc

    return WorkerConfig(
        data_root=data_root,
        backend_id=str(env["ATELIER_BACKEND_ID"]).strip(),
        model_id=str(env["ATELIER_MODEL_ID"]).strip(),
        model_revision=str(env["ATELIER_MODEL_REVISION"]).strip(),
        processor_id=str(env["ATELIER_PROCESSOR_ID"]).strip(),
        lora_path=lora_path,
        lora_alpha=lora_alpha,
    )


# --------------------------------------------------------------------------- 入力検証


@dataclass(frozen=True)
class GenerateJob:
    """検証済みの generate リクエスト。"""

    job_id: str
    prompt: str
    num_frames: int
    num_inference_steps: int
    seed: int
    width: int
    height: int
    fps: int
    audio_sample_rate: int
    output_partial_path: Path
    last_frame_partial_path: Path
    #: 継続生成のキーフレーム画像（単発生成は None）
    keyframe_path: Optional[Path] = None

    @property
    def is_continuation(self) -> bool:
        return self.keyframe_path is not None


def _require_int(value: Any, field: str) -> int:
    """bool を弾いたうえで int を取り出す（True は 1 として通さない）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputValidationError(f"{field} は整数で指定してください（受信値: {value!r}）")
    return value


def is_within_root(root: Path, target: Path) -> bool:
    """target が root 配下（root 自身は除く）かどうか。``..`` やシンボリックリンクは resolve 後に判定。"""
    try:
        resolved_root = Path(root).resolve()
        resolved_target = Path(target).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if resolved_target == resolved_root:
        return False
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def validate_partial_path(raw_value: Any, data_root: Path, field: str) -> Path:
    """出力先 partial パスを検証する。

    - 絶対パスの文字列であること（ワーカーの cwd は DiffSynth-Studio なので相対解釈させない）
    - ``.partial`` で終わること（正式名への直接書き込みを構造的に防ぐ。設計書 §10.7）
    - data_root 配下であること（``..`` 脱出・シンボリックリンク越えは resolve 後に拒否）
    - 親ディレクトリが存在すること
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise InputValidationError(f"{field} が指定されていません")
    text = raw_value.strip()
    if not text.endswith(PARTIAL_SUFFIX):
        raise InputValidationError(
            f"{field} は {PARTIAL_SUFFIX} で終わる必要があります（受信値: {text}）"
        )
    candidate = Path(text)
    if not candidate.is_absolute():
        raise InputValidationError(f"{field} は絶対パスで指定してください（受信値: {text}）")
    if not is_within_root(data_root, candidate):
        raise InputValidationError(
            f"{field} がデータフォルダの外を指しています（受信値: {text}）"
        )
    resolved = candidate.resolve()
    if not resolved.parent.is_dir():
        raise InputValidationError(
            f"{field} の保存先フォルダが存在しません（{resolved.parent}）"
        )
    return resolved


def validate_keyframe_path(
    raw_value: Any, data_root: Path, field: str = "keyframe_path"
) -> Optional[Path]:
    """継続生成のキーフレーム画像パスを検証する（契約 §2・設計書 §10.2）。

    ``None``（またはキー自体が無い）は**単発生成**を意味し、そのまま None を返す。
    値がある場合だけ次を検証する（UI を迂回した不正値をワーカー側でも止める）:

    - 絶対パスの文字列であること（ワーカーの cwd は DiffSynth-Studio）
    - data_root 配下であること（``..`` 脱出・シンボリックリンク越えは resolve 後に拒否）
    - ファイルとして実在すること

    画像として開けるか（RGB 変換できるか）は ``open_keyframe_image`` が確認する。
    違反はすべて InputValidationError（非 fatal / category="input"）。
    """
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise InputValidationError(
            f"{field} が空です（継続生成にはキーフレーム画像が必要です）"
        )
    text = raw_value.strip()
    candidate = Path(text)
    if not candidate.is_absolute():
        raise InputValidationError(f"{field} は絶対パスで指定してください（受信値: {text}）")
    if not is_within_root(data_root, candidate):
        raise InputValidationError(
            f"{field} がデータフォルダの外を指しています（受信値: {text}）"
        )
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise InputValidationError(
            f"{field} のキーフレーム画像が見つかりません（受信値: {text}）"
        )
    return resolved


def open_keyframe_image(
    path: Path, expected_size: tuple[int, int] = (FIXED_WIDTH, FIXED_HEIGHT)
) -> Any:
    """キーフレーム画像を PIL で開いて検証し、RGB へ変換する（実証スクリプトと同一処理）。

    ``reference_scripts/run_h3_mac_turbo_4step_clip2_samevoice.py`` の
    ``Image.open(...).convert("RGB")`` と同値。``with`` で包むのは、変換後は
    元ファイルのハンドルが不要だからで、返す画像（RGB へ変換済みの新しい Image）は同じ。

    ワーカー自身での再検証（P5契約 §5.1）:

    - PIL でデコードできること（``load()`` まで通ること）
    - **PNG であること**（親動画の最終フレームは必ず PNG で保存される）
    - **``expected_size`` ちょうど**であること（既定 576×320）

    実在と data_root 配下であることは ``validate_keyframe_path`` が先に確認している。

    PIL は torch / diffsynth と同じく**遅延 import** する（この関数を呼ばない
    ユニットテストは PIL 無しでも通る）。違反はすべて InputValidationError にして
    **非 fatal な input エラー**として報告する（ワーカーは生存し、モデルも捨てない）。
    """
    from PIL import Image  # noqa: PLC0415 - 遅延 import は設計要件

    try:
        with Image.open(str(path)) as image:
            # 壊れた PNG は load() で初めて失敗することがあるため、必ず読み切る
            image.load()
            image_format = image.format
            size = tuple(image.size)
            converted = image.convert("RGB")
    except MemoryError:  # OOM は input ではなく oom として分類させる
        raise
    except Exception as exc:  # noqa: BLE001 - 破損・非画像・権限などをまとめて input 扱い
        raise InputValidationError(
            f"{KEYFRAME_LABEL}を開けませんでした（{path.name}）: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        if image_format != KEYFRAME_FORMAT:
            raise InputValidationError(
                f"{KEYFRAME_LABEL}が {KEYFRAME_FORMAT} ではありません"
                f"（{path.name}: {image_format or '不明な形式'}）"
            )
        if size != tuple(expected_size):
            raise InputValidationError(
                f"{KEYFRAME_LABEL}の大きさが違います"
                f"（{path.name}: {size[0]}×{size[1]}）。"
                f"{expected_size[0]}×{expected_size[1]} の画像が必要です"
            )
    except InputValidationError:
        # 検証で弾く場合は変換済み画像を掴んだままにしない
        close_image(converted)
        raise
    return converted


def close_image(image: Any) -> None:
    """PIL 画像を解放する（失敗しても本処理を止めない。設計書 §14）。"""
    if image is None:
        return
    try:
        close = getattr(image, "close", None)
        if callable(close):
            close()
    except Exception as exc:  # pragma: no cover - PIL 側の失敗で生成を妨げない
        log(f"warning: キーフレーム画像を解放できませんでした: {exc}")


def validate_generate_command(
    command: Mapping[str, Any], config: WorkerConfig
) -> GenerateJob:
    """generate コマンドをワーカー側でも再検証する（UI を迂回した不正値を止める。契約 §2）。

    違反はすべて InputValidationError（非 fatal / category="input"）。
    """
    job_id = command.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise InputValidationError("job_id が指定されていません")

    backend_id = command.get("backend_id")
    if backend_id != config.backend_id:
        raise InputValidationError(
            f"backend_id が一致しません（期待: {config.backend_id} / 受信: {backend_id!r}）"
        )

    params = command.get("params")
    if not isinstance(params, Mapping):
        raise InputValidationError("params がオブジェクトではありません")

    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InputValidationError("prompt が空です")

    num_frames = _require_int(params.get("num_frames"), "num_frames")
    if num_frames not in ALLOWED_NUM_FRAMES:
        raise InputValidationError(
            f"num_frames は {list(ALLOWED_NUM_FRAMES)} のみ対応しています（受信値: {num_frames}）"
        )

    steps = _require_int(params.get("num_inference_steps"), "num_inference_steps")
    if steps not in ALLOWED_STEPS:
        raise InputValidationError(
            f"num_inference_steps は {list(ALLOWED_STEPS)} のみ対応しています（受信値: {steps}）"
        )

    width = _require_int(params.get("width"), "width")
    if width != FIXED_WIDTH:
        raise InputValidationError(f"width は {FIXED_WIDTH} 固定です（受信値: {width}）")

    height = _require_int(params.get("height"), "height")
    if height != FIXED_HEIGHT:
        raise InputValidationError(f"height は {FIXED_HEIGHT} 固定です（受信値: {height}）")

    fps = _require_int(params.get("fps"), "fps")
    if fps != FIXED_FPS:
        raise InputValidationError(f"fps は {FIXED_FPS} 固定です（受信値: {fps}）")

    audio_sample_rate = _require_int(params.get("audio_sample_rate"), "audio_sample_rate")
    if audio_sample_rate != FIXED_AUDIO_SAMPLE_RATE:
        raise InputValidationError(
            f"audio_sample_rate は {FIXED_AUDIO_SAMPLE_RATE} 固定です（受信値: {audio_sample_rate}）"
        )

    seed = _require_int(params.get("seed"), "seed")
    if not (SEED_MIN <= seed <= SEED_MAX):
        raise InputValidationError(
            f"seed は {SEED_MIN}〜{SEED_MAX} の範囲で指定してください（受信値: {seed}）"
        )

    # 継続生成（P4）。null / キー無しは単発生成で、以降の挙動は一切変わらない（契約 §2）。
    keyframe_path = validate_keyframe_path(
        params.get("keyframe_path", None), config.data_root, "keyframe_path"
    )

    output_partial_path = validate_partial_path(
        params.get("output_partial_path"), config.data_root, "output_partial_path"
    )
    last_frame_partial_path = validate_partial_path(
        params.get("last_frame_partial_path"), config.data_root, "last_frame_partial_path"
    )
    if output_partial_path == last_frame_partial_path:
        raise InputValidationError(
            "output_partial_path と last_frame_partial_path が同一です"
        )

    return GenerateJob(
        job_id=job_id,
        prompt=prompt,
        num_frames=num_frames,
        num_inference_steps=steps,
        seed=seed,
        width=width,
        height=height,
        fps=fps,
        audio_sample_rate=audio_sample_rate,
        output_partial_path=output_partial_path,
        last_frame_partial_path=last_frame_partial_path,
        keyframe_path=keyframe_path,
    )


# --------------------------------------------------------------------------- コマンド解析


def parse_command(line: str) -> dict[str, Any]:
    """stdin の1行を JSON コマンドとして解釈する。不正なら CommandError。"""
    text = str(line).strip()
    if not text:
        raise CommandError("空のコマンド行を受信しました")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise CommandError(f"コマンドを JSON として解釈できません: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CommandError("コマンドが JSON オブジェクトではありません")
    name = parsed.get("cmd")
    if not isinstance(name, str) or not name.strip():
        raise CommandError("cmd が指定されていません")
    parsed["cmd"] = name.strip()
    return parsed


# --------------------------------------------------------------------------- 進捗ラッパ


def make_progress_bar(
    job_id: str,
    emit_progress: Callable[..., Any],
    on_missing_total: Optional[Callable[[], Any]] = None,
) -> Callable[..., Iterator[Any]]:
    """``progress_bar_cmd`` 用のラッパを作る（契約 §9）。

    DiffSynth 側は ``for i, _ in enumerate(progress_bar_cmd(self.scheduler.timesteps))``
    という単一 iterable 呼び出しをデノイズループの1箇所だけで行う
    （diffsynth/pipelines/minimax_h3_audio_video.py:132）。
    ``len(scheduler.timesteps) == num_inference_steps`` なので
    （diffsynth/diffusion/flow_match.py:305-310 の ``linspace(d, 0.0, n+1)[:-1]``）、
    4ステップなら 1/4 … 4/4 の系列が得られる。

    進捗ラッパの不具合で本生成を壊さないこと（emit の例外は握り潰す）。
    """

    def progress_bar(iterable: Iterable[Any], *args: Any, **kwargs: Any) -> Iterator[Any]:
        try:
            total = len(iterable)  # type: ignore[arg-type]
        except (TypeError, AttributeError):
            total = None
        if not total and on_missing_total is not None:
            try:
                on_missing_total()
            except Exception:
                pass
        for index, item in enumerate(iterable):
            if total:
                try:
                    emit_progress(job_id, step=index + 1, total=total)
                except Exception:
                    pass
            yield item

    return progress_bar


# --------------------------------------------------------------------------- モデル初期化


def load_runtime() -> Any:
    """torch / diffsynth を遅延 import して返す（トップレベルで import しない）。"""
    # 実証スクリプトと同じ環境（RealEngine が設定済みのはず。未設定時のみ既定値を補う）。
    for name, default in (
        ("PYTORCH_ENABLE_MPS_FALLBACK", "1"),
        ("DIFFSYNTH_SKIP_DOWNLOAD", "True"),
    ):
        if not os.environ.get(name):
            os.environ[name] = default
            log(f"warning: 環境変数 {name} が未設定のため {default} を補いました")

    import torch  # noqa: PLC0415 - 遅延 import は設計要件
    from diffsynth.pipelines.minimax_h3_audio_video import (  # noqa: PLC0415
        MiniMaxH3Pipeline,
        ModelConfig,
    )
    from diffsynth.utils.data.audio_video import write_video_audio  # noqa: PLC0415

    class _Runtime:
        pass

    runtime = _Runtime()
    runtime.torch = torch  # type: ignore[attr-defined]
    runtime.MiniMaxH3Pipeline = MiniMaxH3Pipeline  # type: ignore[attr-defined]
    runtime.ModelConfig = ModelConfig  # type: ignore[attr-defined]
    runtime.write_video_audio = write_video_audio  # type: ignore[attr-defined]
    return runtime


def build_pipeline(runtime: Any, config: WorkerConfig) -> Any:
    """実証済みスクリプトと同一構成でパイプラインを構築する（設計書 §14・契約 §7）。

    reference_scripts/run_h3_mac_turbo_4step_promptcheck_5sec.py からの差分は
    「model_id / processor_id を config 由来にした」ことだけで、
    vram_config・4本のモデル構成・dtype・device・vram_limit は一字一句同じ。
    """
    torch = runtime.torch
    ModelConfig = runtime.ModelConfig

    vram_config = {
        "offload_dtype": "disk",
        "offload_device": "disk",
        "onload_dtype": "disk",
        "onload_device": "disk",
        "preparing_dtype": "disk",
        "preparing_device": "disk",
        "computation_dtype": torch.bfloat16,
        "computation_device": "mps",
    }

    return runtime.MiniMaxH3Pipeline.from_pretrained(
        torch_dtype=torch.bfloat16,
        device="mps",
        model_configs=[
            ModelConfig(
                model_id=config.model_id,
                origin_file_pattern=pattern,
                **vram_config,
            )
            for pattern in MODEL_FILE_PATTERNS
        ],
        processor_config=ModelConfig(
            model_id=config.processor_id,
            origin_file_pattern=PROCESSOR_FILE_PATTERN,
        ),
        vram_limit=0,
    )


def apply_lora(pipe: Any, config: WorkerConfig) -> None:
    """Turbo LoRA をプロセス起動時に1回だけ適用する（clear_lora は呼ばない。契約 §7）。"""
    pipe.load_lora(pipe.dit, str(config.lora_path), alpha=config.lora_alpha)


# --------------------------------------------------------------------------- 後始末


def release_job_memory(torch_module: Any = None) -> None:
    """ジョブ後の後始末（設計書 §14）。pipe と LoRA は保持したまま。"""
    gc.collect()
    try:
        mps = getattr(torch_module, "mps", None)
        empty_cache = getattr(mps, "empty_cache", None)
        if callable(empty_cache):
            empty_cache()
    except Exception as exc:  # MPS 側の失敗で生成継続を妨げない
        log(f"warning: torch.mps.empty_cache() に失敗しました: {exc}")


def temp_encode_path(partial_path: Path) -> Path:
    """MP4 エンコード用の一時ファイルパス（同一ディレクトリ内の隠しファイル）。

    PyAV（``av.open(path, mode="w")``）は**拡張子から出力コンテナ形式を推定する**ため、
    ``….mp4.partial`` へ直接書くと ``ValueError: Could not determine output format``
    になる（av 18.0.0 で実測確認済み）。そこで ``.mp4`` で終わる一時ファイルへ書いてから
    ``os.replace()`` で partial へ原子的に移す。正式名は決して作らない（設計書 §10.7）。
    """
    return partial_path.with_name("." + partial_path.name + ".tmp.mp4")


def remove_quietly(path: Path) -> None:
    """一時ファイルを best-effort で削除する（失敗しても本処理を止めない）。"""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"warning: 一時ファイルを削除できませんでした: {path} ({exc})")


def verify_partial_artifact(path: Path, label: str) -> int:
    """partial ファイルの存在とサイズ>0 を自己確認する（設計書 §10.7 手順1〜3）。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArtifactError(f"{label} を保存できませんでした（{path}）: {exc}") from exc
    if size <= 0:
        raise ArtifactError(f"{label} のサイズが 0 です（{path}）")
    return size


# --------------------------------------------------------------------------- generate 処理


def handle_generate(
    command: Mapping[str, Any],
    pipe: Any,
    runtime: Any,
    config: WorkerConfig,
    emitter: EventEmitter,
) -> bool:
    """generate コマンドを処理する。戻り値は「fatal だったか」。

    fatal=False の場合、ワーカーは生存して次のコマンドを受け付ける。
    """
    raw_job_id = command.get("job_id")
    job_id_for_error = raw_job_id if isinstance(raw_job_id, str) else None

    try:
        job = validate_generate_command(command, config)
    except InputValidationError as exc:
        category, fatal = classify_exception(exc)
        log(f"input error (job_id={job_id_for_error}): {exc}")
        emitter.error(
            message=str(exc),
            category=category,
            fatal=fatal,
            job_id=job_id_for_error,
            detail="",
        )
        return False

    warnings: list[str] = []

    def note_missing_total() -> None:
        message = "ステップ進捗を取得できませんでした（ステージ表示のみ）"
        if message not in warnings:
            warnings.append(message)
        log("warning: progress_bar_cmd の iterable から総ステップ数を取得できませんでした")

    def fail(exc: BaseException, default_category: str) -> bool:
        category, fatal = classify_exception(exc, default_category=default_category)
        detail = tail_detail(traceback.format_exc())
        log(f"error (job_id={job.job_id}, category={category}, fatal={fatal}): {exc}")
        try:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
        # 入力の不備は日本語のメッセージだけを返す。ここでクラス名を前置すると
        # 「InputValidationError: 継続元のキーフレーム画像の…」のように、内部の
        # 例外クラス名がそのまま履歴と画面へ出てしまう（分類は category で伝わる）。
        # 想定外の例外だけは、原因追跡のためにクラス名を残す。
        if isinstance(exc, (InputValidationError, CommandError)):
            message = str(exc)
        else:
            message = f"{type(exc).__name__}: {exc}"
        emitter.error(
            message=message,
            category=category,
            fatal=fatal,
            job_id=job.job_id,
            detail=detail,
        )
        return fatal

    log(
        f"generate start job_id={job.job_id} frames={job.num_frames} "
        f"steps={job.num_inference_steps} seed={job.seed} "
        f"keyframe={job.keyframe_path.name if job.keyframe_path else 'none'} "
        f"{format_max_rss(read_max_rss_bytes())}"
    )

    started_at = time.monotonic()
    video: Any = None
    audio: Any = None
    last_frame: Any = None
    keyframe_image: Any = None
    #: 継続生成のときだけ増える pipe 引数（単発生成では常に空＝呼び出し形が変わらない）
    continuation_kwargs: dict[str, Any] = {}

    try:
        progress_bar = make_progress_bar(
            job.job_id,
            lambda job_id, step, total: emitter.progress(job_id, step, total),
            on_missing_total=note_missing_total,
        )
        # テキストエンコードや latent 準備で最初の progress まで数十秒かかりうる。
        # UI を無反応にしないため、生成開始をここで通知する（§9.3）。
        emitter.stage("preparing", job_id=job.job_id)

        if job.keyframe_path is not None:
            # 継続生成: 実証済みスクリプトと同じ「PIL で開いて RGB へ変換」を行い、
            # あわせて PNG・寸法をワーカー自身で再検証する（P5契約 §5.1）。
            # モデル・LoRA は常駐のまま再利用する（再初期化はしない。契約 §2）。
            try:
                keyframe_image = open_keyframe_image(
                    job.keyframe_path, expected_size=(job.width, job.height)
                )
            except BaseException as exc:  # noqa: BLE001 - 分類して報告するため全捕捉
                # 画像が壊れている等は InputValidationError → input / 非fatal。
                # 既定を pipeline にしているのは、想定外の例外（PIL 自体が無い等の
                # 環境不良）を「利用者の入力ミス」に見せないため。
                return fail(exc, default_category="pipeline")
            continuation_kwargs["keyframes"] = [keyframe_image]
            continuation_kwargs["keyframe_indices"] = list(KEYFRAME_INDICES)

        try:
            video, audio = pipe(
                prompt=job.prompt,
                height=job.height,
                width=job.width,
                num_frames=job.num_frames,
                num_inference_steps=job.num_inference_steps,
                seed=job.seed,
                progress_bar_cmd=progress_bar,
                **continuation_kwargs,
            )
        except BaseException as exc:  # noqa: BLE001 - 分類して報告するため全捕捉
            return fail(exc, default_category="pipeline")
        finally:
            # pipe へ渡し終わったら、この辞書が画像を掴み続けないようにする
            continuation_kwargs.clear()

        emitter.stage("saving", job_id=job.job_id)

        try:
            if not video:
                raise ArtifactError("生成結果のフレームが空です")
            if len(video) != job.num_frames:
                warnings.append(
                    f"生成フレーム数が要求値と異なります（要求 {job.num_frames} / 実際 {len(video)}）"
                )
                log(
                    f"warning: frame count mismatch requested={job.num_frames} actual={len(video)}"
                )

            # PyAV は拡張子でコンテナ形式を決めるため、.mp4 の一時ファイルへ書いてから
            # partial へ原子的に移す（temp_encode_path の docstring 参照）。
            encode_path = temp_encode_path(job.output_partial_path)
            try:
                runtime.write_video_audio(
                    video=video,
                    audio=audio,
                    output_path=str(encode_path),
                    fps=job.fps,
                    audio_sample_rate=job.audio_sample_rate,
                )
                video_size = verify_partial_artifact(encode_path, "動画ファイル")
                os.replace(str(encode_path), str(job.output_partial_path))
            finally:
                remove_quietly(encode_path)

            # 最終フレームはメモリ上の PIL.Image から直接保存する（設計書 §10.1 決定D6）。
            # MP4 からの再抽出はしない。PIL は format 指定があれば拡張子に依存しない。
            last_frame = video[-1].convert("RGB")
            last_frame.save(str(job.last_frame_partial_path), format="PNG")

            verify_partial_artifact(job.output_partial_path, "動画ファイル")
            png_size = verify_partial_artifact(job.last_frame_partial_path, "最終フレーム画像")
        except BaseException as exc:  # noqa: BLE001 - 分類して報告するため全捕捉
            return fail(exc, default_category="pipeline")

        elapsed = round(time.monotonic() - started_at, 2)
        log(
            f"generate done job_id={job.job_id} elapsed={elapsed}s "
            f"mp4={video_size}B png={png_size}B"
        )
        emitter.emit(
            {
                "type": "done",
                "job_id": job.job_id,
                "elapsed_sec": elapsed,
                "output_partial_path": str(job.output_partial_path),
                "last_frame_partial_path": str(job.last_frame_partial_path),
                "seed_used": job.seed,
                "backend_id": config.backend_id,
                "model_id": config.model_id,
                "model_revision": config.model_revision,
                "num_frames": job.num_frames,
                "warnings": list(warnings),
            }
        )
        return False
    finally:
        # pipe と LoRA は保持したまま、ジョブ成果物への参照だけを捨てる（契約 §7）。
        # 継続生成で開いたキーフレーム画像もここで解放する（次のジョブへ持ち越さない）。
        continuation_kwargs.clear()
        close_image(keyframe_image)
        del keyframe_image
        del video
        del audio
        del last_frame
        release_job_memory(getattr(runtime, "torch", None))
        log(f"generate cleanup job_id={job.job_id} {format_max_rss(read_max_rss_bytes())}")


# --------------------------------------------------------------------------- コマンドループ


def run_command_loop(
    stdin_stream: Any,
    emitter: EventEmitter,
    generate_fn: Callable[[Mapping[str, Any]], bool],
) -> int:
    """stdin から1行ずつコマンドを読んで処理する。戻り値はプロセス終了コード。

    generate_fn は「fatal だったか」を返す。fatal なら追加の generate を受け付けず終了する。
    """
    while True:
        try:
            line = stdin_stream.readline()
        except Exception as exc:  # 親プロセスが落ちた等
            log(f"stdin を読めなくなりました: {exc}")
            return EXIT_OK
        if line == "":  # EOF（親プロセスがパイプを閉じた）
            log("stdin が閉じられたため終了します")
            return EXIT_OK
        if not line.strip():
            continue

        try:
            command = parse_command(line)
        except CommandError as exc:
            category, fatal = classify_exception(exc)
            log(f"command error: {exc}")
            emitter.error(message=str(exc), category=category, fatal=fatal, job_id=None)
            continue

        name = command["cmd"]
        if name == "ping":
            # ワイヤ上の制御応答（EngineEvent には変換されない。契約 §3）
            emitter.emit({"type": "pong"})
            continue
        if name == "shutdown":
            log("shutdown コマンドを受信しました")
            return EXIT_OK
        if name == "generate":
            fatal = generate_fn(command)
            if fatal:
                log("fatal エラーのためワーカーを終了します")
                return EXIT_FATAL_ERROR
            continue

        message = f"未知のコマンドです: {name}"
        log(message)
        emitter.error(
            message=message,
            category="input",
            fatal=False,
            job_id=command.get("job_id") if isinstance(command.get("job_id"), str) else None,
        )

    return EXIT_OK  # pragma: no cover - 到達しない


# --------------------------------------------------------------------------- main


def _configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_streams()
    emitter = EventEmitter(sys.stdout)

    # 1) 起動時検証
    try:
        config = load_worker_config(os.environ)
    except WorkerConfigError as exc:
        log(f"config error: {exc}")
        emitter.error(
            message=str(exc), category="model_state", fatal=True, job_id=None, detail=""
        )
        return EXIT_CONFIG_ERROR

    log(
        f"start backend_id={config.backend_id} model_id={config.model_id} "
        f"revision={config.model_revision} data_root={config.data_root} "
        f"cwd={Path.cwd()} {format_max_rss(read_max_rss_bytes())}"
    )

    # 2) モデル読込
    emitter.stage("loading_model")
    try:
        runtime = load_runtime()
        pipe = build_pipeline(runtime, config)
    except BaseException as exc:  # noqa: BLE001 - 分類して報告する
        category, _ = classify_exception(exc, default_category="model_state")
        try:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
        emitter.error(
            message=f"モデルの読み込みに失敗しました: {type(exc).__name__}: {exc}",
            category=category,
            fatal=True,
            job_id=None,
            detail=tail_detail(traceback.format_exc()),
        )
        return EXIT_INIT_ERROR
    log(f"model loaded {format_max_rss(read_max_rss_bytes())}")

    # 3) LoRA 適用（1回だけ）
    emitter.stage("loading_lora")
    try:
        apply_lora(pipe, config)
    except BaseException as exc:  # noqa: BLE001
        category, _ = classify_exception(exc, default_category="model_state")
        try:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
        emitter.error(
            message=f"Turbo LoRA の適用に失敗しました: {type(exc).__name__}: {exc}",
            category=category,
            fatal=True,
            job_id=None,
            detail=tail_detail(traceback.format_exc()),
        )
        return EXIT_INIT_ERROR
    log(f"lora applied alpha={config.lora_alpha} {format_max_rss(read_max_rss_bytes())}")

    # 4) ready
    emitter.emit(
        {
            "type": "ready",
            "backend_id": config.backend_id,
            "model_id": config.model_id,
            "model_revision": config.model_revision,
            "capabilities": CAPABILITIES,
        }
    )

    # 5) コマンドループ
    try:
        return run_command_loop(
            sys.stdin,
            emitter,
            lambda command: handle_generate(command, pipe, runtime, config, emitter),
        )
    except KeyboardInterrupt:
        log("KeyboardInterrupt を受信しました")
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
