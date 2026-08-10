"""開始画像の取込・検証・正規化（P8）。UI にもエンジンにも依存しない純粋層。

アップロードされた画像を「PNG・576×320ちょうど・RGB・メタデータなし」へ正規化し、
`data/start_images/staging/` へ原子的に保存する（設計書 §10.7）。
ワーカー側の受入条件（`app/engine/backends/minimax_h3/h3_worker.py` の
`open_keyframe_image`: PNG・576×320ちょうど・PIL で読み切れる）をここで先に満たす。

守っている安全上の約束:

- 例外 `StartImageError` は**利用者向け日本語だけ**を持つ。内部パス・元の例外文・
  例外クラス名・スタックを含めない（UI にそのまま出すため）
- `Image.MAX_IMAGE_PIXELS` や `ImageFile.LOAD_TRUNCATED_IMAGES` などの PIL の
  グローバル設定を**書き換えない**（プロセス全体に効いてしまい、他スレッドと競合する）。
  巨大画像は `Image.open()` 直後の `im.size` から**明示的に**弾く
- アニメーション画像の先頭フレームを黙って使わない（必ず拒否する）
- 正規化は同時に1件だけ実行する（ピークメモリを固定するため）
- 出力 PNG に `icc_profile` / `exif` / `pnginfo` を持ち込まない
  （デコード済み画素から新しい画像を作り直してメタデータを落とす）

`gradio` は import しない。アップロード元ディレクトリは呼び出し側が `upload_root`
として渡す。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from app.core.fileops import (
    PARTIAL_SUFFIX,
    FileopsError,
    ensure_within,
    partial_path,
    promote,
    verify_png,
)

TARGET_W, TARGET_H = 576, 320
TARGET_SIZE = (TARGET_W, TARGET_H)
TARGET_AR = TARGET_W / TARGET_H          # 1.8（9:5）。16:9（1.7778）ではない
MAX_UPLOAD_BYTES = 32 * 1024 * 1024      # 32MB
MAX_PIXELS = 50_000_000
MAX_SIDE = 12_000
MIN_W, MIN_H = TARGET_W, TARGET_H        # 拡大を発生させない条件
AR_MIN, AR_MAX = 0.5, 3.0
ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
ALLOWED_MODES = frozenset({"1", "L", "LA", "P", "PA", "RGB", "RGBA", "CMYK", "YCbCr"})
TRANSPARENT_BG = (0, 0, 0)               # 透過は黒で塗りつぶす（利用者に必ず告知する）
WARN_CROP_LOSS = 0.25                    # 面積の25%超を捨てたら警告
ID_PREFIX = "si_"
ID_PATTERN = re.compile(r"^si_[0-9a-f]{12}$")
STAGING_MAX_AGE_SEC = 24 * 3600

#: EXIF の Orientation タグ番号。5〜8 は縦横が入れ替わる。
_EXIF_ORIENTATION = 0x0112
_SWAPPED_ORIENTATIONS = (5, 6, 7, 8)

#: 形式判定用に読むファイル先頭のバイト数（PIL が開けない場合の案内分けに使う）。
_HEADER_BYTES = 64

_HEIF_BRANDS = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
)
_AVIF_BRANDS = frozenset({b"avif", b"avis"})

# --- 利用者向けメッセージ（内部情報を一切含めない） ---------------------------

MSG_RECEIVE_FAILED = "画像を受け取れませんでした。もう一度選び直してください。"
MSG_NOT_FOUND = "選んだ開始画像が見つかりません。もう一度選び直してください。"
MSG_NOT_IMAGE = "画像として読み取れないファイルです。PNG・JPEG・WebP の画像を選んでください。"
MSG_BROKEN = (
    "画像ファイルが壊れているようです（途中で切れています）。"
    "もう一度書き出してから選び直してください。"
)
MSG_ANIMATED = "動く画像（アニメーション）は開始画像に使えません。1枚の静止画を選んでください。"
MSG_HIGH_BIT_DEPTH = (
    "16bit（HDR）の画像には対応していません。通常の8bitの PNG・JPEG で書き出してください。"
)
MSG_HEIC = (
    "iPhone の HEIC 形式には対応していません。［設定］→［カメラ］→［フォーマット］を"
    "「互換性優先」にするか、写真アプリから JPEG で書き出してください。"
)
MSG_AVIF = "AVIF 形式には対応していません。PNG・JPEG・WebP で書き出してから選んでください。"
MSG_GIF = "GIF 形式には対応していません。PNG・JPEG・WebP を選んでください。"
MSG_SVG = "SVG 形式には対応していません。PNG・JPEG・WebP を選んでください。"

WARN_FLATTENED = "透過部分は黒で塗りつぶしました。"


def _msg_unsupported_format(fmt: str) -> str:
    return f"対応していない画像形式です（{fmt}）。PNG・JPEG・WebP を選んでください。"


def _msg_file_too_large(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    return f"ファイルが大きすぎます（{mb:.1f}MB）。32MB までの画像を選んでください。"


#: 寸法が判らないまま巨大と判った場合（PIL が open 時点で拒否したとき）。
MSG_PIXELS_TOO_LARGE = "画像が大きすぎます。5000万画素・辺12000px までの画像を選んでください。"


def _msg_pixels_too_large(width: int, height: int) -> str:
    return (
        f"画像が大きすぎます（{width}×{height}）。"
        "5000万画素・辺12000px までの画像を選んでください。"
    )


def _msg_too_small(width: int, height: int) -> str:
    return (
        f"画像が小さすぎます（{width}×{height}）。"
        f"横{MIN_W}px・縦{MIN_H}px 以上の画像を選んでください。"
    )


def _msg_bad_ratio(width: int, height: int) -> str:
    return (
        f"画像の形が極端です（{width}×{height}）。"
        "大きく切り取られてしまうので、もう少し普通の形の画像を選んでください。"
    )


def _msg_cropped(lost_percent: int) -> str:
    return (
        f"元の画像の約{lost_percent}%を切り取りました。"
        "正方形や縦長の画像は切り取られる範囲が大きくなります。"
        "重要な人物や物が画像の中央付近にあるか、プレビューで確認してください。"
    )


class StartImageError(Exception):
    """利用者向け日本語メッセージ**だけ**を持つ。内部パス・例外文・スタックを含めない。"""


@dataclass(frozen=True)
class StartImageResult:
    """正規化の結果（UI とジョブ投入の両方が使う）。"""

    start_image_id: str            # "si_xxxxxxxxxxxx"
    staged_path: Path              # data/start_images/staging/si_xxxxxxxxxxxx.png（絶対）
    png_bytes: bytes               # ジョブへ渡すものと**同一バイト列**
    source_size: tuple[int, int]   # 元画像（EXIF回転後）の寸法
    source_format: str             # "PNG" | "JPEG" | "WEBP"
    cropped: bool                  # クロップが発生したか
    kept_area_ratio: float         # 残した面積比（0.0〜1.0）
    flattened: bool                # 透過を塗りつぶしたか
    passthrough: bool              # 576×320 で画素を変えずに通したか
    warnings: tuple[str, ...]      # 利用者向け日本語の注意（0件もありうる）


#: 正規化を同時に1件へ絞る（デコード中のピークメモリを 24GB 機で固定するため）。
_NORMALIZE_LOCK = threading.Lock()
#: 確定（commit）の直列化。同じ .partial 名へ2スレッドが書き込むのを防ぐ。
_COMMIT_LOCK = threading.Lock()


def is_valid_start_image_id(value: object) -> bool:
    """開始画像IDの形式検証（`si_` ＋ 16進12桁）。"""
    return isinstance(value, str) and ID_PATTERN.match(value) is not None


# --- 内部ヘルパ ---------------------------------------------------------------


def _silent_unlink(path: Path) -> None:
    """片づけ用。消せなくても本処理のエラーを塗りつぶさない。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_header(path: Path) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(_HEADER_BYTES)
    except OSError:
        return b""


def _sniff_container(header: bytes) -> str | None:
    """PIL が開けなかったファイルの正体を先頭バイトから推測する（案内文の出し分け用）。"""
    if not header:
        return None
    text = header.lstrip(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"svg" in header.lower()):
        return "SVG"
    if text.startswith(b"<?xml") or text.startswith(b"<!DOCTYPE svg"):
        # SVG は XML 宣言のあとに <svg> が来る。先頭64バイトに収まらない場合もあるため
        # XML そのものは SVG 扱いにする（画像として選ばれる XML は事実上 SVG だけ）。
        return "SVG"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in _AVIF_BRANDS:
            return "AVIF"
        if brand in _HEIF_BRANDS:
            return "HEIC"
    return None


def _msg_for_unopenable(header: bytes) -> str:
    kind = _sniff_container(header)
    if kind == "SVG":
        return MSG_SVG
    if kind == "AVIF":
        return MSG_AVIF
    if kind == "HEIC":
        return MSG_HEIC
    return MSG_NOT_IMAGE


def _msg_for_format(fmt: str, header: bytes) -> str:
    if fmt == "GIF":
        return MSG_GIF
    if fmt in ("AVIF", "HEIF"):
        return MSG_AVIF if fmt == "AVIF" else MSG_HEIC
    kind = _sniff_container(header)
    if kind == "HEIC":
        return MSG_HEIC
    if kind == "AVIF":
        return MSG_AVIF
    return _msg_unsupported_format(fmt or "不明")


def _is_animated(im: Image.Image) -> bool:
    if bool(getattr(im, "is_animated", False)):
        return True
    try:
        return int(getattr(im, "n_frames", 1) or 1) > 1
    except Exception:  # noqa: BLE001 - 形式によっては n_frames が失敗しうる
        return False


def _exif_orientation(im: Image.Image) -> int:
    try:
        exif = im.getexif()
    except Exception:  # noqa: BLE001 - 壊れた EXIF で本処理を止めない
        return 1
    try:
        return int(exif.get(_EXIF_ORIENTATION, 1) or 1)
    except (TypeError, ValueError):
        return 1


def _check_geometry(width: int, height: int) -> None:
    """最小寸法と縦横比の検証（EXIF 回転後の寸法で行う）。"""
    if width < MIN_W or height < MIN_H:
        raise StartImageError(_msg_too_small(width, height))
    ratio = width / height
    if ratio < AR_MIN or ratio > AR_MAX:
        raise StartImageError(_msg_bad_ratio(width, height))


def _kept_area_ratio(width: int, height: int) -> float:
    """中央クロップで残る面積比。クロップ先は 1.8:1（出力そのものの形）。"""
    ratio = width / height
    if ratio > TARGET_AR:      # 横に長い → 左右を捨てる
        return TARGET_AR / ratio
    if ratio < TARGET_AR:      # 縦に長い → 上下を捨てる
        return ratio / TARGET_AR
    return 1.0


def _verify_target_size(path: Path) -> None:
    """昇格前の最終検証: PNG で 576×320 ちょうどであること（ワーカーの受入条件）。"""
    try:
        with Image.open(path) as img:
            img.load()
            size = (img.width, img.height)
            fmt = img.format
    except Exception as e:  # noqa: BLE001 - fileops のエラーへ寄せる
        raise FileopsError(f"画像として開けません: {path.name}") from e
    if fmt != "PNG":
        raise FileopsError(f"PNG ではありません: {path.name}")
    if size != TARGET_SIZE:
        raise FileopsError(f"画像サイズが不正です: {path.name}（{size[0]}×{size[1]}）")


def _staging_dir(data_root: Path) -> Path:
    return Path(data_root).resolve() / "start_images" / "staging"


def _final_dir(data_root: Path) -> Path:
    return Path(data_root).resolve() / "start_images"


def _write_atomic(png_bytes: bytes, final_path: Path) -> None:
    """`.partial` → flush → fsync → 検証 → `os.replace()`（設計書 §10.7）。

    失敗時は `.partial` を残さない（staging に孤児を溜めないため）。
    """
    final_path.parent.mkdir(parents=True, exist_ok=True)
    part = partial_path(final_path)
    try:
        with open(part, "wb") as f:
            f.write(png_bytes)
            f.flush()
            os.fsync(f.fileno())
        promote(part, final_path, (verify_png, _verify_target_size))
    except Exception:
        _silent_unlink(part)
        raise


# --- 正規化 -------------------------------------------------------------------


def normalize_start_image(
    src_path: str | Path,
    *,
    data_root: Path,
    upload_root: Path | None = None,
) -> StartImageResult:
    """アップロードされた画像を検証・正規化し、staging へ原子的に保存する。

    `upload_root` が与えられたら、`src_path` がその配下であることを要求する
    （UI から渡された一時パスを信用しないため）。None なら省略（テスト用）。
    失敗はすべて `StartImageError`（日本語）。
    """
    if src_path is None or (isinstance(src_path, str) and not src_path.strip()):
        raise StartImageError(MSG_RECEIVE_FAILED)

    raw = Path(src_path)
    # symlink は resolve() の前に見る（resolve するとリンクかどうかが判らなくなる）
    if raw.is_symlink():
        raise StartImageError(MSG_RECEIVE_FAILED)
    try:
        resolved = raw.resolve()
    except OSError:
        raise StartImageError(MSG_RECEIVE_FAILED) from None

    if upload_root is not None:
        try:
            resolved = ensure_within(Path(upload_root), resolved)
        except FileopsError:
            raise StartImageError(MSG_RECEIVE_FAILED) from None

    if not resolved.is_file():
        raise StartImageError(MSG_RECEIVE_FAILED)
    try:
        size_bytes = resolved.stat().st_size
    except OSError:
        raise StartImageError(MSG_RECEIVE_FAILED) from None
    if size_bytes > MAX_UPLOAD_BYTES:
        raise StartImageError(_msg_file_too_large(size_bytes))

    with _NORMALIZE_LOCK:
        return _normalize_locked(resolved, Path(data_root))


def _normalize_locked(src: Path, data_root: Path) -> StartImageResult:
    header = _read_header(src)
    try:
        im = Image.open(src)
    except Image.DecompressionBombError:
        # PIL 自身が open 時点で拒否する大きさ（既定で約1.79億画素超）。寸法は判らない。
        raise StartImageError(MSG_PIXELS_TOO_LARGE) from None
    except Exception:  # noqa: BLE001 - 非画像・HEIC・SVG をまとめて案内へ
        raise StartImageError(_msg_for_unopenable(header)) from None

    try:
        png_bytes, meta = _decode_and_fit(im, header)
    finally:
        try:
            im.close()
        except Exception:  # noqa: BLE001 - 片づけ失敗で本処理を止めない
            pass

    start_image_id = ID_PREFIX + hashlib.sha256(png_bytes).hexdigest()[:12]
    staging = _staging_dir(data_root)
    final_path = staging / f"{start_image_id}.png"
    try:
        _write_atomic(png_bytes, final_path)
    except Exception:  # noqa: BLE001 - 保存失敗の内部事情は利用者に出さない
        raise StartImageError(MSG_RECEIVE_FAILED) from None

    warnings: list[str] = []
    lost = 1.0 - meta["kept_area_ratio"]
    if lost > WARN_CROP_LOSS:
        warnings.append(_msg_cropped(int(round(lost * 100))))
    if meta["flattened"]:
        warnings.append(WARN_FLATTENED)

    return StartImageResult(
        start_image_id=start_image_id,
        staged_path=final_path,
        png_bytes=png_bytes,
        source_size=meta["source_size"],
        source_format=meta["source_format"],
        cropped=meta["cropped"],
        kept_area_ratio=meta["kept_area_ratio"],
        flattened=meta["flattened"],
        passthrough=meta["passthrough"],
        warnings=tuple(warnings),
    )


def _decode_and_fit(im: Image.Image, header: bytes) -> tuple[bytes, dict]:
    """検証 → デコード → EXIF回転 → 透過合成 → 中央クロップ → PNG バイト列。"""
    fmt = (im.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise StartImageError(_msg_for_format(fmt, header))
    if _is_animated(im):
        raise StartImageError(MSG_ANIMATED)
    if im.mode not in ALLOWED_MODES:
        # I / I;16 / F は 8bit へ落とすと白飛びするので、黙って変換せず拒否する
        if im.mode.startswith(("I", "F")):
            raise StartImageError(MSG_HIGH_BIT_DEPTH)
        raise StartImageError(MSG_NOT_IMAGE)

    width, height = im.size
    if width <= 0 or height <= 0:
        raise StartImageError(MSG_NOT_IMAGE)
    # ここで確実に弾くことで、PIL の警告どまりの中間サイズの画像爆弾もデコードしない
    if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
        raise StartImageError(_msg_pixels_too_large(width, height))

    if _exif_orientation(im) in _SWAPPED_ORIENTATIONS:
        width, height = height, width
    _check_geometry(width, height)

    try:
        im.load()
    except MemoryError:
        raise StartImageError(_msg_pixels_too_large(width, height)) from None
    except Exception:  # noqa: BLE001 - 途中で切れた JPEG/PNG はここで捕まる
        raise StartImageError(MSG_BROKEN) from None

    try:
        oriented = ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001 - 壊れた EXIF
        raise StartImageError(MSG_BROKEN) from None

    src_w, src_h = oriented.size
    _check_geometry(src_w, src_h)

    src_mode = oriented.mode
    has_alpha = src_mode in ("RGBA", "LA", "PA") or "transparency" in oriented.info
    flattened = False
    if has_alpha:
        rgba = oriented.convert("RGBA")
        try:
            min_alpha = rgba.getchannel("A").getextrema()[0]
        except Exception:  # noqa: BLE001 - 取得できなければ塗りつぶし扱いにする
            min_alpha = 0
        flattened = min_alpha < 255
        background = Image.new("RGBA", rgba.size, (*TRANSPARENT_BG, 255))
        background.alpha_composite(rgba)
        rgb = background.convert("RGB")
    elif src_mode != "RGB":
        rgb = oriented.convert("RGB")
    else:
        rgb = oriented

    kept = _kept_area_ratio(src_w, src_h)
    cropped = kept < 0.9999

    if (src_w, src_h) == TARGET_SIZE:
        # 576×320 ちょうどは ImageOps.fit を通さない（画素を1ビットも変えない）
        fitted = rgb
        passthrough = (src_mode == "RGB") and not flattened
    else:
        fitted = ImageOps.fit(
            rgb, TARGET_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )
        passthrough = False

    # デコード済み画素だけから作り直して ICC/EXIF/テキストチャンクを完全に落とす。
    # （PNG の保存側は im.info の icc_profile / exif を拾うため、info ごと捨てる）
    clean = Image.frombytes("RGB", TARGET_SIZE, fitted.tobytes())
    buf = io.BytesIO()
    clean.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    meta = {
        "source_size": (src_w, src_h),
        "source_format": fmt,
        "cropped": cropped,
        "kept_area_ratio": kept,
        "flattened": flattened,
        "passthrough": passthrough,
    }
    return png_bytes, meta


# --- 確定・解決・片づけ -------------------------------------------------------


def commit_start_image(start_image_id: str, *, data_root: Path) -> tuple[Path, bool]:
    """staging の画像をジョブ用の正式パスへ確定する。

    戻り値 `(final_path, created)`。`created` は「この呼び出しで新規作成したか」。
    既に正式ファイルがあれば何もせず `(path, False)` を返す（＝二重クリックで上書きしない）。
    """
    if not is_valid_start_image_id(start_image_id):
        raise StartImageError(MSG_NOT_FOUND)

    final_dir = _final_dir(data_root)
    final_path = final_dir / f"{start_image_id}.png"
    staged = final_dir / "staging" / f"{start_image_id}.png"

    with _COMMIT_LOCK:
        if final_path.is_file() and not final_path.is_symlink():
            return final_path, False
        if final_path.is_symlink() or staged.is_symlink() or not staged.is_file():
            raise StartImageError(MSG_NOT_FOUND)
        try:
            data = staged.read_bytes()
        except OSError:
            raise StartImageError(MSG_NOT_FOUND) from None
        # ID は正規化した PNG バイト列のハッシュ。ここで照合しておけば、
        # ジョブへ渡る画像がプレビューした画像と同一であることを保証できる。
        if ID_PREFIX + hashlib.sha256(data).hexdigest()[:12] != start_image_id:
            raise StartImageError(MSG_NOT_FOUND)
        try:
            _write_atomic(data, final_path)
        except Exception:  # noqa: BLE001
            raise StartImageError(MSG_RECEIVE_FAILED) from None
        return final_path, True


def discard_start_image(final_path: Path, *, data_root: Path) -> None:
    """commit で新規作成した正式ファイルを片づける（キュー登録失敗時のみ呼ぶ）。"""
    base = _final_dir(data_root)
    target = Path(final_path)
    if target.is_symlink():
        raise StartImageError(MSG_RECEIVE_FAILED)
    try:
        resolved = ensure_within(base, target)
    except FileopsError:
        raise StartImageError(MSG_RECEIVE_FAILED) from None
    # staging や配下の別ディレクトリは対象外（正式ファイルだけを消す）
    if resolved.parent != base:
        raise StartImageError(MSG_RECEIVE_FAILED)
    if resolved.suffix != ".png" or not is_valid_start_image_id(resolved.stem):
        raise StartImageError(MSG_RECEIVE_FAILED)
    _silent_unlink(resolved)


def resolve_start_image(start_image_id: str, *, data_root: Path) -> Path:
    """ID 文字列からジョブ用の正式パスを解決する。ID 形式・data_root 配下・実在を検証。"""
    if not is_valid_start_image_id(start_image_id):
        raise StartImageError(MSG_NOT_FOUND)
    base = _final_dir(data_root)
    path = base / f"{start_image_id}.png"
    if path.is_symlink():
        raise StartImageError(MSG_NOT_FOUND)
    try:
        resolved = ensure_within(base, path)
    except FileopsError:
        raise StartImageError(MSG_NOT_FOUND) from None
    if not resolved.is_file():
        raise StartImageError(MSG_NOT_FOUND)
    return resolved


def cleanup_staging(data_root: Path, *, max_age_sec: int = STAGING_MAX_AGE_SEC) -> int:
    """起動時掃除。staging 内の古いファイルと孤児 `.partial` を消して件数を返す。

    `.partial` は起動時点で必ず孤児なので、経過時間によらず消す。
    """
    staging = _staging_dir(data_root)
    if not staging.is_dir():
        return 0
    now = time.time()
    removed = 0
    for entry in sorted(staging.iterdir()):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if entry.name.endswith(PARTIAL_SUFFIX):
                entry.unlink()
                removed += 1
                continue
            if now - entry.stat().st_mtime > max_age_sec:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed
