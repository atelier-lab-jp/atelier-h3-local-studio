"""接続用QRコードの生成（P5契約 §2）。

`segno`（純Python・依存ゼロ）で**完全ローカル**に生成する。
外部のQRサービス・API・CDN は一切使わない。

**符号化するのは URL だけ。PIN は絶対に入れない。**
（QRは写真に撮られたり肩越しに見られたりする。PIN は別に手入力させる）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import segno

# ターミナルのテーマ（明るい/暗い）に関係なく読み取れるよう、
# 前景=白・背景=黒を ANSI で固定する。
# segno の compact 出力は「明るいモジュール」を █ で描くため、
# 前景を白にすると 明るい=白 / 暗い=黒 となり QR の極性が正しくなる。
_ANSI_ON = "\x1b[38;5;231;48;5;16m"
_ANSI_OFF = "\x1b[0m"

QR_FILENAME = "lan_qr.png"

# QR の内容が想定外に大きくならないための上限（URL 以外を入れさせない）
MAX_URL_LENGTH = 512


class QrError(Exception):
    """QRコードを生成できなかった（日本語メッセージ）。"""


@dataclass(frozen=True)
class QrArtifact:
    png_path: Path
    ascii_art: str  # ターミナル表示用（半角ブロック文字）
    url: str


def _validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise QrError("QRコードにするURLが文字列ではありません")
    text = url.strip()
    if not text:
        raise QrError("QRコードにするURLが指定されていません")
    if not text.startswith("http://") and not text.startswith("https://"):
        raise QrError(f"QRコードにできるのは http/https のURLだけです: {text[:60]}")
    if len(text) > MAX_URL_LENGTH:
        raise QrError("QRコードにするURLが長すぎます")
    if any(ch.isspace() for ch in text):
        raise QrError("QRコードにするURLに空白が含まれています")
    # QR は写真に撮られ、肩越しに見られ、スクリーンショットが共有されうる。
    # 接続先だけを載せ、認証情報が紛れ込む形（クエリ・フラグメント・userinfo）は
    # 関数自身が拒否する（設計書 §15.1.4「PIN を QR に入れない」の担保）。
    for ch, why in (("?", "クエリ"), ("#", "フラグメント"), ("@", "ユーザー情報")):
        if ch in text:
            raise QrError(
                f"QRコードにするURLに{why}（{ch}）は含められません"
                "（PINなどの認証情報を載せないためです）"
            )
    return text


def _terminal_art(qr, *, border: int = 4, ansi_color: bool = True) -> str:
    """ターミナル表示用のQR。余白（クワイエットゾーン）は規格どおり4モジュール。

    ISO/IEC 18004 は周囲4モジュールの余白を必須としている。2に削ると画面上は
    小さく収まるが、iPhone のカメラが読み取れないことがある。
    """
    import io

    buf = io.StringIO()
    qr.terminal(buf, compact=True, border=border)
    raw = buf.getvalue().rstrip("\n")
    if not ansi_color:
        return raw
    return "\n".join(f"{_ANSI_ON}{line}{_ANSI_OFF}" for line in raw.splitlines())


def render_qr(
    url: str,
    out_dir: Path,
    *,
    scale: int = 8,
    ansi_color: bool = True,
) -> QrArtifact:
    """URL の QR を生成する。PNG は 0o600（本人だけが読める権限）で保存。

    保存は `.partial` → 検証 → `os.replace()` の原子的昇格（設計書 §10.7）。
    """
    target_url = _validate_url(url)
    directory = Path(out_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise QrError(f"QRコードの保存先を作成できません: {directory}（{e}）") from e

    try:
        qr = segno.make(target_url, error="m")
    except Exception as e:  # segno 側の想定外
        raise QrError(f"QRコードを生成できませんでした: {e}") from e

    png_path = directory / QR_FILENAME
    partial = directory / (QR_FILENAME + ".partial")
    try:
        # 生成の瞬間から 0o600。umask に左右されないよう chmod でも念押しする
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            qr.save(fh, kind="png", scale=scale, border=4)
        os.chmod(partial, 0o600)
        if partial.stat().st_size <= 0:
            raise QrError("QRコード画像が空でした")
        os.replace(partial, png_path)
        os.chmod(png_path, 0o600)
    except QrError:
        _silent_unlink(partial)
        raise
    except OSError as e:
        _silent_unlink(partial)
        raise QrError(f"QRコード画像を保存できません: {png_path}（{e}）") from e

    return QrArtifact(
        png_path=png_path,
        ascii_art=_terminal_art(qr, ansi_color=ansi_color),
        url=target_url,
    )


def _silent_unlink(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_qr(artifact: QrArtifact | None) -> None:
    """正常終了時に QR の PNG を削除する。無くてもエラーにしない。"""
    if artifact is None:
        return
    path = getattr(artifact, "png_path", None)
    if path is None:
        return
    _silent_unlink(Path(path))
