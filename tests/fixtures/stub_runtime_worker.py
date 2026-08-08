"""実 h3_worker.py を「DiffSynth ランタイムだけ差し替えて」起動するラッパ。

目的: **実モデルを使わずに、A のワーカー実装と B の RealEngine の
ワイヤプロトコルが本当に噛み合うか**を検証する（P2 の最大の統合リスク）。

差し替えるのは `load_runtime` / `build_pipeline` / `apply_lora` の3つだけで、
イベント出力・入力検証・進捗ラッパ・partial 保存・エラー分類・コマンドループ・
継続生成のキーフレーム検証／PIL 読込は **実装そのもの**が動く。

`ATELIER_TEST_PIPE_DUMP` を指定すると、pipe() が受け取った引数の要約
（`keyframes` / `keyframe_indices` を含む）を JSON Lines で書き出す。

`app.*` は import しない（ワーカーと同じ制約下で動かすため）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

WORKER_PATH = Path(os.environ["ATELIER_TEST_WORKER_PATH"]).resolve()
#: 正常系で「生成結果」として使う実 MP4（576×320・24fps・H.264/AAC・56フレーム）
MOCK_ASSETS = Path(os.environ["ATELIER_TEST_MOCK_ASSETS"]).resolve()
#: 生成1本あたりの疑似所要時間（秒）
FAKE_GENERATE_SEC = float(os.environ.get("ATELIER_TEST_GENERATE_SEC", "0.05"))
#: 生成時に送出する例外（分類テスト用）
FAKE_RAISE = os.environ.get("ATELIER_TEST_RAISE", "")
#: pipe() が受け取った引数の要約を書き出す先（継続生成のワイヤ検証用。JSON Lines）
PIPE_DUMP = os.environ.get("ATELIER_TEST_PIPE_DUMP", "")


def _load_worker():
    spec = importlib.util.spec_from_file_location("h3_worker_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker = _load_worker()


def _dump_pipe_call(kwargs: dict) -> None:
    """pipe() が受け取った引数の要約を JSON Lines で書き出す（テストから検証する）。

    継続生成では `keyframes=[PIL.Image]` と `keyframe_indices=[0]` が
    **実ワーカーのコードを通って**届くことを確認したい。画像そのものは運べないので、
    mode / size と枚数だけを記録する。
    """
    if not PIPE_DUMP:
        return
    keyframes = kwargs.get("keyframes")
    summary = {
        "keys": sorted(kwargs.keys()),
        "num_frames": kwargs.get("num_frames"),
        "num_inference_steps": kwargs.get("num_inference_steps"),
        "seed": kwargs.get("seed"),
        "has_keyframes": keyframes is not None,
        "keyframe_indices": kwargs.get("keyframe_indices"),
        "keyframes": [
            {
                "type": type(image).__name__,
                "mode": getattr(image, "mode", None),
                "size": list(getattr(image, "size", []) or []),
            }
            for image in (keyframes or [])
        ],
    }
    try:
        with open(PIPE_DUMP, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except OSError:
        pass


class _FakePipe:
    """`pipe(...)` の呼び出し契約だけを再現する（モデルは載せない）。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.dit = object()

    def __call__(self, **kwargs):
        import time

        from PIL import Image

        # kwargs をそのまま溜め込むとキーフレーム画像を掴み続けてしまうため、
        # 画像を含まないキー一覧だけを残す（ワーカーの解放処理を邪魔しない）。
        self.calls.append(sorted(kwargs.keys()))
        _dump_pipe_call(kwargs)
        steps = int(kwargs["num_inference_steps"])
        frames = int(kwargs["num_frames"])
        width = int(kwargs["width"])
        height = int(kwargs["height"])

        if FAKE_RAISE == "mps":
            raise RuntimeError("MPS backend error: Metal command buffer execution failed")
        if FAKE_RAISE == "oom":
            raise MemoryError("Invalid buffer size: cannot allocate")
        if FAKE_RAISE == "pipeline":
            raise ValueError("something went wrong inside the pipeline")

        # 実物と同じ形で progress_bar_cmd を1回だけ呼ぶ（denoise ループ相当）
        progress_bar_cmd = kwargs.get("progress_bar_cmd")
        timesteps = list(range(steps))  # len == num_inference_steps（実物と同じ性質）
        iterator = progress_bar_cmd(timesteps) if progress_bar_cmd else timesteps
        for _ in enumerate(iterator):
            time.sleep(FAKE_GENERATE_SEC / max(steps, 1))

        video = [Image.new("RGB", (width, height), (16, 96, 32)) for _ in range(frames)]
        audio = object()  # write_video_audio を差し替えるので中身は使わない
        return video, audio


class _FakeRuntime:
    """`load_runtime()` の戻り値を模す。torch は最小限のスタブ。"""

    class _MPS:
        @staticmethod
        def empty_cache() -> None:
            return None

    class _Torch:
        bfloat16 = "bfloat16"
        mps = None

    def __init__(self) -> None:
        self.torch = _FakeRuntime._Torch()
        self.torch.mps = _FakeRuntime._MPS()
        self.MiniMaxH3Pipeline = None
        self.ModelConfig = None
        self.write_video_audio = self._write_video_audio

    @staticmethod
    def _write_video_audio(video, audio, output_path, fps, audio_sample_rate):
        """実素材をコピーして「実際に再生できる MP4」を作る。

        ワーカーは PyAV の拡張子制約を回避するため `.tmp.mp4` へ書いてから
        partial へ os.replace する。その経路をそのまま通すため、
        ここでは渡された output_path（= .tmp.mp4）へコピーする。
        """
        source = MOCK_ASSETS / f"mock_{len(video)}.mp4"
        if not source.is_file():
            raise FileNotFoundError(f"モック素材がありません: {source}")
        shutil.copyfile(source, output_path)


def _fake_load_runtime():
    return _FakeRuntime()


def _fake_build_pipeline(runtime, config):
    return _FakePipe()


def _fake_apply_lora(pipe, config):
    return None


worker.load_runtime = _fake_load_runtime
worker.build_pipeline = _fake_build_pipeline
worker.apply_lora = _fake_apply_lora

if __name__ == "__main__":
    sys.exit(worker.main())
