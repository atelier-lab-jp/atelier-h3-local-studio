# THIRD-PARTY-NOTICES — 第三者ソフトウェア・モデルの帰属とライセンス情報

本リポジトリ（ATELIER H3 Local Studio）のコードは
**Apache License 2.0（Copyright 2026 ATELIER LAB）** で提供する（`LICENSE` 参照）。

本書は、本リポジトリが由来コードを含む第三者ソフトウェアの帰属と、
本アプリが利用する外部モデル・weight のライセンス情報をまとめたものである。
記載は 2026-08-21 時点の調査に基づく。**最新かつ正式な条件は必ず各配布元で確認すること。**

---

## 1. 本リポジトリに由来コードを含む第三者ソフトウェア

### 1.1 DiffSynth-Studio

- ライセンス: **Apache License 2.0**（著作権表示は上流 `LICENSE` の記載による）
- 配布元: https://github.com/modelscope/DiffSynth-Studio
- 本リポジトリでの該当箇所:
  - `reference_scripts/*.py` — DiffSynth-Studio の MiniMax-H3 利用例に基づく実証スクリプト
  - `app/engine/backends/minimax_h3/h3_worker.py` — パイプライン呼び出し
    （`MiniMaxH3Pipeline` / `ModelConfig` / `write_video_audio` の用法）は同プロジェクトの利用例に基づく
- Apache License 2.0 の全文は本リポジトリの `LICENSE`（公式本文）と同一。

### 1.2 Real-ESRGAN

- ライセンス: **BSD 3-Clause License**（Copyright (c) 2021, Xintao Wang）
- 配布元: https://github.com/xinntao/Real-ESRGAN
- 本リポジトリでの該当箇所:
  - `app/postprocess/upscale_worker.py` — `SRVGGNetCompact` 互換のネットワーク定義。
    Real-ESRGAN（basicsr の `srvgg_arch` 互換）のアーキテクチャに基づき、
    既存 venv へパッケージを追加しないために必要部分のみ自己実装したもの
- モデル weight `realesr-animevideov3.pth` は本リポジトリに**同梱しない**。
  `./scripts/setup.sh --with-upscale` が同リポジトリの release v0.2.5.0 から取得し、
  SHA-256 で検証する（`scripts/setup.sh` / `app/core/preflight.py`）。

Real-ESRGAN のライセンス全文（原文のまま転載）:

```text
BSD 3-Clause License

Copyright (c) 2021, Xintao Wang
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

---

## 2. モデル・weight について（本リポジトリには同梱しない）

**本リポジトリは、いかなるモデル weight も同梱・再配布しない。**
**本リポジトリの Apache License 2.0 は、利用者が別途取得するモデル weight には適用されない。**
各モデルの利用可否・条件は各配布元のライセンスに従う。以下は情報提供であり、
法的助言ではない。**利用者は各配布元で最新のライセンス条文を自身で確認する必要がある。**

### 2.1 MiniMax H3（ベースモデル）

- 配布元（例）:
  - ModelScope: https://www.modelscope.cn/models/MiniMax/MiniMax-H3
  - Hugging Face: https://huggingface.co/MiniMaxAI/MiniMax-H3
- ライセンス: **MiniMax H3 Community License Agreement（独自ライセンス。`license: other`）**
- 2026-08 時点の同ライセンス条文には、**地域に関する制限**（ライセンス許諾地域から
  EU・英国・韓国・米国が除外される旨の定め）や、**商用利用に関する条件**
  （一定規模超の商用利用に対する別途許諾、製品表示に関する義務等）、
  再配布時の条件（契約書の提供・NOTICE 文言等）が含まれる。
  適用範囲・最新条件は必ず配布元の条文本文で確認すること。

### 2.2 MiniMax-H3-NF4（NF4 量子化版。本アプリが既定で利用）

- 配布元: ModelScope: https://www.modelscope.cn/models/DiffSynth-Studio/MiniMax-H3-NF4
- 配布ページの表記: **Apache License 2.0**（`base_model: MiniMax/MiniMax-H3`, `base_model_relation: quantized`）
- 注意: 本モデルはベースモデル MiniMax H3 の量子化版である。配布ページの
  Apache-2.0 表記と、ベースモデルの Community License（派生モデルの扱いに関する定めを含む）
  との関係は、**利用者自身が両方の条文を確認して判断する必要がある。**

### 2.3 MiniMax-H3 Turbo LoRA（少ステップ生成用 LoRA。本アプリが既定で利用）

- 配布元: Hugging Face: https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora
- 対象ファイル: `minimax_h3_turbo_4step_ckpt500.safetensors`
  （SHA-256: `82d0acff583b04ad9a4238a7440b584b56094bfb7c4fdb2981f67c7a4784b62d` で同定）
- 配布ページの表記: **apache-2.0**
- 注意: 本 LoRA は MiniMax H3 に対して学習されたモデル派生物である。配布ページの
  Apache-2.0 表記と、ベースモデルの Community License との関係は、
  **利用者自身が両方の条文を確認して判断する必要がある。**

### 2.4 realesr-animevideov3（1080p 高品質化。任意機能）

- 配布元: https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0 （§1.2 参照）
- リポジトリのライセンス: BSD 3-Clause License

### 2.5 補足（情報提供）

- MiniMax H3 の配布物（processor）には Qwen 系のトークナイザ構成
  （`Qwen2Tokenizer` / `Qwen3VLProcessor` 等の宣言）が含まれる。
  本リポジトリはこれらを同梱しないが、モデル取得時にはその配布条件も配布元で確認すること。

---

## 3. その他の依存パッケージ（情報提供）

以下は pip / uv 経由で利用者環境に取得されるものであり、本リポジトリにコードを同梱しない。
ライセンスは各配布物の記載（ローカル環境の dist-info メタデータで確認した値）:

| パッケージ | ライセンス表記 |
|---|---|
| gradio | Apache-2.0 |
| pillow | MIT-CMU |
| segno | BSD 系（配布物の LICENSE 記載による） |
| imageio-ffmpeg | BSD-2-Clause |
| pytest（開発用） | MIT |

DiffSynth-Studio の venv 側依存（torch / diffsynth / av / numpy 等）は
本リポジトリの管理外であり、各配布元のライセンスに従う。
