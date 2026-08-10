"""設定管理: config/config.toml の読込と検証（読み取り専用）。設計書 §12・§22。

検証エラーは日本語メッセージの ConfigError で報告する。
実機検証済みの固定値（576×320・24fps・56/124f・4/8step）はここで強制し、
設定ファイルの書き換えでも迂回できないようにする。

用語（設計書 §22.1）:
- Execution Engine（engine.mode）: 実行方式。"real" | "mock"
- Generation Backend（engine.backend）: 生成モデル実装。V1 は "minimax_h3" のみ
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

FIXED_WIDTH = 576
FIXED_HEIGHT = 320
FIXED_FPS = 24
ALLOWED_NUM_FRAMES = (56, 124)
ALLOWED_STEPS = (4, 8)

# V1 で実装済みの Generation Backend（未登録IDは preflight が拒否する。§13.1）
SUPPORTED_BACKENDS = ("minimax_h3",)

ENGINE_MODES = ("real", "mock")

# LAN モードの PIN 桁数の許容範囲（P5契約 §5.4）
LAN_PIN_DIGITS_MIN = 4
LAN_PIN_DIGITS_MAX = 12


class ConfigError(Exception):
    """設定ファイルの不備（日本語メッセージ）。"""


@dataclass(frozen=True)
class BackendConfig:
    """Generation Backend の identity と実行環境（設計書 §22.2）。"""

    backend_id: str
    display_name: str
    worker_python: Path
    working_directory: Path  # 読み取り専用。書き込みは data_root 配下のみ
    worker_script: str       # プロジェクト相対パス（P2 で実装）
    model_id: str
    model_revision: str
    processor_id: str
    lora_relpath: str
    lora_alpha: float


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    # [app]
    name: str
    version: str
    # [engine]
    engine_mode: str   # "real" | "mock"（Execution Engine）
    backend_id: str    # Generation Backend の識別子
    backend: BackendConfig
    # [server]
    host: str
    port: int
    auto_open_browser: bool
    # [paths]
    data_root: Path
    ffmpeg_path: str
    # [generation]
    allowed_num_frames: tuple[int, ...]
    allowed_steps: tuple[int, ...]
    audio_sample_rate: int
    default_num_frames: int
    default_steps: int
    seed_max: int
    # [estimates]
    estimates: dict
    stall_warn_factor: float
    stall_abort_factor: float
    # [queue]
    max_queued_jobs: int
    allow_cancel_queued: bool
    auto_restart_worker: bool
    max_auto_restarts: int
    restart_backoff_sec: tuple[int, ...]
    # [concat]
    # V1 の連結方式は「PTS正規化つき再エンコード」の1つだけ（P5 で reencode を排除）。
    # `-c copy` は config から選べない（設計書 §10.6・ffmpeg_ops.concat_copy の docstring）。
    dedupe_boundary_frame: bool
    dedupe_max_mean_diff: float
    dedupe_max_max_diff: float
    # [lan] — LANモードの調整値のみ。**有効化はしない**（LANモードは --lan だけで決まる）
    lan_host_override: str
    lan_pin_digits: int
    lan_max_auth_failures: int
    lan_auth_lockout_sec: float
    # [logging]
    log_level: str
    log_max_bytes: int
    log_backup_count: int
    # [safety]
    warn_free_disk_gb: float
    stop_free_disk_gb: float
    # [mock]
    mock_speed_factor: float

    @property
    def outputs_dir(self) -> Path:
        return self.data_root / "outputs"

    @property
    def concat_dir(self) -> Path:
        return self.data_root / "concat"

    @property
    def tmp_dir(self) -> Path:
        return self.data_root / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def history_path(self) -> Path:
        return self.data_root / "history.json"

    @property
    def concat_manifest_path(self) -> Path:
        """任意順序連結の成果物台帳（P5.2）。history.json とは別ファイル。"""
        return self.data_root / "concat_manifest.json"

    @property
    def upscaled_dir(self) -> Path:
        """1080p高品質版の保存先（P6）。**HTTP 配信対象に入れる唯一の追加先**。"""
        return self.data_root / "upscaled"

    @property
    def upscale_weights_path(self) -> Path:
        """realesr-animevideov3 の重み（P6）。リポジトリには含めない。"""
        return self.project_root / "app" / "assets" / "upscale" / "realesr-animevideov3.pth"

    @property
    def upscale_worker_script(self) -> Path:
        return self.project_root / "app" / "postprocess" / "upscale_worker.py"

    @property
    def trash_dir(self) -> Path:
        """アプリ内ゴミ箱（P5.3-B）。**HTTP 配信対象には入れない**。

        実際に移動するときだけ作る（起動や一覧表示では作らない）。
        """
        return self.data_root / "trash"

    @property
    def start_images_dir(self) -> Path:
        """開始画像のジョブ用スナップショット（P8）。**HTTP 配信対象には入れない**。"""
        return self.data_root / "start_images"

    @property
    def start_images_staging_dir(self) -> Path:
        """開始画像の一時領域（P8）。プレビュー段階。起動時に掃除する。"""
        return self.data_root / "start_images" / "staging"

    @property
    def assets_mock_dir(self) -> Path:
        return self.project_root / "app" / "assets" / "mock"


def _section(doc: dict, name: str) -> dict:
    sec = doc.get(name)
    if not isinstance(sec, dict):
        raise ConfigError(f"config.toml に [{name}] セクションがありません")
    return sec


def _get(sec: dict, sec_name: str, key: str, types) -> object:
    if key not in sec:
        raise ConfigError(f"config.toml の [{sec_name}] に {key} がありません")
    value = sec[key]
    expected = types if isinstance(types, tuple) else (types,)
    # bool は int の派生なので、数値項目に true を書いた場合は明示的に弾く
    wrong_type = not isinstance(value, expected)
    bool_where_number = isinstance(value, bool) and bool not in expected
    if wrong_type or bool_where_number:
        raise ConfigError(f"config.toml の [{sec_name}] {key} の型が不正です")
    return value


def _optional(sec: dict, sec_name: str, key: str, types, default):
    """省略可能なキーを型チェックつきで読む（未記載なら default）。"""
    if key not in sec:
        return default
    return _get(sec, sec_name, key, types)


@dataclass(frozen=True)
class LanSettings:
    """[lan] セクションの調整値（LANモードの**有効化はしない**）。"""

    host_override: str
    pin_digits: int
    max_auth_failures: int
    auth_lockout_sec: float


#: [lan] に書かれていたら拒否するキー。設定ファイルだけで LAN 公開が始まる状態を作らない。
LAN_FORBIDDEN_KEYS = ("enabled", "enable", "auto_enable", "auto_start", "share", "pin")


def _parse_lan(doc: dict) -> LanSettings:
    """[lan] セクション（省略可）を読む（P5契約 §5.4）。

    **有効化キーは置かない**。iPhone接続モード（LANモード）は起動時の `--lan`
    オプションだけで決まり、設定ファイルだけでは絶対に有効にならない。
    セクションが無い場合は既定値を使う（通常モードの起動を [lan] の有無に
    依存させないため）。値が書かれている場合だけ型と範囲を検証する。
    """
    lan = doc.get("lan", {})
    if not isinstance(lan, dict):
        raise ConfigError("config.toml の [lan] はセクション（テーブル）で書いてください")

    for forbidden in LAN_FORBIDDEN_KEYS:
        if forbidden in lan:
            raise ConfigError(
                f"config.toml の [lan] {forbidden} は使用できません。"
                "iPhone接続モードは起動時の --lan オプションでのみ有効になります"
                "（設定ファイルだけでは有効になりません）。この行を削除してください"
            )

    host_override = str(_optional(lan, "lan", "host_override", str, "")).strip()
    if host_override:
        # 検証は app/core/network.py に一本化する（RFC1918 のみ許可）。
        # 遅延 import は循環参照を避けるため（network は lanauth を読む）。
        from app.core.network import NetworkError, validate_lan_host

        try:
            host_override = validate_lan_host(host_override)
        except NetworkError as e:
            raise ConfigError(
                f"config.toml の [lan] host_override が不正です: {e}"
            ) from e

    pin_digits = int(_optional(lan, "lan", "pin_digits", int, 6))
    if not (LAN_PIN_DIGITS_MIN <= pin_digits <= LAN_PIN_DIGITS_MAX):
        raise ConfigError(
            f"config.toml の [lan] pin_digits は {LAN_PIN_DIGITS_MIN}〜"
            f"{LAN_PIN_DIGITS_MAX} の範囲で指定してください（指定: {pin_digits}）"
        )

    max_failures = int(_optional(lan, "lan", "max_auth_failures", int, 10))
    if max_failures < 1:
        raise ConfigError(
            "config.toml の [lan] max_auth_failures は 1 以上を指定してください"
            f"（指定: {max_failures}）"
        )

    lockout = float(_optional(lan, "lan", "auth_lockout_sec", (int, float), 30))
    # 0 を許すとレート制限が丸ごと無効になり、PIN の総当たりを止められなくなる。
    # 「無効化できる設定」を残さないため 1 秒以上を必須にする。
    if lockout < 1:
        raise ConfigError(
            "config.toml の [lan] auth_lockout_sec は 1 以上を指定してください"
            "（0 にすると PIN の連続入力を止められなくなるためです）"
            f"（指定: {lockout}）"
        )

    return LanSettings(
        host_override=host_override,
        pin_digits=pin_digits,
        max_auth_failures=max_failures,
        auth_lockout_sec=lockout,
    )


def _parse_backend(doc: dict, backend_id: str) -> BackendConfig:
    backends_tbl = _section(doc, "backends")
    if backend_id not in backends_tbl or not isinstance(backends_tbl[backend_id], dict):
        raise ConfigError(
            f"config.toml に [backends.{backend_id}] セクションがありません"
            f"（engine.backend = \"{backend_id}\" が指しています）"
        )
    b = backends_tbl[backend_id]
    sec = f"backends.{backend_id}"
    return BackendConfig(
        backend_id=backend_id,
        display_name=str(_get(b, sec, "display_name", str)),
        worker_python=Path(str(_get(b, sec, "worker_python", str))),
        working_directory=Path(str(_get(b, sec, "working_directory", str))),
        worker_script=str(_get(b, sec, "worker_script", str)),
        model_id=str(_get(b, sec, "model_id", str)),
        model_revision=str(_get(b, sec, "model_revision", str)),
        processor_id=str(_get(b, sec, "processor_id", str)),
        lora_relpath=str(_get(b, sec, "lora_relpath", str)),
        lora_alpha=float(_get(b, sec, "lora_alpha", (int, float))),
    )


def load_config(project_root: Path, config_path: Path | None = None) -> AppConfig:
    path = config_path or (project_root / "config" / "config.toml")
    if not path.is_file():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"設定ファイルを読み取れません（TOML構文エラー）: {e}") from e

    app = _section(doc, "app")
    engine = _section(doc, "engine")
    server = _section(doc, "server")
    paths = _section(doc, "paths")
    gen = _section(doc, "generation")
    estimates = _section(doc, "estimates")
    queue = _section(doc, "queue")
    concat = _section(doc, "concat")
    log_sec = _section(doc, "logging")
    safety = _section(doc, "safety")
    mock = _section(doc, "mock")

    engine_mode = str(_get(engine, "engine", "mode", str))
    if engine_mode not in ENGINE_MODES:
        raise ConfigError('engine.mode は "real" か "mock" を指定してください')
    env_mock = os.environ.get("ATELIER_MOCK", "").strip().lower()
    if env_mock in ("1", "true"):
        engine_mode = "mock"
    elif env_mock in ("0", "false"):
        engine_mode = "real"

    backend_id = str(_get(engine, "engine", "backend", str))
    backend = _parse_backend(doc, backend_id)

    host = str(_get(server, "server", "host", str))
    if host != "127.0.0.1":
        raise ConfigError(
            "server.host は 127.0.0.1 固定です（外部公開しない設計。設計書 §15）"
        )
    port = int(_get(server, "server", "port", int))
    if not (1024 <= port <= 65535):
        raise ConfigError("server.port は 1024〜65535 の範囲で指定してください")

    for key, fixed in (("width", FIXED_WIDTH), ("height", FIXED_HEIGHT), ("fps", FIXED_FPS)):
        if int(_get(gen, "generation", key, int)) != fixed:
            raise ConfigError(
                f"generation.{key} は実機検証済みの {fixed} 固定です（設計書 §0.4・§2）"
            )

    allowed_frames = tuple(int(v) for v in _get(gen, "generation", "allowed_num_frames", list))
    if sorted(allowed_frames) != sorted(ALLOWED_NUM_FRAMES):
        raise ConfigError(
            "generation.allowed_num_frames は実機検証済みの [56, 124] 固定です（設計書 §0.4）"
        )
    allowed_steps = tuple(int(v) for v in _get(gen, "generation", "allowed_steps", list))
    if sorted(allowed_steps) != sorted(ALLOWED_STEPS):
        raise ConfigError("generation.allowed_steps は実機検証済みの [4, 8] 固定です")

    default_frames = int(_get(gen, "generation", "default_num_frames", int))
    if default_frames not in allowed_frames:
        raise ConfigError("generation.default_num_frames は 56 か 124 を指定してください")
    default_steps = int(_get(gen, "generation", "default_steps", int))
    if default_steps not in allowed_steps:
        raise ConfigError("generation.default_steps は 4 か 8 を指定してください")

    warn_gb = float(_get(safety, "safety", "warn_free_disk_gb", (int, float)))
    stop_gb = float(_get(safety, "safety", "stop_free_disk_gb", (int, float)))
    if not (warn_gb >= stop_gb >= 0):
        raise ConfigError(
            "safety の閾値が不正です（warn_free_disk_gb >= stop_free_disk_gb >= 0）"
        )

    backoff = tuple(int(v) for v in _get(queue, "queue", "restart_backoff_sec", list))
    if not backoff or any(v < 0 for v in backoff):
        raise ConfigError("queue.restart_backoff_sec は正の秒数のリストで指定してください")

    data_root = Path(str(_get(paths, "paths", "data_root", str)))
    if not data_root.is_absolute():
        data_root = project_root / data_root
    # 絶対指定でも必ず解決する（履歴の相対化とパス境界検証を data_root 基準で一貫させる）
    data_root = data_root.resolve()

    speed_factor = float(_get(mock, "mock", "speed_factor", (int, float)))
    if speed_factor <= 0:
        raise ConfigError("mock.speed_factor は 0 より大きい値を指定してください")

    # V1 の連結方式は「PTS正規化つき再エンコード」のみ（P5）。
    # 未配線の `-c copy` が設定から選べるように見える状態を無くす。
    if "reencode" in concat:
        raise ConfigError(
            "config.toml の [concat] reencode は V1 では未対応です。この行を削除してください"
            "（V1 の連結は常に「PTS正規化つき再エンコード」で行います。"
            "-c copy による無再エンコード連結は実機で Non-monotonic DTS 警告が出るため"
            "選択できません）"
        )

    lan = _parse_lan(doc)

    return AppConfig(
        project_root=project_root,
        name=str(_get(app, "app", "name", str)),
        version=str(_get(app, "app", "version", str)),
        engine_mode=engine_mode,
        backend_id=backend_id,
        backend=backend,
        host=host,
        port=port,
        auto_open_browser=bool(_get(server, "server", "auto_open_browser", bool)),
        data_root=data_root,
        ffmpeg_path=str(_get(paths, "paths", "ffmpeg_path", str)),
        allowed_num_frames=allowed_frames,
        allowed_steps=allowed_steps,
        audio_sample_rate=int(_get(gen, "generation", "audio_sample_rate", int)),
        default_num_frames=default_frames,
        default_steps=default_steps,
        seed_max=int(_get(gen, "generation", "seed_max", int)),
        estimates=dict(estimates),
        stall_warn_factor=float(estimates.get("stall_warn_factor", 3.0)),
        stall_abort_factor=float(estimates.get("stall_abort_factor", 0.0)),
        max_queued_jobs=int(_get(queue, "queue", "max_queued_jobs", int)),
        allow_cancel_queued=bool(_get(queue, "queue", "allow_cancel_queued", bool)),
        auto_restart_worker=bool(_get(queue, "queue", "auto_restart_worker", bool)),
        max_auto_restarts=int(_get(queue, "queue", "max_auto_restarts", int)),
        restart_backoff_sec=backoff,
        dedupe_boundary_frame=bool(concat.get("dedupe_boundary_frame", False)),
        dedupe_max_mean_diff=float(concat.get("dedupe_max_mean_diff", 1.0)),
        dedupe_max_max_diff=float(concat.get("dedupe_max_max_diff", 16.0)),
        lan_host_override=lan.host_override,
        lan_pin_digits=lan.pin_digits,
        lan_max_auth_failures=lan.max_auth_failures,
        lan_auth_lockout_sec=lan.auth_lockout_sec,
        log_level=str(_get(log_sec, "logging", "level", str)),
        log_max_bytes=int(_get(log_sec, "logging", "max_bytes", int)),
        log_backup_count=int(_get(log_sec, "logging", "backup_count", int)),
        warn_free_disk_gb=warn_gb,
        stop_free_disk_gb=stop_gb,
        mock_speed_factor=speed_factor,
    )
