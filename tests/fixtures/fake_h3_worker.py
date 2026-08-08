#!/usr/bin/env python3
"""RealEngine 試験用の偽ワーカー（実モデル・torch・DiffSynth を一切使わない）。

付録A／P2固定契約 §1〜§6 のワイヤプロトコルだけを模倣する独立スクリプト。
**`app.*` を import しない**（本物の h3_worker.py と同じ扱いで subprocess 起動するため）。

環境変数:

| 変数 | 意味 |
|---|---|
| `FAKE_WORKER_SCENARIO` | 挙動の切替（下表。既定 `normal`） |
| `FAKE_WORKER_ASSETS` | 実素材ディレクトリ（`app/assets/mock`）。正常系はここから**コピー**する |
| `FAKE_WORKER_OUTSIDE_DIR` | `outside_data_root` シナリオで partial を書く data_root 外のディレクトリ |
| `FAKE_WORKER_ENV_DUMP` | 指定すると受け取った環境変数・cwd を JSON で書き出す（契約 §1 の検証用） |
| `FAKE_WORKER_FLOOD` | ノイズ／stderr 大量出力の行数（既定 3000） |
| `FAKE_WORKER_KEYFRAME_DUMP` | 指定すると generate ごとに受け取った `keyframe_path` を JSON で書き出す（継続生成のワイヤ検証用） |
| `FAKE_WORKER_STATE` | 起動回数を数えるファイル。**プロセスをまたいで**シナリオを変化させる（再起動試験用） |
| `FAKE_WORKER_BAD_RUNS` | 先頭から何回の起動を異常にするか（既定 1。`*_then_ok` 系で使う） |
| `ATELIER_*` | RealEngine が渡す識別子。ready でそのまま返す |

シナリオ一覧:

| 値 | 挙動 |
|---|---|
| `normal` | stage×2 → ready → generate で progress×steps → saving → done（実素材をコピー） |
| `stdout_noise` | 上記に加えて `@@EVT` でない stdout を大量に出す |
| `stderr_flood` | 上記に加えて stderr を大量に出す |
| `bad_json` | 壊れた JSON・配列・未知 type を `@@EVT` で流してから正常動作 |
| `bad_backend_id` | ready の backend_id を偽る |
| `bad_model` | ready の model_revision を偽る |
| `bad_capabilities` | ready の capabilities を偽る |
| `stall_before_ready` | stage までで ready を出さずに待機（ready 前 submit の試験） |
| `job_id_mismatch` | progress / done / error を別の job_id で出す |
| `missing_mp4` / `missing_png` | 片方の partial を作らない |
| `invalid_mp4` / `invalid_png` | デコードできない partial を作る |
| `missing_paths` | done に partial パスを載せない |
| `outside_data_root` | data_root 外へ書き、そのパスを done で報告する |
| `double_done` | 同じ job_id の done を2回出す |
| `error_nonfatal` | error(fatal=false, category=input) を返して生存する |
| `keyframe_required` | `keyframe_path` が null なら input エラー（継続生成のワイヤが本当に届いたかを確かめる） |
| `error_fatal` | error(fatal=true, category=mps) を返して終了する |
| `exit_before_ready` | ready 前に終了する |
| `exit_after_ready` | ready 直後（アイドル時）に終了する |
| `crash_running` | generate 実行中に異常終了する |
| `ignore_shutdown` | shutdown コマンドを無視する（terminate へフォールバック） |
| `ignore_sigterm` | shutdown も SIGTERM も無視する（kill へフォールバック） |

再起動シナリオ（P3。`FAKE_WORKER_STATE` の起動カウンタで**プロセスをまたいで**挙動を変える）:

| 値 | 挙動 |
|---|---|
| `hang_in_generate` | preparing → progress 1 まで出して以後応答しない（生成中の再起動を確定的に試験する） |
| `always_fatal` | 何回再起動しても generate のたびに fatal(mps) を返す。プロセスは生き続ける（連続失敗 → HALTED の試験） |
| `fatal_then_ok` | 先頭 `FAKE_WORKER_BAD_RUNS` 回の起動では generate が fatal(mps)。以降の起動は正常（再起動で復旧する試験） |
| `dead_then_ok` | 先頭 `FAKE_WORKER_BAD_RUNS` 回の起動は ready 前に異常終了。以降の起動は正常（再起動そのものが失敗する試験） |

`FAKE_WORKER_STATE` が指すファイルには起動回数（10進整数）が入る。テストからは
`int(state_path.read_text())` で「何回ワーカーが起動したか」を検証できる。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

SCENARIO = os.environ.get("FAKE_WORKER_SCENARIO", "normal")
ASSETS_DIR = Path(os.environ.get("FAKE_WORKER_ASSETS", ""))
OUTSIDE_DIR = os.environ.get("FAKE_WORKER_OUTSIDE_DIR", "")
ENV_DUMP = os.environ.get("FAKE_WORKER_ENV_DUMP", "")
KEYFRAME_DUMP = os.environ.get("FAKE_WORKER_KEYFRAME_DUMP", "")
FLOOD_LINES = int(os.environ.get("FAKE_WORKER_FLOOD", "3000"))
STATE_PATH = os.environ.get("FAKE_WORKER_STATE", "")
BAD_RUNS = int(os.environ.get("FAKE_WORKER_BAD_RUNS", "1"))

BACKEND_ID = os.environ.get("ATELIER_BACKEND_ID", "minimax_h3")
MODEL_ID = os.environ.get("ATELIER_MODEL_ID", "DiffSynth-Studio/MiniMax-H3-NF4")
MODEL_REVISION = os.environ.get("ATELIER_MODEL_REVISION", "nf4-turbo4step-ckpt500")

#: 契約 §3 の capabilities（P1 契約 MINIMAX_H3_CAPABILITIES と同じ値）
CAPABILITIES = {
    "audio": True,
    "continuation": True,
    "seed": True,
    "num_frames": [56, 124],
    "steps": [4, 8],
    "width": 576,
    "height": 320,
    "fps": 24,
    "last_frame_output": True,
    "references": {"image": False, "video": False, "audio": False},
}

ENV_KEYS = (
    "DIFFSYNTH_SKIP_DOWNLOAD",
    "DIFFSYNTH_MODEL_BASE_PATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTORCH_ENABLE_MPS_FALLBACK",
    "PYTHONUNBUFFERED",
    "ATELIER_DATA_ROOT",
    "ATELIER_BACKEND_ID",
    "ATELIER_MODEL_ID",
    "ATELIER_MODEL_REVISION",
    "ATELIER_PROCESSOR_ID",
    "ATELIER_LORA_PATH",
    "ATELIER_LORA_ALPHA",
    "MODELSCOPE_DOMAIN",
)


# ---------------------------------------------------------------- 起動カウンタ


def count_launch() -> int:
    """このプロセスが何回目の起動かを返す（`FAKE_WORKER_STATE` 未設定なら常に 1）。

    RealEngine.restart() はワーカープロセスを作り直すため、シナリオを
    「1回目は失敗、2回目から正常」のように変えるにはプロセス外に状態を置く必要がある。
    """
    if not STATE_PATH:
        return 1
    path = Path(STATE_PATH)
    try:
        previous = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        previous = 0
    current = previous + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(current), encoding="utf-8")
    except OSError:
        pass
    return current


#: 何回目の起動か（1 始まり）。モジュール読込時に確定する
LAUNCH = count_launch()

#: この起動が「異常な回」か（`*_then_ok` 系シナリオ用）
BAD_LAUNCH = LAUNCH <= BAD_RUNS


# ---------------------------------------------------------------- 出力


def emit(event: dict) -> None:
    sys.stdout.write("@@EVT " + json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def raw_stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def raw_stderr(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def noise(tag: str) -> None:
    """パイプを詰まらせにいく（RealEngine が常時 drain していないと止まる）。"""
    if SCENARIO == "stdout_noise":
        for i in range(FLOOD_LINES):
            raw_stdout(f"[{tag}] loading shard {i}/{FLOOD_LINES} ... " + "x" * 60)
    if SCENARIO == "stderr_flood":
        for i in range(FLOOD_LINES):
            raw_stderr(f"[{tag}] WARNING: noisy library message {i} " + "y" * 60)


def dump_env() -> None:
    if not ENV_DUMP:
        return
    payload = {
        "cwd": os.getcwd(),
        "argv": list(sys.argv),
        "env": {key: os.environ.get(key) for key in ENV_KEYS},
    }
    try:
        Path(ENV_DUMP).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------- handshake


def send_ready() -> None:
    backend_id = BACKEND_ID
    model_revision = MODEL_REVISION
    capabilities = dict(CAPABILITIES)
    if SCENARIO == "bad_backend_id":
        backend_id = "some_other_backend"
    if SCENARIO == "bad_model":
        model_revision = "unknown-revision-9999"
    if SCENARIO == "bad_capabilities":
        capabilities = dict(CAPABILITIES)
        capabilities["audio"] = False
        capabilities["num_frames"] = [56, 124, 243]
        capabilities["fps"] = 30
    emit(
        {
            "type": "ready",
            "backend_id": backend_id,
            "model_id": MODEL_ID,
            "model_revision": model_revision,
            "capabilities": capabilities,
        }
    )


def handshake() -> None:
    noise("init")
    if SCENARIO == "bad_json":
        # 不正 JSON・JSON オブジェクトでない値・未知イベント種別を混ぜる
        raw_stdout("@@EVT {this is not json at all")
        raw_stdout("@@EVT [1, 2, 3]")
        raw_stdout('@@EVT {"type": "totally_unknown", "x": 1}')
    emit({"type": "stage", "stage": "loading_model"})
    if SCENARIO == "exit_before_ready":
        raw_stderr("fake worker: dying before ready")
        sys.exit(3)
    if SCENARIO == "dead_then_ok" and BAD_LAUNCH:
        raw_stderr(f"fake worker: launch {LAUNCH} dies before ready")
        sys.exit(3)
    emit({"type": "stage", "stage": "loading_lora"})
    if SCENARIO == "stall_before_ready":
        return  # ready を出さない
    send_ready()
    if SCENARIO == "exit_after_ready":
        raw_stderr("fake worker: dying while idle")
        sys.exit(0)


# ---------------------------------------------------------------- 成果物


def _asset(num_frames: int, suffix: str) -> Path:
    name = f"mock_{num_frames}.mp4" if suffix == "mp4" else f"mock_{num_frames}_last.png"
    return ASSETS_DIR / name


def write_artifacts(num_frames: int, video_partial: str, png_partial: str) -> tuple:
    """partial パスへ書き、done で報告するパスを返す（正式名へは絶対に書かない）。"""
    video_path = Path(video_partial)
    png_path = Path(png_partial)

    if SCENARIO == "outside_data_root":
        outside = Path(OUTSIDE_DIR or Path(video_partial).parent)
        outside.mkdir(parents=True, exist_ok=True)
        video_path = outside / video_path.name
        png_path = outside / png_path.name

    video_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    if SCENARIO != "missing_mp4":
        if SCENARIO == "invalid_mp4":
            video_path.write_bytes(b"this is definitely not an mp4 file\n" * 8)
        else:
            shutil.copyfile(_asset(num_frames, "mp4"), video_path)
    if SCENARIO != "missing_png":
        if SCENARIO == "invalid_png":
            png_path.write_bytes(b"this is definitely not a png file\n" * 8)
        else:
            shutil.copyfile(_asset(num_frames, "png"), png_path)

    return str(video_path), str(png_path)


# ---------------------------------------------------------------- keyframe（継続生成）

#: PNG のシグネチャ（PIL を使わずに「画像として開ける」相当の判定をする）
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DATA_ROOT = os.environ.get("ATELIER_DATA_ROOT", "")


def dump_keyframe(job_id, keyframe_path) -> None:
    """受け取った keyframe_path を書き出す（RealEngine が本当に載せたかの検証用）。"""
    if not KEYFRAME_DUMP:
        return
    try:
        Path(KEYFRAME_DUMP).write_text(
            json.dumps(
                {"job_id": job_id, "keyframe_path": keyframe_path}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def keyframe_problem(keyframe_path) -> str:
    """本物のワーカーと同じ観点でキーフレームを検証し、問題があれば日本語で返す。

    PIL に依存しないよう、画像として開けるかは PNG シグネチャで代用する
    （偽ワーカーは標準ライブラリだけで動く）。
    """
    if not isinstance(keyframe_path, str) or not keyframe_path.strip():
        return "keyframe_path が空です（継続生成にはキーフレーム画像が必要です）"
    text = keyframe_path.strip()
    path = Path(text)
    if not path.is_absolute():
        return f"keyframe_path は絶対パスで指定してください（受信値: {text}）"
    if DATA_ROOT:
        try:
            path.resolve().relative_to(Path(DATA_ROOT).resolve())
        except ValueError:
            return f"keyframe_path がデータフォルダの外を指しています（受信値: {text}）"
    if not path.is_file():
        return f"keyframe_path のキーフレーム画像が見つかりません（受信値: {text}）"
    try:
        with path.open("rb") as handle:
            head = handle.read(len(PNG_MAGIC))
    except OSError as exc:
        return f"キーフレーム画像を開けませんでした（{path.name}）: {exc}"
    if head != PNG_MAGIC:
        return f"キーフレーム画像を開けませんでした（{path.name}）: PNG ではありません"
    return ""


# ---------------------------------------------------------------- generate


def do_generate(command: dict) -> bool:
    """generate を処理する。戻り値 False でワーカーを終了する（fatal 後）。"""
    job_id = command.get("job_id")
    params = command.get("params") or {}
    num_frames = int(params.get("num_frames") or 56)
    steps = int(params.get("num_inference_steps") or 4)
    seed = params.get("seed")
    video_partial = params.get("output_partial_path") or ""
    png_partial = params.get("last_frame_partial_path") or ""

    # 契約 §5: backend_id 不一致は fatal=false の input エラー
    if command.get("backend_id") != BACKEND_ID:
        emit(
            {
                "type": "error",
                "job_id": job_id,
                "fatal": False,
                "category": "input",
                "message": "生成バックエンドの指定が一致しません",
                "detail": f"received={command.get('backend_id')} expected={BACKEND_ID}",
            }
        )
        return True

    # 継続生成（P4）: 受け取った keyframe_path を記録し、本物のワーカーと同じ観点で検証する
    keyframe_path = params.get("keyframe_path", None)
    dump_keyframe(job_id, keyframe_path)
    problem = ""
    if keyframe_path is None:
        if SCENARIO == "keyframe_required":
            problem = "継続生成のキーフレームが届いていません（keyframe_path が null です）"
    else:
        problem = keyframe_problem(keyframe_path)
    if problem:
        emit(
            {
                "type": "error",
                "job_id": job_id,
                "fatal": False,
                "category": "input",
                "message": problem,
                "detail": f"keyframe_path={keyframe_path!r}",
            }
        )
        return True

    reported_job_id = f"{job_id}_MISMATCH" if SCENARIO == "job_id_mismatch" else job_id

    emit({"type": "stage", "job_id": reported_job_id, "stage": "preparing"})
    noise("generate")

    for step in range(1, steps + 1):
        emit(
            {
                "type": "progress",
                "job_id": reported_job_id,
                "step": step,
                "total": steps,
            }
        )
        if SCENARIO == "crash_running" and step >= 2:
            raw_stderr("fake worker: crashing in the middle of generation")
            os._exit(9)  # イベントを出さずに死ぬ（worker_dead 合成の試験）
        if SCENARIO == "hang_in_generate":
            # 以後いっさい応答しない（生成中の restart を確定的に試験するため）。
            # stdin も読まないので shutdown コマンドは届かず、terminate へ落ちる。
            raw_stderr("fake worker: hanging in the middle of generation")
            while True:
                time.sleep(0.05)

    # 何度再起動しても壊れ続けるワーカー（連続失敗 → バックオフ → HALTED の土台）。
    # プロセスは生存させ、「生きているが再起動しないと使えない」状態を作る（§13.3）。
    if SCENARIO == "always_fatal" or (SCENARIO == "fatal_then_ok" and BAD_LAUNCH):
        emit(
            {
                "type": "error",
                "job_id": reported_job_id,
                "fatal": True,
                "category": "mps",
                "message": f"MPS backend out of memory（起動 {LAUNCH} 回目）",
                "detail": "RuntimeError: MPS backend out of memory",
            }
        )
        return True

    if SCENARIO == "error_nonfatal":
        emit(
            {
                "type": "error",
                "job_id": reported_job_id,
                "fatal": False,
                "category": "input",
                "message": "キーフレーム画像を開けませんでした",
                "detail": "PIL.UnidentifiedImageError: ...",
            }
        )
        return True
    if SCENARIO == "error_fatal":
        emit(
            {
                "type": "error",
                "job_id": reported_job_id,
                "fatal": True,
                "category": "mps",
                "message": "MPS backend out of memory",
                "detail": "RuntimeError: MPS backend out of memory",
            }
        )
        return False  # fatal 後は追加の generate を受け付けずプロセスを終了する

    emit({"type": "stage", "job_id": reported_job_id, "stage": "saving"})
    video_path, png_path = write_artifacts(num_frames, video_partial, png_partial)

    done = {
        "type": "done",
        "job_id": reported_job_id,
        "elapsed_sec": 12.5,
        "output_partial_path": video_path,
        "last_frame_partial_path": png_path,
        "seed_used": seed if isinstance(seed, int) else 1234,
        "backend_id": BACKEND_ID,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "num_frames": num_frames,
        "warnings": [],
    }
    if SCENARIO == "missing_paths":
        done.pop("output_partial_path")
        done.pop("last_frame_partial_path")
    emit(done)
    if SCENARIO == "double_done":
        emit(done)
    return True


# ---------------------------------------------------------------- main


def handle_command(command: dict) -> bool:
    kind = command.get("cmd")
    if kind == "ping":
        emit({"type": "pong"})
        return True
    if kind == "shutdown":
        if SCENARIO in ("ignore_shutdown", "ignore_sigterm"):
            raw_stderr("fake worker: ignoring shutdown command")
            return True
        return False
    if kind == "generate":
        return do_generate(command)
    raw_stderr(f"fake worker: unknown command {kind!r}")
    return True


def main() -> int:
    if SCENARIO == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    dump_env()
    handshake()

    while True:
        line = sys.stdin.readline()
        if not line:  # stdin が閉じられた
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except Exception:
            raw_stderr(f"fake worker: broken command line: {line[:120]}")
            continue
        if not isinstance(command, dict):
            continue
        if not handle_command(command):
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
