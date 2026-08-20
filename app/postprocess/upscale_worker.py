"""1080p高品質化のワーカー（P6・設計書 §26）。

**DiffSynth-Studio の既存 venv の Python で、1ジョブごとに一発起動される。**
h3_worker.py と同じ考え方で `app.*` に一切依存せず、既存 venv に入っている
torch / av / PIL / numpy と標準ライブラリだけを使う（どちらの venv にも
パッケージを追加しない）。

処理（PoC で実測した手順をそのまま移植する）:
  1. PyAV で1フレームずつデコードして RGB へ
  2. MPS 上の realesr-animevideov3（x4）で推論
  3. アスペクト比を保ったまま高さ1080へ Lanczos 縮小（576×320 なら 1944×1080）
  4. 左右を中央から均等にクロップして 1920×1080
  5. **映像だけ**を一時ファイルへ書く（音声は上位層が stream copy で足す）

進捗は stdout へ `@@PROGRESS {"frame": n, "total": m}` の1行で出す。
それ以外の出力（ライブラリの警告など）はログ扱いで、進捗と混ざらない。

常駐しない理由: モデル読込は実測 0.04 秒で、常駐させる利点が無いため。
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path

#: 進捗行の接頭辞（上位層はこの行だけを解析する）
PROGRESS_PREFIX = "@@PROGRESS "
#: 完了時のまとめ行
RESULT_PREFIX = "@@RESULT "

#: 出力の固定仕様（V1 の再生互換に合わせる）
OUT_WIDTH = 1920
OUT_HEIGHT = 1080
UPSCALE_FACTOR = 4


class UpscaleWorkerError(Exception):
    """ワーカー内で処理を続けられない（日本語メッセージ）。"""


# ------------------------------------------------------------ アーキテクチャ
#
# realesr-animevideov3 のネットワーク（basicsr の srvgg_arch と互換）。
# basicsr パッケージを既存 venv へ追加しないよう、必要な部分だけ自己実装する。
# アーキテクチャは Real-ESRGAN（BSD-3-Clause, Copyright (c) 2021, Xintao Wang,
# https://github.com/xinntao/Real-ESRGAN）の SRVGGNetCompact に基づく。
# ライセンス全文と帰属の詳細は THIRD-PARTY-NOTICES.md。


def _build_model(torch, nn, F):
    class SRVGGNetCompact(nn.Module):
        def __init__(self, num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4):
            super().__init__()
            self.upscale = upscale
            self.body = nn.ModuleList()
            self.body.append(nn.Conv2d(num_in_ch, num_feat, 3, 1, 1))
            self.body.append(nn.PReLU(num_parameters=num_feat))
            for _ in range(num_conv):
                self.body.append(nn.Conv2d(num_feat, num_feat, 3, 1, 1))
                self.body.append(nn.PReLU(num_parameters=num_feat))
            self.body.append(nn.Conv2d(num_feat, num_out_ch * upscale * upscale, 3, 1, 1))
            self.upsampler = nn.PixelShuffle(upscale)

        def forward(self, x):
            out = x
            for layer in self.body:
                out = layer(out)
            out = self.upsampler(out)
            base = F.interpolate(x, scale_factor=self.upscale, mode="nearest")
            return out + base

    return SRVGGNetCompact


def load_model(weights: Path):
    """公式重みを読み、MPS（無ければCPU）へ載せた評価モードのモデルを返す。"""
    # 重い import より先に確認する（無いと分かっているのに torch を読まない）
    if not weights.is_file():
        raise UpscaleWorkerError(f"モデルファイルが見つかりません: {weights}")

    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    checkpoint = torch.load(str(weights), map_location="cpu", weights_only=True)
    state = checkpoint.get("params", checkpoint)

    model = _build_model(torch, nn, F)(num_feat=64, num_conv=16, upscale=UPSCALE_FACTOR)
    try:
        model.load_state_dict(state, strict=True)
    except Exception as e:  # 重みが別モデルだった場合など
        raise UpscaleWorkerError(f"モデルを読み込めません（{e}）") from e
    # PoC で検証した精度は fp32。fp16 にはしない（品質と安定性を優先）
    model.eval().to(device=device, dtype=torch.float32)
    return model, device


def to_1080p_center_crop(array):
    """x4 出力（RGB uint8）を高さ1080へ縮小し、左右を**均等に**切って 1920×1080 に。"""
    from PIL import Image

    image = Image.fromarray(array)
    scaled_width = max(1, round(image.width * OUT_HEIGHT / image.height))
    image = image.resize((scaled_width, OUT_HEIGHT), Image.LANCZOS)
    if image.width < OUT_WIDTH:
        raise UpscaleWorkerError(
            f"拡大後の幅が足りません（{image.width}px < {OUT_WIDTH}px）"
        )
    left = (image.width - OUT_WIDTH) // 2
    return image.crop((left, 0, left + OUT_WIDTH, OUT_HEIGHT))


def emit(prefix: str, payload: dict) -> None:
    """1行の機械可読な出力（バッファに溜めず即座に届ける）。"""
    sys.stdout.write(prefix + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def upscale(
    *, source: Path, destination: Path, weights: Path, expected_frames: int | None
) -> dict:
    """映像だけを高品質化して `destination` へ書く（音声は付けない）。"""
    import av
    import numpy as np
    import torch

    model, device = load_model(weights)

    container = av.open(str(source))
    try:
        streams = container.streams.video
        if not streams:
            raise UpscaleWorkerError("元の動画に映像が入っていません")
        rate = streams[0].average_rate or streams[0].base_rate
        if rate is None:
            raise UpscaleWorkerError("元の動画のフレームレートを判定できません")

        out = av.open(str(destination), mode="w")
        try:
            stream = out.add_stream("libx264", rate=rate)
            stream.width, stream.height = OUT_WIDTH, OUT_HEIGHT
            stream.pix_fmt = "yuv420p"  # Safari / iPhone で再生できる形式
            stream.options = {"crf": "18", "preset": "medium"}

            processed = 0
            started = time.perf_counter()
            with torch.inference_mode():
                for frame in container.decode(video=0):
                    rgb = frame.to_ndarray(format="rgb24")
                    tensor = (
                        torch.from_numpy(rgb)
                        .to(device=device, dtype=torch.float32)
                        .div_(255.0)
                        .permute(2, 0, 1)
                        .unsqueeze(0)
                    )
                    result = model(tensor)
                    result = (
                        result.squeeze(0)
                        .permute(1, 2, 0)
                        .clamp_(0, 1)
                        .mul_(255.0)
                        .round_()
                        .to(dtype=torch.uint8)
                        .cpu()
                        .numpy()
                    )
                    # 1フレームぶんの中間結果はここで手放す（次のフレームへ持ち越さない）
                    del tensor

                    final = np.asarray(to_1080p_center_crop(result))
                    video_frame = av.VideoFrame.from_ndarray(final, format="rgb24")
                    for packet in stream.encode(video_frame):
                        out.mux(packet)

                    processed += 1
                    emit(
                        PROGRESS_PREFIX,
                        {"frame": processed, "total": expected_frames or 0},
                    )

            for packet in stream.encode():
                out.mux(packet)
        finally:
            out.close()
    finally:
        container.close()

    if processed == 0:
        raise UpscaleWorkerError("元の動画からフレームを1枚も読み取れませんでした")
    if expected_frames and processed != expected_frames:
        raise UpscaleWorkerError(
            f"フレーム数が想定と違います（実測 {processed} / 想定 {expected_frames}）"
        )

    elapsed = time.perf_counter() - started
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**3)
    return {
        "frames": processed,
        "width": OUT_WIDTH,
        "height": OUT_HEIGHT,
        "elapsed_sec": round(elapsed, 2),
        "max_rss_gb": round(peak_rss, 2),
        "device": device.type,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1080p高品質化ワーカー（P6）")
    parser.add_argument("--source", required=True, help="元の動画（読み取りのみ）")
    parser.add_argument("--destination", required=True, help="映像だけを書き出す先")
    parser.add_argument("--weights", required=True, help="realesr-animevideov3 の重み")
    parser.add_argument("--expected-frames", type=int, default=0)
    args = parser.parse_args(argv)

    source = Path(args.source)
    destination = Path(args.destination)
    weights = Path(args.weights)

    try:
        if not source.is_file():
            raise UpscaleWorkerError(f"元の動画が見つかりません: {source}")
        if destination.exists():
            # 正式名も途中結果も、上位層が用意した一時パスだけを使う
            raise UpscaleWorkerError(f"書き出し先がすでに存在します: {destination}")
        summary = upscale(
            source=source,
            destination=destination,
            weights=weights,
            expected_frames=args.expected_frames or None,
        )
    except UpscaleWorkerError as e:
        emit(RESULT_PREFIX, {"ok": False, "error": str(e)})
        return 2
    except Exception as e:  # 想定外は種類だけ添えて返す（内部パスは出さない）
        emit(RESULT_PREFIX, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return 3

    emit(RESULT_PREFIX, {"ok": True, **summary})
    return 0


if __name__ == "__main__":
    sys.exit(main())
