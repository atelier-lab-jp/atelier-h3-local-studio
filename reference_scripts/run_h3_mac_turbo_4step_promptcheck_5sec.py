# DiffSynth-Studio（Apache-2.0, https://github.com/modelscope/DiffSynth-Studio）の
# MiniMax-H3 利用例に基づく実証スクリプト。帰属の詳細は THIRD-PARTY-NOTICES.md。
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

lora_path = "models/loras/minimax_h3_turbo_4step_ckpt500.safetensors"
print("Turbo LoRAを読み込んでいます…")
pipe.load_lora(pipe.dit, lora_path, alpha=1)

prompt = """
A cute small green dinosaur wizard works at a potion table inside his magical atelier.
He quickly turns toward the camera, smiles, and walks energetically into the center of the room.
First, he raises his wooden staff and green and golden particles burst outward.
Then, glowing runes spin around him while the candles flicker and the potion bottles shine brightly.
A brilliant green portal opens behind him with swirling magical wind and sparkling energy.
He faces the camera and says energetically in Japanese:
<d>[Japanese] よく来たな、勇者よ。ここは大魔導士のアトリエだ。さあ、黒魔法の実験を始めよう！</d>
After speaking, he spins the staff, points it toward the portal, and releases a bright magical wave.
Finally, the camera moves closer as he laughs proudly and the entire atelier glows green and gold.
Fast-paced cinematic animation, energetic character movement, dynamic camera motion, detailed fantasy lighting.
No subtitles, no captions, no watermark.
"""

print("Turbo LoRA・4ステップ・約10秒で動画を生成しています…")

video, audio = pipe(
    prompt=prompt,
    height=320,
    width=576,
    num_frames=124,
    num_inference_steps=4,
    seed=42,
)

write_video_audio(
    video=video,
    audio=audio,
    output_path="minimax_h3_mac_turbo_4step_promptcheck_5sec.mp4",
    fps=24,
    audio_sample_rate=32000,
)

print("完成: minimax_h3_mac_turbo_4step_promptcheck_5sec.mp4")
