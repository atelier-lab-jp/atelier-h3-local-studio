"""起動前チェック（設計書 §13.1）。

すべての結果を日本語メッセージで返す。エラーが1件でもあれば起動を中止する。
mock モードでは DiffSynth-Studio・モデルが無くても起動できるよう、
実機系のチェックをスキップする。

P5 で iPhone接続モード（LANモード）のチェックを追加した。`run_preflight(..., lan=True)`
のときだけ実行し、既定（通常モード）の挙動は一切変えていない。
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import SUPPORTED_BACKENDS, AppConfig
from app.core import ffmpeg_ops
from app.core.fileops import disk_free_gb, disk_state, list_orphan_partials

# minimax_h3 バックエンド固有の資産検査（設計書 §0.1 で確定したローカルモデル配置。
# P2 でバックエンド定義側へ移設する候補）
REQUIRED_NF4_FILES = (
    "minimax-h3-fl2va-nf4.safetensors",
    "minimax-h3-text-encoder-nf4.safetensors",
    "video_vae_nf4.safetensors",
    "audio_vae_nf4.safetensors",
)
NF4_SUBDIR = Path("models/DiffSynth-Studio/MiniMax-H3-NF4")
PROCESSOR_SUBDIR = Path("models/MiniMax/MiniMax-H3/FL2VA")

WORKER_IMPORT_CHECK = "import torch, diffsynth, av, PIL"


@dataclass
class PreflightResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_port_free(host: str, port: int) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return None
    except OSError:
        return (
            f"ポート {port} は使用中です。config.toml の [server] port を"
            "変更するか、使用中のアプリを終了してください"
        )


def check_lan_port(host: str, port: int) -> str | None:
    """LANモードで待ち受けるアドレス・ポートに bind できるか確認する。

    「そのアドレスがこの Mac に無い」場合と「ポートが埋まっている」場合は
    利用者の対処がまったく違うので、日本語メッセージを分ける。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return None
    except OSError as e:
        if e.errno == errno.EADDRNOTAVAIL:
            return (
                f"このMacに {host} というアドレスは割り当てられていません。\n"
                "    Wi-Fiに接続されているか確認し、--lan-host の指定を見直してください\n"
                "    （--lan-host を付けなければ自動で検出します）"
            )
        return (
            f"iPhone接続モードで使うポートが空いていません（{host}:{port}）。\n"
            "    別のアプリが同じポートを使っているか、通常モードのアプリが起動したままです。\n"
            "    先に起動しているアプリを終了するか、config.toml の [server] port を"
            "変更してください"
        )


def check_lan(cfg: AppConfig, host_override: str = "") -> PreflightResult:
    """LANモードでだけ行う追加チェック（P5契約 §5.5）。

    - 同じWi-Fi内で使えるプライベート IPv4（RFC1918）が見つかるか
    - そのアドレスで `server.port` を bind できるか

    見つからない場合は日本語で「Wi-Fiに接続されているか」「ゲストWi-Fiではないか」を案内する
    （文面は `app/core/network.py` の NO_LAN_MESSAGE に一本化してある）。
    **通常モードでは一切呼ばない**（LANモードは --lan でのみ有効になる）。
    """
    result = PreflightResult()
    # network は lanauth を読むため、通常モードの起動を重くしないよう遅延 import する
    from app.core import network

    preferred = (host_override or cfg.lan_host_override or "").strip()
    interfaces: list = []
    try:
        if preferred:
            host = network.validate_lan_host(preferred)
        else:
            interfaces = network.list_lan_interfaces()
            host = network.detect_lan_ipv4(interfaces=interfaces)
    except network.NetworkError as e:
        result.errors.append(f"iPhone接続モードを開始できません: {e}")
        return result

    result.infos.append(
        f"iPhone接続モード: この Mac のアドレス {host} で待ち受けます"
        f"（接続先 http://{host}:{cfg.port}）"
    )
    if preferred:
        result.infos.append(f"アドレスは手動指定です（自動検出をしていません）: {host}")
    elif len(interfaces) > 1:
        result.infos.append(network.describe_interfaces(interfaces))

    bind_error = check_lan_port(host, cfg.port)
    if bind_error:
        result.errors.append(bind_error)
    return result


def check_worker_packages(cfg: AppConfig, timeout: int = 240) -> str | None:
    """DiffSynth venv で必要パッケージが import できるか検査する（§13.1 v1.1追加）。

    PYTHONDONTWRITEBYTECODE=1 を設定し、DiffSynth-Studio 側へ
    __pycache__ を新規作成しない。
    """
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [str(cfg.backend.worker_python), "-c", WORKER_IMPORT_CHECK],
            cwd=str(cfg.backend.working_directory),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "既存Python環境のパッケージ検査がタイムアウトしました"
    except OSError as e:
        return f"既存Python環境を起動できません: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        return (
            "既存Python環境に必要なパッケージ（torch / diffsynth / av / PIL）を"
            "読み込めません:\n    " + "\n    ".join(tail)
        )
    return None


def run_preflight(
    cfg: AppConfig,
    mode: str,
    deep_worker_check: bool = False,
    free_gb_override: float | None = None,
    *,
    lan: bool = False,
    lan_host: str = "",
) -> PreflightResult:
    """起動前チェック。`mode` は "real" | "mock"（Execution Engine）。

    `lan=True` のときだけ、iPhone接続モード用の追加チェック（プライベート IPv4 の
    検出とポートの空き）を行う。既定は False なので、既存の呼び出し
    `run_preflight(cfg, mode)` の挙動は一切変わらない（P5契約 §5.5）。
    """
    result = PreflightResult()

    # データ領域の作成・書込可否
    try:
        for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
        probe = cfg.tmp_dir / ".write_check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        result.errors.append(
            f"データ領域（{cfg.data_root}）を作成・書き込みできません: {e}"
        )
        return result

    # 生成バックエンド識別（§22.1: 未登録の backend_id は拒否する）
    backend_registered = cfg.backend_id in SUPPORTED_BACKENDS
    if not backend_registered:
        result.errors.append(
            f"未登録の生成バックエンドです: {cfg.backend_id}"
            f"（V1 で利用可能: {', '.join(SUPPORTED_BACKENDS)}）。"
            "config.toml の [engine] backend を確認してください"
        )
    else:
        result.infos.append(
            f"生成バックエンド: {cfg.backend.display_name}（{cfg.backend_id} / "
            f"{cfg.backend.model_id} @ {cfg.backend.model_revision}）"
        )

    # ffmpeg 実体
    try:
        ffmpeg = ffmpeg_ops.resolve_ffmpeg(cfg.ffmpeg_path)
        version = ffmpeg_ops.ffmpeg_version(ffmpeg)
        result.infos.append(f"ffmpeg: {version}（{ffmpeg}）")
    except ffmpeg_ops.FfmpegError as e:
        result.errors.append(f"ffmpeg を利用できません: {e}")

    # ポート
    port_error = check_port_free(cfg.host, cfg.port)
    if port_error:
        result.errors.append(port_error)

    # iPhone接続モード（LANモード）。--lan のときだけ実行する
    if lan:
        lan_result = check_lan(cfg, lan_host)
        result.errors.extend(lan_result.errors)
        result.warnings.extend(lan_result.warnings)
        result.infos.extend(lan_result.infos)

    # 空きディスク（警告20GB / 受付停止5GB。§21.1-3 確定）
    free_gb = free_gb_override if free_gb_override is not None else disk_free_gb(cfg.data_root)
    state = disk_state(free_gb, cfg.warn_free_disk_gb, cfg.stop_free_disk_gb)
    if state == "stop":
        result.warnings.append(
            f"空き容量が {free_gb:.1f}GB しかありません（{cfg.stop_free_disk_gb:.0f}GB未満）。"
            "新しい生成・連結の受付を停止します。Finderで不要な動画を整理してください"
        )
    elif state == "warn":
        result.warnings.append(
            f"空き容量が {free_gb:.1f}GB です（{cfg.warn_free_disk_gb:.0f}GB未満）。"
            "残り容量にご注意ください"
        )
    else:
        result.infos.append(f"空き容量: {free_gb:.1f}GB（問題なし）")

    # 孤児 partial の列挙（削除はしない。§10.7 手順6）
    orphans = list_orphan_partials(cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir)
    if orphans:
        names = ", ".join(p.name for p in orphans[:10])
        result.infos.append(
            f"未完了の作業ファイル（partial）が {len(orphans)} 件残っています: {names}"
        )

    if mode == "mock":
        missing_assets = [
            name
            for name in ("mock_56.mp4", "mock_124.mp4", "mock_56_last.png", "mock_124_last.png")
            if not (cfg.assets_mock_dir / name).is_file()
        ]
        if missing_assets:
            result.errors.append(
                "モック素材が見つかりません（"
                + ", ".join(missing_assets)
                + "）。先に ./scripts/setup.sh を実行してください"
            )
        return result

    # ---- 実機モードのチェック（完全オフラインの成立条件。§12.3 v1.1） ----
    if not backend_registered:
        # 未登録バックエンドの資産検査は無意味なのでここで打ち切る
        return result

    root = cfg.backend.working_directory
    if not root.is_dir():
        result.errors.append(
            f"DiffSynth-Studio が見つかりません: {root}\n"
            "    config.toml の [backends.minimax_h3] working_directory を確認してください"
        )
        return result

    if not cfg.backend.worker_python.is_file():
        result.errors.append(
            f"既存Python環境が見つかりません: {cfg.backend.worker_python}\n"
            "    config.toml の [backends.minimax_h3] worker_python を確認してください"
        )

    nf4_dir = root / NF4_SUBDIR
    missing_models = [
        name for name in REQUIRED_NF4_FILES if not (nf4_dir / name).is_file()
    ]
    if missing_models:
        result.errors.append(
            "MiniMax-H3-NF4 のモデルファイルが不足しています（"
            + ", ".join(missing_models)
            + f"）。配置場所: {nf4_dir}"
        )

    processor_dir = root / PROCESSOR_SUBDIR
    if not processor_dir.is_dir():
        result.errors.append(
            f"processor ディレクトリが見つかりません: {processor_dir}"
        )

    lora_path = root / cfg.backend.lora_relpath
    if not lora_path.is_file():
        result.errors.append(f"Turbo LoRA が見つかりません: {lora_path}")

    # ワーカースクリプトはアプリ側（プロジェクト相対）に置く（設計書 §22.6）
    worker_script = Path(cfg.backend.worker_script)
    if not worker_script.is_absolute():
        worker_script = cfg.project_root / worker_script
    if not worker_script.is_file():
        result.errors.append(
            f"ワーカースクリプトが見つかりません: {worker_script}\n"
            "    config.toml の [backends.minimax_h3] worker_script を確認してください"
        )

    if deep_worker_check and cfg.backend.worker_python.is_file():
        pkg_error = check_worker_packages(cfg)
        if pkg_error:
            result.errors.append(pkg_error)
        else:
            result.infos.append(
                "既存Python環境のパッケージ検査: 合格（torch / diffsynth / av / PIL）"
            )

    return result


def format_report(result: PreflightResult, mode: str) -> str:
    lines = [f"===== 起動前チェック（{mode}モード） ====="]
    for e in result.errors:
        lines.append(f"  [エラー] {e}")
    for w in result.warnings:
        lines.append(f"  [警告]   {w}")
    for i in result.infos:
        lines.append(f"  [情報]   {i}")
    lines.append(
        "  結果: " + ("合格（起動できます）" if result.ok else "不合格（起動を中止します）")
    )
    return "\n".join(lines)
