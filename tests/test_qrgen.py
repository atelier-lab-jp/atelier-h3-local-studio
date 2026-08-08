"""QRコード生成のユニットテスト（P5契約 §2・§7.3）。

- 完全ローカル生成（外部サービス・ネットワークを使わない）
- 符号化するのは URL のみ（PIN を絶対に入れない）
- PNG は 0o600・原子的昇格・確実な後始末

書き込み先はすべて `tmp_path`（プロジェクトの `data/` には触れない）。
"""

from __future__ import annotations

import io
import os
import socket
from pathlib import Path

import pytest
import segno

from app.core.qrgen import QR_FILENAME, QrArtifact, QrError, cleanup_qr, render_qr

URL = "http://192.168.1.23:7860"
PIN = "482913"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------- 正常系


def test_render_creates_png_under_given_directory(tmp_path: Path):
    art = render_qr(URL, tmp_path)
    assert isinstance(art, QrArtifact)
    assert art.png_path == tmp_path / QR_FILENAME
    assert art.png_path.is_file()
    assert art.url == URL


def test_render_creates_missing_directories(tmp_path: Path):
    target = tmp_path / "data" / "tmp"
    art = render_qr(URL, target)
    assert art.png_path.is_file()
    assert art.png_path.parent == target


def test_png_is_a_real_png_and_decodable(tmp_path: Path):
    art = render_qr(URL, tmp_path)
    data = art.png_path.read_bytes()
    assert data.startswith(PNG_MAGIC)
    assert len(data) > 0

    from PIL import Image

    with Image.open(art.png_path) as img:
        img.load()
        assert img.format == "PNG"
        assert img.width >= 100 and img.height >= 100
        assert img.width == img.height


def test_png_permission_is_owner_only(tmp_path: Path):
    art = render_qr(URL, tmp_path)
    assert (art.png_path.stat().st_mode & 0o777) == 0o600


def test_png_permission_is_owner_only_even_with_permissive_umask(tmp_path: Path):
    old = os.umask(0o000)
    try:
        art = render_qr(URL, tmp_path)
    finally:
        os.umask(old)
    assert (art.png_path.stat().st_mode & 0o777) == 0o600


def test_ascii_art_uses_half_block_characters(tmp_path: Path):
    art = render_qr(URL, tmp_path, ansi_color=False)
    assert art.ascii_art
    assert any(ch in art.ascii_art for ch in "█▀▄")
    lines = art.ascii_art.splitlines()
    assert len(lines) > 5
    assert len({len(line) for line in lines}) == 1  # 各行の幅がそろっている


def test_ascii_art_forces_colors_so_it_scans_on_any_terminal_theme(tmp_path: Path):
    """明るいテーマでも暗いテーマでも極性が正しくなるよう ANSI 色を固定する。"""
    art = render_qr(URL, tmp_path)
    assert "\x1b[38;5;231;48;5;16m" in art.ascii_art
    assert art.ascii_art.endswith("\x1b[0m")


def test_render_is_repeatable_and_overwrites_same_path(tmp_path: Path):
    first = render_qr(URL, tmp_path)
    second = render_qr(URL, tmp_path)
    assert first.png_path == second.png_path
    assert first.png_path.read_bytes() == second.png_path.read_bytes()
    assert list(tmp_path.iterdir()) == [first.png_path]  # .partial を残さない


def test_no_partial_file_is_left_behind(tmp_path: Path):
    render_qr(URL, tmp_path)
    assert not any(p.name.endswith(".partial") for p in tmp_path.iterdir())


# ---------------------------------------------------------------- 中身は URL だけ


def test_encoded_payload_is_exactly_the_url(tmp_path: Path):
    """生成物が「URL だけを符号化した QR」と1バイト単位で一致することを示す。"""
    art = render_qr(URL, tmp_path)

    expected = io.BytesIO()
    segno.make(URL, error="m").save(expected, kind="png", scale=8, border=4)
    assert art.png_path.read_bytes() == expected.getvalue()


def test_pin_is_never_encoded(tmp_path: Path):
    """PIN を混ぜた QR とは別物であること＝PIN が入っていないこと。"""
    art = render_qr(URL, tmp_path)

    with_pin = io.BytesIO()
    segno.make(f"{URL}/?pin={PIN}", error="m").save(
        with_pin, kind="png", scale=8, border=4
    )
    assert art.png_path.read_bytes() != with_pin.getvalue()

    assert PIN not in art.url
    assert PIN not in art.ascii_art
    assert PIN not in str(art.png_path)
    assert PIN.encode() not in art.png_path.read_bytes()


def test_artifact_has_no_pin_field():
    assert set(QrArtifact.__dataclass_fields__) == {"png_path", "ascii_art", "url"}


def test_artifact_is_frozen(tmp_path: Path):
    art = render_qr(URL, tmp_path)
    with pytest.raises(Exception):
        art.url = "http://example.com"  # type: ignore[misc]


def test_generation_works_without_any_network(tmp_path: Path, monkeypatch):
    """外部QRサービスを使わない（ソケットを禁止しても生成できる）。"""

    def no_socket(*args, **kwargs):
        raise AssertionError("QR生成でネットワークを使ってはいけません")

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    art = render_qr(URL, tmp_path)
    assert art.png_path.is_file()


# ---------------------------------------------------------------- 異常系


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "192.168.1.23:7860",
        "ftp://192.168.1.23",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "file:///etc/passwd",
        "http://192.168.1.23:7860 extra",
    ],
)
def test_render_rejects_non_http_url(bad: str, tmp_path: Path):
    with pytest.raises(QrError):
        render_qr(bad, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_render_rejects_non_string_url(tmp_path: Path):
    for bad in (None, 123, b"http://192.168.1.23"):
        with pytest.raises(QrError, match="URL"):
            render_qr(bad, tmp_path)  # type: ignore[arg-type]


def test_render_rejects_too_long_url(tmp_path: Path):
    with pytest.raises(QrError, match="長すぎ"):
        render_qr("http://192.168.1.23/" + "a" * 600, tmp_path)


def test_error_messages_are_japanese(tmp_path: Path):
    with pytest.raises(QrError) as excinfo:
        render_qr("", tmp_path)
    assert "指定されていません" in str(excinfo.value)


def test_render_reports_unwritable_directory(tmp_path: Path):
    blocked = tmp_path / "readonly"
    blocked.mkdir(mode=0o500)
    try:
        with pytest.raises(QrError, match="保存"):
            render_qr(URL, blocked)
    finally:
        blocked.chmod(0o700)


def test_partial_is_removed_when_save_fails(tmp_path: Path, monkeypatch):
    class Boom:
        def save(self, *a, **kw):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr("app.core.qrgen.segno.make", lambda *a, **kw: Boom())
    with pytest.raises(QrError):
        render_qr(URL, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_segno_failure_is_wrapped_in_japanese(tmp_path: Path, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("segno internal")

    monkeypatch.setattr("app.core.qrgen.segno.make", boom)
    with pytest.raises(QrError, match="生成できませんでした"):
        render_qr(URL, tmp_path)


# ---------------------------------------------------------------- 後始末


def test_cleanup_removes_png(tmp_path: Path):
    art = render_qr(URL, tmp_path)
    cleanup_qr(art)
    assert not art.png_path.exists()


def test_cleanup_is_idempotent(tmp_path: Path):
    art = render_qr(URL, tmp_path)
    cleanup_qr(art)
    cleanup_qr(art)  # 2回目でも例外にしない
    assert not art.png_path.exists()


def test_cleanup_accepts_none():
    cleanup_qr(None)  # 例外にならないこと


def test_cleanup_survives_missing_directory(tmp_path: Path):
    art = render_qr(URL, tmp_path / "sub")
    for p in sorted((tmp_path / "sub").iterdir()):
        p.unlink()
    (tmp_path / "sub").rmdir()
    cleanup_qr(art)


def test_cleanup_survives_unexpected_object():
    cleanup_qr(object())  # type: ignore[arg-type]
