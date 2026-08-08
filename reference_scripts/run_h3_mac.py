import os

os.environ["MODELSCOPE_DOMAIN"] = "www.modelscope.ai"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

import torch
from diffsynth.pipelines.minimax_h3_audio_video import (
    MiniMaxH3Pipeline,
    ModelConfig,
)
from diffsynth.utils.data.audio_video import write_video_audio


vram_config = {
    "offload_dtype": "disk",
    "offload_device": "disk",
    "onload_dtype": "disk",
    "onload_device": "disk",
    "preparing_dtype": "disk",
    "preparing_device": "disk",
    "computation_dtype": torch.bfloat16,
    "computation_device": "mps",
}

print("MiniMax-H3-NF4を読み込んでいます…")

pipe = MiniMaxH3Pipeline.from_pretrained(
    torch_dtype=torch.bfloat16,
    device="mps",
    model_configs=[
        ModelConfig(
            model_id="DiffSynth-Studio/MiniMax-H3-NF4",
            origin_file_pattern="minimax-h3-fl2va-nf4.safetensors",
            **vram_config,
        ),
        ModelConfig(
            model_id="DiffSynth-Studio/MiniMax-H3-NF4",
            origin_file_pattern="minimax-h3-text-encoder-nf4.safetensors",
            **vram_config,
        ),
        ModelConfig(
            model_id="DiffSynth-Studio/MiniMax-H3-NF4",
            origin_file_pattern="video_vae_nf4.safetensors",
            **vram_config,
        ),
        ModelConfig(
            model_id="DiffSynth-Studio/MiniMax-H3-NF4",
            origin_file_pattern="audio_vae_nf4.safetensors",
            **vram_config,
        ),
    ],
    processor_config=ModelConfig(
        model_id="MiniMax/MiniMax-H3",
        origin_file_pattern="FL2VA/processor/",
    ),
    vram_limit=0,
)

prompt = """
A cute small green dinosaur wizard stands inside a magical atelier.
He raises his wooden staff, sparkling green and golden particles swirl around him,
and he smiles proudly at the camera.
He says clearly in Japanese:
<d>[Japanese] ローカル生成、成功！</d>
Cinematic lighting, smooth natural motion, detailed animation.
No subtitles, no captions, no watermark.
"""

print("動画を生成しています…")

video, audio = pipe(
    prompt=prompt,
    height=320,
    width=576,
    num_frames=56,
    num_inference_steps=20,
    seed=42,
)

write_video_audio(
    video=video,
    audio=audio,
    output_path="minimax_h3_mac_test.mp4",
    fps=24,
    audio_sample_rate=32000,
)

print("完成: minimax_h3_mac_test.mp4")
