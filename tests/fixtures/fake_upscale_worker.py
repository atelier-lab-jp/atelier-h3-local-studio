"""1080p高品質化ワーカーの偽物（P6のテスト用）。

**本物のモデルも MPS も使わない。** `upscale_worker.py` と同じ約束
（引数・`@@PROGRESS` / `@@RESULT` の行・終了コード）だけを守り、中身は
ffmpeg で 1920×1080 の映像を作る。UpscaleService の配線・進捗・取消・
検証・原子的昇格を、実行時間 1 秒未満で確かめられるようにするためのもの。

**本物と同じく、書き出す枚数は入力を実際に数えて決める**（2026-09-18 改訂）。
以前は `--expected-frames` をそのまま鵜呑みにしていたため、「連結動画の実フレーム数が
台帳の名目値より多い」という本物のワーカーの失敗（実測 745 / 想定 744）を
テストで再現できなかった。数えた枚数が `--expected-frames` と食い違えば、
本物のワーカーと同じ文言・同じ終了コードで失敗する（自己検査も本物と同じ）。

環境変数で振る舞いを変えられる（テストが失敗経路を作るのに使う）:
  FAKE_UPSCALE_FAIL=1        ワーカー内エラーとして失敗する（終了コード2）
  FAKE_UPSCALE_SLEEP=秒      1フレームごとに待つ（取消のテスト用）
  FAKE_UPSCALE_FRAMES=n      入力を数えずこの枚数を書き、**成功と報告する**
                             （自己検査をすり抜けた想定＝サービス側検証のテスト用）
  FAKE_UPSCALE_SIZE=WxH      1920x1080 以外を作る（検証が弾くことの確認用）
  FAKE_UPSCALE_WITH_AUDIO=1  映像だけでなく音声も入れる（音声を二重にしない確認）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROGRESS_PREFIX = "@@PROGRESS "
RESULT_PREFIX = "@@RESULT "


def emit(prefix: str, payload: dict) -> None:
    sys.stdout.write(prefix + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def ffmpeg_binary() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - 実行環境依存
        return "ffmpeg"


def count_source_frames(source: Path) -> int | None:
    """入力の実フレーム数を数える（fps 変換なしのデコード計数）。

    本物のワーカーは PyAV の `container.decode(video=0)` で数える。ここでは
    同じ意味の値を ffmpeg のフルデコード（`-f null -`）で得る（av 非依存のまま）。
    """
    proc = subprocess.run(
        [ffmpeg_binary(), "-nostdin", "-i", str(source), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    matches = re.findall(r"frame=\s*(\d+)", proc.stderr or "")
    return int(matches[-1]) if matches else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--expected-frames", type=int, default=0)
    args = parser.parse_args(argv)

    source = Path(args.source)
    destination = Path(args.destination)

    if not source.is_file():
        emit(RESULT_PREFIX, {"ok": False, "error": f"元の動画が見つかりません: {source}"})
        return 2
    if destination.exists():
        emit(RESULT_PREFIX, {"ok": False, "error": "書き出し先がすでに存在します"})
        return 2

    override = os.environ.get("FAKE_UPSCALE_FRAMES", "")
    if override:
        # 意図的に嘘をつくモード: 入力を数えず、自己検査もせず「成功」と報告する
        frames = int(override)
        honest = False
    else:
        counted = count_source_frames(source)
        if not counted:
            # 本物のワーカーが読めない入力に出す文言と揃える
            emit(
                RESULT_PREFIX,
                {"ok": False, "error": "元の動画からフレームを1枚も読み取れませんでした"},
            )
            return 2
        frames = counted
        honest = True

    delay = float(os.environ.get("FAKE_UPSCALE_SLEEP", "") or 0)
    size = os.environ.get("FAKE_UPSCALE_SIZE", "") or "1920x1080"

    for i in range(1, frames + 1):
        if delay:
            time.sleep(delay)
        emit(PROGRESS_PREFIX, {"frame": i, "total": args.expected_frames or 0})

    if os.environ.get("FAKE_UPSCALE_FAIL"):
        emit(RESULT_PREFIX, {"ok": False, "error": "テスト用の失敗です"})
        return 2

    # `-t` ではなくフレーム数で切る（音声の量子化で長さがぶれないように）
    fps = 24
    cmd = [
        ffmpeg_binary(), "-y", "-nostdin",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
        "-frames:v", str(frames),
    ]
    if os.environ.get("FAKE_UPSCALE_WITH_AUDIO"):
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(destination)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not destination.is_file():
        tail = (result.stderr or "").strip().splitlines()
        emit(RESULT_PREFIX, {"ok": False, "error": tail[-1] if tail else "ffmpeg に失敗"})
        return 3

    # 本物のワーカーと同じ位置（書き出し完了後）・同じ文言の自己検査
    if honest and args.expected_frames and frames != args.expected_frames:
        emit(
            RESULT_PREFIX,
            {
                "ok": False,
                "error": (
                    f"フレーム数が想定と違います"
                    f"（実測 {frames} / 想定 {args.expected_frames}）"
                ),
            },
        )
        return 2

    width, _, height = size.partition("x")
    emit(
        RESULT_PREFIX,
        {
            "ok": True,
            "frames": frames,
            "width": int(width),
            "height": int(height),
            "elapsed_sec": 0.0,
            "max_rss_gb": 0.0,
            "device": "fake",
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
