"""開始画像の取込・正規化・セキュリティ層のテスト（P8）。

テスト用の画像はすべて tmp_path 内で自作する（data/ の実ファイルは一切使わない）。
"""

import hashlib
import io
import os
import random
import struct
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from app.core.fileops import FileopsError
from app.core.start_image import (
    ID_PREFIX,
    MAX_UPLOAD_BYTES,
    TARGET_H,
    TARGET_SIZE,
    TARGET_W,
    StartImageError,
    cleanup_staging,
    commit_start_image,
    discard_start_image,
    is_valid_start_image_id,
    normalize_start_image,
    resolve_start_image,
)

# --- ヘルパ -------------------------------------------------------------------


def _roots(tmp_path):
    """(data_root, upload_root) を作って返す。"""
    data_root = tmp_path / "data"
    upload_root = tmp_path / "upload"
    data_root.mkdir(exist_ok=True)
    upload_root.mkdir(exist_ok=True)
    return data_root, upload_root


def _staging(data_root: Path) -> Path:
    return data_root / "start_images" / "staging"


def _finals(data_root: Path) -> Path:
    return data_root / "start_images"


def _noise(width: int, height: int, seed: int = 1) -> Image.Image:
    """圧縮の効かないランダム画像（画素一致の検証用）。"""
    rnd = random.Random(seed)
    return Image.frombytes("RGB", (width, height), rnd.randbytes(width * height * 3))


def _gradient(width: int, height: int) -> Image.Image:
    """決定的で見分けのつく画像（左上から右下へ色が変わる）。"""
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(0, height, 8):
        for x in range(0, width, 8):
            color = (x * 255 // max(width - 1, 1), y * 255 // max(height - 1, 1), 128)
            for dy in range(min(8, height - y)):
                for dx in range(min(8, width - x)):
                    px[x + dx, y + dy] = color
    return img


def _save(img: Image.Image, path: Path, **kwargs) -> Path:
    img.save(path, **kwargs)
    return path


def _open_result(result) -> Image.Image:
    img = Image.open(io.BytesIO(result.png_bytes))
    img.load()
    return img


def _png_chunk_types(data: bytes) -> list[str]:
    """PNG のチャンク種別を並び順に取り出す。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG シグネチャがありません"
    types: list[str] = []
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        types.append(data[pos + 4 : pos + 8].decode("ascii"))
        pos += 12 + length
    return types


def _fake_large_png(path: Path, width: int, height: int) -> Path:
    """IHDR だけ巨大な PNG（open() は成功するがデコードはさせない）。"""

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"\x00" * 8) + chunk(b"IEND", b"")
    )
    return path


def _srgb_icc() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


# --- 受け入れ ----------------------------------------------------------------


def test_accepts_png_jpeg_and_still_webp(tmp_path):
    """PNG・JPEG・非アニメーション WebP を受け付ける。"""
    data_root, upload = _roots(tmp_path)
    src = _gradient(1152, 640)
    cases = [
        ("a.png", {"format": "PNG"}, "PNG"),
        ("b.jpg", {"format": "JPEG", "quality": 95}, "JPEG"),
        ("c.webp", {"format": "WEBP", "lossless": True}, "WEBP"),
    ]
    for name, kwargs, expected in cases:
        path = _save(src, upload / name, **kwargs)
        result = normalize_start_image(path, data_root=data_root, upload_root=upload)
        assert result.source_format == expected, f"{name} の形式判定が違う"
        assert _open_result(result).size == TARGET_SIZE, f"{name} が 576×320 になっていない"
        assert result.staged_path.is_file(), f"{name} が staging に保存されていない"


def test_exact_size_rgb_png_is_pixel_identical(tmp_path):
    """576×320 の RGB PNG は画素が1ビットも変わらず passthrough になる。"""
    data_root, upload = _roots(tmp_path)
    src = _noise(TARGET_W, TARGET_H, seed=7)
    path = _save(src, upload / "exact.png", format="PNG")

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    out = _open_result(result)
    assert out.mode == "RGB"
    assert out.tobytes() == src.tobytes(), "576×320 の画像で画素が変化した"
    assert result.passthrough is True
    assert result.cropped is False
    assert result.flattened is False
    assert result.kept_area_ratio == pytest.approx(1.0)
    assert result.warnings == (), "passthrough では警告を出さない"


@pytest.mark.parametrize(
    "size",
    [(1536, 1024), (1000, 1000), (600, 1000), (1920, 1080), (2000, 700)],
)
def test_various_shapes_become_target_size(tmp_path, size):
    """3:2・正方形・縦長・16:9・横長のいずれも 576×320 になる。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(*size), upload / "src.png", format="PNG")

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert _open_result(result).size == TARGET_SIZE
    assert result.source_size == size
    assert result.passthrough is False


def test_crop_region_keeps_aspect_ratio(tmp_path):
    """クロップ領域の縦横比は 1.8（＝出力の形）で、引き伸ばしは起きない。"""
    data_root, upload = _roots(tmp_path)
    for width, height in ((1536, 1024), (1000, 1000), (2000, 700), (600, 1000)):
        path = _save(_gradient(width, height), upload / f"{width}x{height}.png", format="PNG")
        result = normalize_start_image(path, data_root=data_root, upload_root=upload)

        if width / height < TARGET_W / TARGET_H:
            crop_w, crop_h = width, height * result.kept_area_ratio
        else:
            crop_w, crop_h = width * result.kept_area_ratio, height
        assert crop_w / crop_h == pytest.approx(1.8), (
            f"{width}×{height} のクロップ領域が 1.8:1 になっていない"
        )


def test_square_object_stays_square(tmp_path):
    """正方形の被写体が出力でも正方形のまま（縦横に引き伸ばしていない）。"""
    data_root, upload = _roots(tmp_path)
    src = Image.new("RGB", (1152, 1024), (0, 0, 0))
    src.paste((255, 255, 255), (320, 256, 832, 768))  # 中央に 512×512 の白い正方形
    path = _save(src, upload / "square.png", format="PNG")

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    mask = _open_result(result).convert("L").point(lambda v: 255 if v > 128 else 0)
    box = mask.getbbox()
    assert box is not None, "白い正方形が出力に見つからない"
    got_w, got_h = box[2] - box[0], box[3] - box[1]
    assert abs(got_w - got_h) <= 2, f"正方形が歪んだ（{got_w}×{got_h}）"
    assert 250 <= got_w <= 262, f"倍率が想定外（{got_w}×{got_h}）"


def test_exif_orientation_is_applied(tmp_path):
    """EXIF Orientation=6 の JPEG が正しく回転される。"""
    data_root, upload = _roots(tmp_path)
    displayed = Image.new("RGB", (1152, 640))
    displayed.paste((255, 0, 0), (0, 0, 576, 640))      # 左半分＝赤
    displayed.paste((0, 0, 255), (576, 0, 1152, 640))   # 右半分＝青
    stored = displayed.transpose(Image.Transpose.ROTATE_90)
    exif = Image.Exif()
    exif[0x0112] = 6
    path = upload / "rotated.jpg"
    stored.save(path, format="JPEG", quality=95, exif=exif)

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert result.source_size == (1152, 640), "EXIF回転後の寸法になっていない"
    out = _open_result(result)
    left = out.getpixel((100, 160))
    right = out.getpixel((476, 160))
    assert left[0] > 180 and left[2] < 80, f"左半分が赤くない: {left}"
    assert right[2] > 180 and right[0] < 80, f"右半分が青くない: {right}"


def test_rgba_transparency_is_flattened_to_black(tmp_path):
    """RGBA の透過は黒で塗りつぶされ、flattened と警告が立つ。"""
    data_root, upload = _roots(tmp_path)
    src = Image.new("RGBA", (1152, 640), (255, 255, 255, 255))
    src.paste((255, 255, 255, 0), (576, 0, 1152, 640))  # 右半分を完全透過
    path = _save(src, upload / "alpha.png", format="PNG")

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert result.flattened is True
    assert "透過部分は黒で塗りつぶしました。" in result.warnings
    out = _open_result(result)
    assert out.mode == "RGB"
    assert out.getpixel((100, 160)) == (255, 255, 255)
    assert out.getpixel((476, 160)) == (0, 0, 0), "透過部分が黒になっていない"


def test_opaque_rgba_is_not_reported_as_flattened(tmp_path):
    """透過が無い RGBA では余計な告知を出さない。"""
    data_root, upload = _roots(tmp_path)
    src = Image.new("RGBA", (1152, 640), (20, 40, 60, 255))
    path = _save(src, upload / "opaque.png", format="PNG")

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert result.flattened is False
    assert result.warnings == ()


def test_palette_transparency_is_flattened(tmp_path):
    """パレット（P）画像の透過も黒で塗りつぶす。"""
    data_root, upload = _roots(tmp_path)
    base = Image.new("RGBA", (1152, 640), (255, 0, 0, 255))
    base.paste((0, 255, 0, 0), (576, 0, 1152, 640))
    path = upload / "palette.png"
    base.convert("P", palette=Image.Palette.ADAPTIVE, colors=8).save(path, transparency=0)

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert result.flattened is True
    assert _open_result(result).mode == "RGB"


@pytest.mark.parametrize(
    ("mode", "name", "kwargs"),
    [("CMYK", "cmyk.jpg", {"format": "JPEG"}), ("L", "gray.png", {"format": "PNG"})],
)
def test_other_color_modes_become_rgb(tmp_path, mode, name, kwargs):
    """CMYK・グレースケールは RGB へ変換して受け付ける（passthrough にはしない）。"""
    data_root, upload = _roots(tmp_path)
    path = _save(Image.new(mode, TARGET_SIZE, 128 if mode == "L" else (0, 255, 255, 0)),
                 upload / name, **kwargs)

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert _open_result(result).mode == "RGB"
    assert result.passthrough is False, "モード変換したものを画素不変とみなしてはいけない"


def test_icc_profile_is_accepted_and_stripped(tmp_path):
    """ICC プロファイル付き画像は通り、出力に ICC は残らない。"""
    data_root, upload = _roots(tmp_path)
    path = upload / "icc.png"
    _gradient(1152, 640).save(path, format="PNG", icc_profile=_srgb_icc())

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert "iCCP" not in _png_chunk_types(result.png_bytes)
    assert "icc_profile" not in _open_result(result).info, "ICC が出力に残っている"


def test_output_png_is_clean(tmp_path):
    """出力は RGB・PNG・576×320 で、EXIF や tEXt/iCCP/tIME を持たない。"""
    data_root, upload = _roots(tmp_path)
    exif = Image.Exif()
    exif[0x010E] = "secret description"
    path = upload / "meta.jpg"
    _gradient(1600, 900).save(path, format="JPEG", quality=90, exif=exif, icc_profile=_srgb_icc())

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    out = _open_result(result)
    assert out.format == "PNG"
    assert out.mode == "RGB"
    assert out.size == TARGET_SIZE
    assert dict(out.getexif()) == {}, "EXIF が残っている"
    types = _png_chunk_types(result.png_bytes)
    for banned in ("tEXt", "iTXt", "zTXt", "iCCP", "tIME", "eXIf"):
        assert banned not in types, f"{banned} チャンクが残っている"
    assert result.staged_path.read_bytes() == result.png_bytes


def test_normalization_is_deterministic(tmp_path):
    """同じ入力からは同じ PNG バイト列（ID）ができる。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1536, 1024), upload / "det.png", format="PNG")

    first = normalize_start_image(path, data_root=data_root, upload_root=upload)
    second = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert hashlib.sha256(first.png_bytes).hexdigest() == hashlib.sha256(second.png_bytes).hexdigest()
    assert first.start_image_id == second.start_image_id
    assert first.start_image_id.startswith(ID_PREFIX)
    assert is_valid_start_image_id(first.start_image_id)


def test_crop_warning_only_when_large_loss(tmp_path):
    """25%超を捨てたときだけクロップ警告を出す（16:9 では出ない）。"""
    data_root, upload = _roots(tmp_path)
    square = _save(_gradient(1000, 1000), upload / "square.png", format="PNG")
    wide = _save(_gradient(1920, 1080), upload / "wide.png", format="PNG")

    square_result = normalize_start_image(square, data_root=data_root, upload_root=upload)
    wide_result = normalize_start_image(wide, data_root=data_root, upload_root=upload)

    assert square_result.cropped is True
    assert any("切り取りました" in w for w in square_result.warnings), "正方形で警告が出ていない"
    assert wide_result.cropped is True, "16:9 でも僅かにクロップは起きる"
    assert wide_result.warnings == (), "16:9 で不要な警告が出ている"


def test_extension_spoofing_is_accepted(tmp_path):
    """拡張子が偽装されていても実体で判定するので通る（.png という名の JPEG）。"""
    data_root, upload = _roots(tmp_path)
    path = upload / "actually_jpeg.png"
    _gradient(1152, 640).save(path, format="JPEG", quality=90)

    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert result.source_format == "JPEG"
    assert _open_result(result).size == TARGET_SIZE


# --- 拒否 --------------------------------------------------------------------


def test_truncated_png_is_rejected(tmp_path):
    """途中で切れた PNG を拒否する。"""
    data_root, upload = _roots(tmp_path)
    buf = io.BytesIO()
    _noise(1152, 640).save(buf, format="PNG")
    path = upload / "broken.png"
    path.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])

    with pytest.raises(StartImageError, match="壊れている"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_truncated_jpeg_is_rejected(tmp_path):
    """load() で初めて失敗する壊れた JPEG を拒否する。"""
    data_root, upload = _roots(tmp_path)
    buf = io.BytesIO()
    _noise(1152, 640).save(buf, format="JPEG", quality=95)
    path = upload / "broken.jpg"
    path.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])

    with pytest.raises(StartImageError, match="壊れている"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_file_size_limit(tmp_path):
    """32MB を超えるファイルを拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = upload / "huge.png"
    with open(path, "wb") as f:
        f.truncate(MAX_UPLOAD_BYTES + 1)

    with pytest.raises(StartImageError, match="ファイルが大きすぎます"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_pixel_count_limit(tmp_path):
    """5000万画素を超える画像をデコード前に拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _fake_large_png(upload / "bomb.png", 10000, 8000)

    with pytest.raises(StartImageError, match="画像が大きすぎます（10000×8000）"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_decompression_bomb_is_rejected_with_size_message(tmp_path):
    """PIL が open 時点で弾く大きさでも「大きすぎます」と案内する。"""
    data_root, upload = _roots(tmp_path)
    path = _fake_large_png(upload / "megabomb.png", 20000, 20000)

    with pytest.raises(StartImageError, match="画像が大きすぎます"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_max_side_limit(tmp_path):
    """辺が 12000px を超える画像を拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _fake_large_png(upload / "long.png", 12001, 400)

    with pytest.raises(StartImageError, match="画像が大きすぎます"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


@pytest.mark.parametrize("size", [(500, 400), (576, 319), (400, 320)])
def test_too_small_is_rejected(tmp_path, size):
    """576×320 未満の画像は（拡大になるので）拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(*size), upload / "small.png", format="PNG")

    with pytest.raises(StartImageError, match="画像が小さすぎます"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


@pytest.mark.parametrize("size", [(2000, 400), (600, 1400)])
def test_extreme_aspect_ratio_is_rejected(tmp_path, size):
    """縦横比が極端な画像を拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(*size), upload / "extreme.png", format="PNG")

    with pytest.raises(StartImageError, match="画像の形が極端です"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_svg_is_rejected(tmp_path):
    """SVG（画像として開けない）を専用の案内で拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = upload / "logo.svg"
    path.write_bytes(
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
        b'width="1152" height="640"></svg>'
    )

    with pytest.raises(StartImageError, match="SVG 形式には対応していません"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_heic_like_file_is_rejected(tmp_path):
    """HEIC（開けないファイル）を iPhone 向けの案内で拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = upload / "IMG_0001.HEIC"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 64)

    with pytest.raises(StartImageError, match="HEIC 形式には対応していません"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_gif_is_rejected(tmp_path):
    """GIF を拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1152, 640), upload / "a.gif", format="GIF")

    with pytest.raises(StartImageError, match="GIF 形式には対応していません"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


@pytest.mark.parametrize(("fmt", "name"), [("TIFF", "a.tif"), ("BMP", "a.bmp")])
def test_other_formats_are_rejected(tmp_path, fmt, name):
    """TIFF・BMP を「対応していない画像形式」として拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1152, 640), upload / name, format=fmt)

    with pytest.raises(StartImageError, match=f"対応していない画像形式です（{fmt}）"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


@pytest.mark.parametrize(("fmt", "name"), [("WEBP", "anim.webp"), ("PNG", "anim.png")])
def test_animated_images_are_rejected(tmp_path, fmt, name):
    """アニメーション WebP と APNG は先頭フレームを使わず拒否する。"""
    data_root, upload = _roots(tmp_path)
    frames = [Image.new("RGB", (1152, 640), (i * 60, 10, 10)) for i in range(3)]
    path = upload / name
    frames[0].save(path, format=fmt, save_all=True, append_images=frames[1:], duration=100)

    with pytest.raises(StartImageError, match="動く画像（アニメーション）"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_16bit_image_is_rejected(tmp_path):
    """16bit（I;16）の画像を拒否する。"""
    data_root, upload = _roots(tmp_path)
    path = _save(Image.new("I;16", (1152, 640)), upload / "deep.png", format="PNG")

    with pytest.raises(StartImageError, match="16bit（HDR）"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)


def test_symlink_is_rejected(tmp_path):
    """symlink 経由の指定を拒否する。"""
    data_root, upload = _roots(tmp_path)
    real = _save(_gradient(1152, 640), upload / "real.png", format="PNG")
    link = upload / "link.png"
    os.symlink(real, link)

    with pytest.raises(StartImageError, match="画像を受け取れませんでした"):
        normalize_start_image(link, data_root=data_root, upload_root=upload)


def test_path_outside_upload_root_is_rejected(tmp_path):
    """upload_root の外にあるパスを拒否する。"""
    data_root, upload = _roots(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    path = _save(_gradient(1152, 640), outside / "x.png", format="PNG")

    with pytest.raises(StartImageError, match="画像を受け取れませんでした"):
        normalize_start_image(path, data_root=data_root, upload_root=upload)
    # upload_root を渡さなければ同じファイルが通る（境界検証だけが理由であることの確認）
    assert normalize_start_image(path, data_root=data_root).staged_path.is_file()


def test_missing_file_is_rejected(tmp_path):
    """存在しないパスを拒否する。"""
    data_root, upload = _roots(tmp_path)
    with pytest.raises(StartImageError, match="画像を受け取れませんでした"):
        normalize_start_image(upload / "nope.png", data_root=data_root, upload_root=upload)


def test_failure_leaves_no_orphan_in_staging(tmp_path, monkeypatch):
    """保存に失敗しても staging に .partial も正式名も残さない。"""
    import app.core.start_image as start_image

    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1152, 640), upload / "ok.png", format="PNG")

    def _boom(_path):
        raise FileopsError("検証に失敗しました")

    monkeypatch.setattr(start_image, "verify_png", _boom)

    with pytest.raises(StartImageError):
        normalize_start_image(path, data_root=data_root, upload_root=upload)

    assert sorted(p.name for p in _staging(data_root).iterdir()) == [], "staging に孤児が残った"


# --- 確定・解決・破棄・掃除 ---------------------------------------------------


def test_commit_promotes_and_is_idempotent(tmp_path):
    """commit は .partial 経由で昇格し、2回目は created=False で上書きしない。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1536, 1024), upload / "c.png", format="PNG")
    result = normalize_start_image(path, data_root=data_root, upload_root=upload)

    final_path, created = commit_start_image(result.start_image_id, data_root=data_root)
    assert created is True
    assert final_path == _finals(data_root) / f"{result.start_image_id}.png"
    assert final_path.read_bytes() == result.png_bytes, "確定した画像がプレビューと違う"
    assert not (final_path.parent / f"{final_path.name}.partial").exists()

    marker = b"do-not-overwrite"
    final_path.write_bytes(marker)
    again_path, again_created = commit_start_image(result.start_image_id, data_root=data_root)
    assert again_created is False
    assert again_path == final_path
    assert final_path.read_bytes() == marker, "2回目の commit が上書きしてしまった"


def test_commit_rejects_unknown_and_invalid_ids(tmp_path):
    """staging に無い ID・形式が不正な ID を拒否する。"""
    data_root, _ = _roots(tmp_path)
    with pytest.raises(StartImageError, match="見つかりません"):
        commit_start_image("si_" + "0" * 12, data_root=data_root)
    with pytest.raises(StartImageError, match="見つかりません"):
        commit_start_image("../etc/passwd", data_root=data_root)


def test_commit_rejects_tampered_staging_file(tmp_path):
    """staging の中身が ID と一致しなければ確定しない。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1152, 640), upload / "t.png", format="PNG")
    result = normalize_start_image(path, data_root=data_root, upload_root=upload)
    result.staged_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk")

    with pytest.raises(StartImageError, match="見つかりません"):
        commit_start_image(result.start_image_id, data_root=data_root)
    assert not (_finals(data_root) / f"{result.start_image_id}.png").exists()


def test_discard_removes_committed_file(tmp_path):
    """discard は確定済みの正式ファイルを消す。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1152, 640), upload / "d.png", format="PNG")
    result = normalize_start_image(path, data_root=data_root, upload_root=upload)
    final_path, _ = commit_start_image(result.start_image_id, data_root=data_root)

    discard_start_image(final_path, data_root=data_root)

    assert not final_path.exists()
    discard_start_image(final_path, data_root=data_root)  # 2回目でも例外にしない


def test_discard_rejects_paths_outside_data_root(tmp_path):
    """discard は data_root の外や staging のファイルを拒否する。"""
    data_root, upload = _roots(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(StartImageError, match="画像を受け取れませんでした"):
        discard_start_image(outside, data_root=data_root)
    assert outside.exists(), "data_root 外のファイルを消してはいけない"

    path = _save(_gradient(1152, 640), upload / "s.png", format="PNG")
    result = normalize_start_image(path, data_root=data_root, upload_root=upload)
    with pytest.raises(StartImageError, match="画像を受け取れませんでした"):
        discard_start_image(result.staged_path, data_root=data_root)
    assert result.staged_path.exists(), "staging のファイルを消してはいけない"


def test_resolve_returns_committed_path(tmp_path):
    """resolve は確定済みの正式パスを返す。"""
    data_root, upload = _roots(tmp_path)
    path = _save(_gradient(1152, 640), upload / "r.png", format="PNG")
    result = normalize_start_image(path, data_root=data_root, upload_root=upload)
    final_path, _ = commit_start_image(result.start_image_id, data_root=data_root)

    assert resolve_start_image(result.start_image_id, data_root=data_root) == final_path


@pytest.mark.parametrize(
    "bad_id",
    ["", "si_", "si_ZZZZZZZZZZZZ", "si_0123456789ab.png", "../../etc/passwd", "si_0123456789abc"],
)
def test_resolve_rejects_invalid_ids(tmp_path, bad_id):
    """形式が不正な ID を拒否する。"""
    data_root, _ = _roots(tmp_path)
    with pytest.raises(StartImageError, match="見つかりません"):
        resolve_start_image(bad_id, data_root=data_root)


def test_resolve_rejects_missing_and_escaping_files(tmp_path):
    """実在しない ID と、data_root の外を指す symlink を拒否する。"""
    data_root, _ = _roots(tmp_path)
    finals = _finals(data_root)
    finals.mkdir(parents=True, exist_ok=True)
    with pytest.raises(StartImageError, match="見つかりません"):
        resolve_start_image("si_" + "a" * 12, data_root=data_root)

    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")
    escaping_id = "si_" + "b" * 12
    os.symlink(outside, finals / f"{escaping_id}.png")
    with pytest.raises(StartImageError, match="見つかりません"):
        resolve_start_image(escaping_id, data_root=data_root)


def test_is_valid_start_image_id():
    """ID の形式検証。"""
    assert is_valid_start_image_id("si_0123456789ab")
    assert not is_valid_start_image_id("si_0123456789AB")  # 大文字は不許可
    assert not is_valid_start_image_id("si_0123456789")
    assert not is_valid_start_image_id("v_20260807_101530_abcd")
    assert not is_valid_start_image_id(None)
    assert not is_valid_start_image_id(Path("si_0123456789ab"))


def test_cleanup_staging_removes_old_files_and_partials(tmp_path):
    """掃除は古いファイルと孤児 .partial を消し、新しいものを残す。"""
    data_root, _ = _roots(tmp_path)
    staging = _staging(data_root)
    staging.mkdir(parents=True, exist_ok=True)
    fresh = staging / "si_000000000001.png"
    old = staging / "si_000000000002.png"
    orphan = staging / "si_000000000003.png.partial"
    for path in (fresh, old, orphan):
        path.write_bytes(b"x")
    long_ago = time.time() - (48 * 3600)
    os.utime(old, (long_ago, long_ago))

    removed = cleanup_staging(data_root)

    assert removed == 2
    assert fresh.exists(), "新しいファイルを消してはいけない"
    assert not old.exists()
    assert not orphan.exists()


def test_cleanup_staging_without_directory(tmp_path):
    """staging が無い状態でも例外にせず 0 を返す。"""
    data_root, _ = _roots(tmp_path)
    assert cleanup_staging(data_root) == 0


def test_error_messages_have_no_internal_details(tmp_path):
    """エラーメッセージに絶対パスや例外クラス名を含めない。"""
    data_root, upload = _roots(tmp_path)
    sources = [
        _save(_gradient(500, 400), upload / "small.png", format="PNG"),
        _save(_gradient(2000, 400), upload / "extreme.png", format="PNG"),
        _save(_gradient(1152, 640), upload / "a.gif", format="GIF"),
        _save(_gradient(1152, 640), upload / "a.tif", format="TIFF"),
        _fake_large_png(upload / "bomb.png", 10000, 8000),
        upload / "missing.png",
    ]
    messages = []
    for src in sources:
        with pytest.raises(StartImageError) as excinfo:
            normalize_start_image(src, data_root=data_root, upload_root=upload)
        messages.append(str(excinfo.value))

    with pytest.raises(StartImageError) as excinfo:
        commit_start_image("si_" + "0" * 12, data_root=data_root)
    messages.append(str(excinfo.value))

    for message in messages:
        assert message, "メッセージが空"
        assert "/" not in message, f"パスらしき文字が含まれる: {message}"
        assert str(tmp_path) not in message
        for leak in ("Error", "Exception", "Traceback", "PIL", "Path("):
            assert leak not in message, f"内部情報が含まれる: {message}"
