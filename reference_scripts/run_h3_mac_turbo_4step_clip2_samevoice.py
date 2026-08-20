# DiffSynth-Studio（Apache-2.0, https://github.com/modelscope/DiffSynth-Studio）の
# MiniMax-H3 利用例に基づく実証スクリプト。帰属の詳細は THIRD-PARTY-NOTICES.md。
import os

os.environ["MODELSCOPE_DOMAIN"] = "www.modelscope.ai"
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

import torch
from PIL import Image
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

continuation_frame = Image.open("minimax_h3_clip1_last.png").convert("RGB")

prompt = """
Continue directly from the supplied first frame.
The same cute small green dinosaur wizard, wearing exactly the same clothes and holding the same wooden staff, stands in the same magical atelier in front of the glowing green portal.
He looks toward the portal, raises his staff and says energetically in Japanese, using the same cute youthful boyish voice with a medium-high pitch:
<d>[Japanese] ポータルの先へ、出発だ！</d>
He turns around, runs energetically toward the portal and jumps into the swirling green light.
The camera follows him toward the portal as green and golden magical particles fill the room.
Continuous cinematic shot, consistent character design, smooth energetic motion.
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
    keyframes=[continuation_frame],
    keyframe_indices=[0],
)

write_video_audio(
    video=video,
    audio=audio,
    output_path="minimax_h3_clip2_samevoice_5sec.mp4",
    fps=24,
    audio_sample_rate=32000,
)

print("完成: minimax_h3_clip2_samevoice_5sec.mp4")
