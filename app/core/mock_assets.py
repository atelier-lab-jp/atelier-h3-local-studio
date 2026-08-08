"""モック素材の生成（設計書 §16.3）。

`scripts/setup.sh` から `python -m app.core.mock_assets` として一度だけ実行する。
imageio-ffmpeg 同梱バイナリで、実機と同一コーデック構成
（H.264/yuv420p/AAC・576×320・24fps・32kHz）の短い動画と、
正確な最終フレーム PNG（index 55 / 123）を作る。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core import ffmpeg_ops

ASSET_SPECS = (("mock_56", 56), ("mock_124", 124))


def generate(assets_dir: Path, ffmpeg: str | None = None, force: bool = False) -> list[Path]:
    ffmpeg = ffmpeg or ffmpeg_ops.resolve_ffmpeg("")
    assets_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name, frames in ASSET_SPECS:
        mp4 = assets_dir / f"{name}.mp4"
        png = assets_dir / f"{name}_last.png"
        if force or not mp4.is_file():
            ffmpeg_ops.make_mock_clip(ffmpeg, mp4, frames)
            created.append(mp4)
        if force or not png.is_file():
            ffmpeg_ops.extract_frame_exact(ffmpeg, mp4, frames - 1, png)
            created.append(png)
    return created


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    assets_dir = project_root / "app" / "assets" / "mock"
    print("モック素材を生成しています（imageio-ffmpeg 同梱バイナリ使用）…")
    try:
        ffmpeg = ffmpeg_ops.resolve_ffmpeg("")
        print(f"  ffmpeg: {ffmpeg}")
        created = generate(assets_dir, ffmpeg=ffmpeg)
    except Exception as e:
        print(f"エラー: モック素材の生成に失敗しました: {e}")
        return 1
    if created:
        for p in created:
            print(f"  作成: {p.relative_to(project_root)}")
    else:
        print("  既に生成済みです（再生成する場合はファイルを削除して再実行）")
    print("モック素材の準備が完了しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
