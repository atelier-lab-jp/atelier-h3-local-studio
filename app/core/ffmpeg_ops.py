"""FFmpeg処理（設計書 §7・§10.6・§10.7）。

- ffmpeg 実体は config の ffmpeg_path、空なら imageio-ffmpeg 同梱バイナリを使う。
- 連結は **PTS 正規化つき再エンコードのみ**（P5 で `-c copy` を config から排除）。
- 出力はすべて partial → 検証 → 昇格（fileops.promote）。
- imageio-ffmpeg には ffprobe が同梱されないため、duration 等の確認は
  ffmpeg デコード（-f null -）の解析で行う。
- サブプロセスはすべて引数リスト形式（シェル非経由）で起動する。
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from app.core.fileops import FileopsError, partial_path, promote, verify_png

log = logging.getLogger("atelier.ffmpeg")

#: 単一動画（生成1本・モック素材）の duration 許容差。P0 から変えていない実績値。
#: **連結には使わない**（連結は `concat_duration_tolerance_sec()` が本数から算出する）。
DURATION_TOLERANCE_SEC = 0.5

#: AAC の1フレームは 1024 サンプル固定。32kHz なら 1024/32000 = 32ms。
#: 音声の長さは必ずこの単位へ切り上がるため、各クリップの音声は映像より
#: 最大 32ms だけ長くなる（P5 実測で確認。`concat_duration_tolerance_sec` の根拠）。
AAC_FRAME_SAMPLES = 1024

#: 連結結果が「期待秒数 × この比率」を下回ったら、許容差の計算に関係なく拒否する。
#: 明らかな短縮・破損（フレーム落ち・途中で切れた MP4）を必ず弾くための下限。
CONCAT_MIN_DURATION_RATIO = 0.95

# ffmpeg 処理は直列実行（設計 §7 決定D5: 同時実行は最大1）
_lock = threading.Lock()


class FfmpegError(Exception):
    """FFmpeg 処理の失敗（日本語メッセージ）。"""


def resolve_ffmpeg(configured_path: str = "") -> str:
    """ffmpeg 実体パスを解決する。

    config の ffmpeg_path が指定されていればそれを使い、
    空なら imageio-ffmpeg 同梱の静的バイナリを使う（§21.1-1 で確定した方針）。
    """
    if configured_path:
        p = Path(configured_path)
        if not p.is_file():
            raise FfmpegError(
                f"config.toml の ffmpeg_path が見つかりません: {configured_path}"
            )
        return str(p)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise FfmpegError(
            f"imageio-ffmpeg の ffmpeg バイナリを解決できません（{e}）。"
            "scripts/setup.sh を実行して依存を導入してください"
        ) from e


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    with _lock:
        log.debug("ffmpeg 実行: %s", " ".join(args))
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as e:
            raise FfmpegError(f"ffmpeg がタイムアウトしました（{timeout}秒）") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise FfmpegError(
            "ffmpeg が異常終了しました（終了コード "
            f"{proc.returncode}）:\n" + "\n".join(tail)
        )
    return proc


def ffmpeg_version(ffmpeg: str) -> str:
    proc = _run([ffmpeg, "-version"], timeout=30)
    first_line = (proc.stdout or proc.stderr).strip().splitlines()[0]
    return first_line


@dataclass(frozen=True)
class ProbeResult:
    duration_sec: float | None
    frames: int | None
    has_video: bool
    has_audio: bool
    video_desc: str
    audio_desc: str


def decode_probe(ffmpeg: str, path: Path, timeout: int = 300) -> ProbeResult:
    """動画をフルデコードして読込可否・duration・フレーム数を確認する（§10.7 手順4）。"""
    proc = _run(
        [ffmpeg, "-nostdin", "-i", str(path), "-f", "null", "-"], timeout=timeout
    )
    stderr = proc.stderr or ""

    frames = None
    frame_matches = re.findall(r"frame=\s*(\d+)", stderr)
    if frame_matches:
        frames = int(frame_matches[-1])

    duration = None
    time_matches = re.findall(r"time=(\d+):(\d+):([\d.]+)", stderr)
    if time_matches:
        h, m, s = time_matches[-1]
        duration = int(h) * 3600 + int(m) * 60 + float(s)

    video_lines = [ln.strip() for ln in stderr.splitlines() if "Video:" in ln]
    audio_lines = [ln.strip() for ln in stderr.splitlines() if "Audio:" in ln]
    return ProbeResult(
        duration_sec=duration,
        frames=frames,
        has_video=bool(video_lines),
        has_audio=bool(audio_lines),
        video_desc=video_lines[0] if video_lines else "",
        audio_desc=audio_lines[0] if audio_lines else "",
    )


def video_validator(
    ffmpeg: str,
    expected_duration_sec: float | None,
    require_audio: bool = True,
    *,
    tolerance_sec: float | None = None,
):
    """§10.7 手順4 の動画検証を `fileops.promote()` へ渡せる形で返す。

    連結・モック生成・実機生成が同じ検証を使えるよう公開している。
    `tolerance_sec` を省略すると単一動画向けの `DURATION_TOLERANCE_SEC`（0.5秒）。
    連結は本数に応じた許容差が必要なので `concat_validator()` を使うこと。
    """
    tolerance = (
        DURATION_TOLERANCE_SEC if tolerance_sec is None else float(tolerance_sec)
    )

    def _validate(path: Path) -> None:
        probe = decode_probe(ffmpeg, path)
        if not probe.has_video:
            raise FileopsError(f"映像ストリームを読み取れません: {path.name}")
        if require_audio and not probe.has_audio:
            raise FileopsError(f"音声ストリームを読み取れません: {path.name}")
        if expected_duration_sec is not None:
            if probe.duration_sec is None:
                raise FileopsError(f"再生時間を確認できません: {path.name}")
            if abs(probe.duration_sec - expected_duration_sec) > tolerance:
                raise FileopsError(
                    f"再生時間が想定と一致しません: {path.name}"
                    f"（実測 {probe.duration_sec:.2f}s / 想定 {expected_duration_sec:.2f}s"
                    f" / 許容 ±{tolerance:.2f}s）"
                )

    return _validate


# ------------------------------------------------------- 連結結果の検証（P5・§10.6）
#
# 【なぜ本数に比例する許容差が要るのか（P5 実測で確定）】
#
# ffmpeg の concat フィルタは、各セグメントの終端を「そのセグメントに含まれる
# 全ストリームの終端の最大値」で決め、次のセグメントをそこから始める。
# ところが AAC は 1024 サンプル単位でしか書けないため、映像長が 1024 サンプルの
# 倍数にならないクリップでは **音声が映像より最大 1024/sample_rate 秒だけ長くなる**。
# その差のぶん映像側に隙間ができ、出力 fps を固定している（-r 24）ので
# **境界ごとに最大1フレームぶんの重複フレームが挿入される**。
# したがって誤差は本数ではなく**境界数 (n-1) に比例**する。
#
# 実測（mock_56.mp4 / mock_124.mp4 を 2・5・10・20 本連結。24fps・32kHz）:
#
#   素材    n   期待frame  実測frame  期待秒     実測秒     誤差
#   56f     2      112       112       4.6667    4.6400    -0.027
#   56f     5      280       280      11.6667   11.6400    -0.027
#   56f    10      560       560      23.3333   23.3200    -0.013
#   56f    20     1120      1120      46.6667   46.6500    -0.017
#   124f    2      248       248      10.3333   10.3300    -0.003
#   124f    5      620       621      25.8333   25.8700    +0.037
#   124f   10     1240      1243      51.6667   51.7900    +0.123
#   124f   20     2480      2487     103.3333  103.6200    +0.287
#   混在    2      180       180       7.5000    7.5000    +0.000
#   混在    5      416       416      17.3333   17.3300    -0.003
#   混在   10      900       901      37.5000   37.5400    +0.040
#   混在   20     1800      1803      75.0000   75.1200    +0.120
#
# 124f クリップの音声超過は実測 +0.0173秒/本（= 0.416フレーム/境界）で、
# 増えたフレーム数は 20本で +7 ＝ floor(19 × 0.416)。予測と一致する。
# 56f クリップは超過が +0.0027秒/本（0.064フレーム/境界）しかないため
# 20本でも増えない。**P4 実機2本（248フレーム・10.33秒・A/V skew +0.030秒）**
# もこの式に収まる（skew 0.030秒 = 0.72フレーム/境界 < 1フレーム）。
#
# 主検証はフレーム数（下限は厳密一致）、duration は補助という位置づけにする。
#
# **なぜ隙間を消さないのか**（相互レビューでの追試結果。誤解を残さないため明記する）:
# 各クリップの音声を映像長へ `atrim` してから連結すると、超過フレームは**完全に
# 消える**（実測: 124f×5=620・×10=1240・×20=2480 でいずれも +0）。つまり技術的に
# 不可能なのではない。それでもやらないのは、**生成された音声の末尾が1本あたり
# 最大 17ms 切れる**（20本で計 0.32 秒）ためで、設計書 §10.6.1-4「音声を不用意に
# 切らない」に従い、映像を1フレーム多く許容する側を選んでいる。
#
# **実機素材の値**（`data/outputs` を読み取って計測）: 現行 56f は音声が映像より
# +0.064 フレーム/境界、124f は +0.416 フレーム/境界。1境界あたりの許容
# 0.768 フレームに対し最大でも 0.416 なので、実機素材でも上界に収まる。
# なお下の表の 56f が「0増」なのは**モック素材の音声が映像より短い**（-0.704
# フレーム/境界）ためで、実機の 56f とは性質が逆である点に注意（試験素材のほうが
# 甘いので、56f 経路の回帰はこの表だけでは検出できない）。


def audio_quantum_sec(sample_rate: int) -> float:
    """AAC 1フレーム分の秒数（音声長はこの単位へ切り上がる）。"""
    rate = int(sample_rate)
    if rate <= 0:
        raise FfmpegError(f"audio_sample_rate は 1 以上を指定してください（指定: {sample_rate}）")
    return AAC_FRAME_SAMPLES / float(rate)


def concat_max_extra_frames(clips: int, fps: int, sample_rate: int) -> int:
    """連結で増えうる重複フレーム数の上限（境界数 (n-1) に比例）。

    1境界あたりの隙間は最大 `audio_quantum_sec` 秒。それを fps 倍してフレーム数へ
    直し、切り上げたものが上限になる（24fps・32kHz なら 0.768 フレーム/境界）。
    """
    boundaries = max(int(clips) - 1, 0)
    if boundaries == 0:
        return 0
    if int(fps) <= 0:
        raise FfmpegError(f"fps は 1 以上を指定してください（指定: {fps}）")
    return math.ceil(boundaries * audio_quantum_sec(sample_rate) * int(fps))


def concat_duration_tolerance_sec(clips: int, fps: int, sample_rate: int) -> float:
    """連結結果の duration 許容差（秒）。

    `DURATION_TOLERANCE_SEC`（単一動画の実績値）に、境界数に比例する項
    `(n-1) × audio_quantum_sec` を足すだけ。固定値は広げていない。
    24fps・32kHz なら 20本で 0.5 + 19×0.032 = 1.108秒（実測誤差 0.287秒の約3.9倍）。
    """
    boundaries = max(int(clips) - 1, 0)
    return DURATION_TOLERANCE_SEC + boundaries * audio_quantum_sec(sample_rate)


def concat_validator(
    ffmpeg: str,
    *,
    clips: int,
    expected_frames: int,
    fps: int,
    sample_rate: int,
    require_audio: bool = True,
    trimmed_frames: int = 0,
):
    """連結結果を1回のデコードで検証する（フレーム数が主・duration が補助）。

    - 映像・音声ストリームが読めること
    - **フレーム数 >= 期待値**（下回ったら短縮・破損。必ず拒否）
    - フレーム数 <= 期待値 + `concat_max_extra_frames()`（増えすぎも拒否）
    - duration が期待値の `CONCAT_MIN_DURATION_RATIO` 倍以上
    - |duration 誤差| <= `concat_duration_tolerance_sec()`
    """
    if not isinstance(expected_frames, int) or isinstance(expected_frames, bool):
        raise FfmpegError(f"expected_frames は整数で指定してください（指定: {expected_frames!r}）")
    if expected_frames <= 0:
        raise FfmpegError(f"expected_frames は 1 以上を指定してください（指定: {expected_frames}）")
    if int(fps) <= 0:
        raise FfmpegError(f"fps は 1 以上を指定してください（指定: {fps}）")

    # 重複フレームを除去した場合、その境界では映像が 1/fps 秒だけ短くなる。
    # 音声はそのままなので映像との差が AAC の量子化幅（32ms）を超え、ffmpeg が
    # 同じ枚数を埋め戻す。つまり「除去を依頼した枚数」は上限側では戻ってくるため、
    # 上限にだけ加算する（下限は除去後の枚数のまま＝短縮・破損は従来どおり拒否）。
    # 実測（124f×10本・全境界を除去指定）: 依頼9枚に対し実際の減少は2枚。
    max_extra = concat_max_extra_frames(clips, fps, sample_rate) + max(int(trimmed_frames), 0)
    tolerance = concat_duration_tolerance_sec(clips, fps, sample_rate)
    expected_sec = expected_frames / float(int(fps))
    min_sec = expected_sec * CONCAT_MIN_DURATION_RATIO

    def _validate(path: Path) -> None:
        probe = decode_probe(ffmpeg, path)
        if not probe.has_video:
            raise FileopsError(f"映像ストリームを読み取れません: {path.name}")
        if require_audio and not probe.has_audio:
            raise FileopsError(f"音声ストリームを読み取れません: {path.name}")

        if probe.frames is None:
            raise FileopsError(f"フレーム数を確認できません: {path.name}")
        if probe.frames < expected_frames:
            raise FileopsError(
                f"連結動画のフレーム数が足りません: {path.name}"
                f"（実測 {probe.frames} / 想定 {expected_frames}）"
            )
        if probe.frames > expected_frames + max_extra:
            raise FileopsError(
                f"連結動画のフレーム数が想定より多すぎます: {path.name}"
                f"（実測 {probe.frames} / 想定 {expected_frames}"
                f"＋許容 {max_extra}）"
            )

        if probe.duration_sec is None:
            raise FileopsError(f"再生時間を確認できません: {path.name}")
        if probe.duration_sec < min_sec:
            raise FileopsError(
                f"連結動画の再生時間が短すぎます: {path.name}"
                f"（実測 {probe.duration_sec:.2f}s / 想定 {expected_sec:.2f}s"
                f" / 下限 {min_sec:.2f}s）"
            )
        if abs(probe.duration_sec - expected_sec) > tolerance:
            raise FileopsError(
                f"連結動画の再生時間が想定と一致しません: {path.name}"
                f"（実測 {probe.duration_sec:.2f}s / 想定 {expected_sec:.2f}s"
                f" / 許容 ±{tolerance:.2f}s・{clips}本）"
            )

    return _validate


def make_mock_clip(
    ffmpeg: str,
    out_path: Path,
    num_frames: int,
    fps: int = 24,
    width: int = 576,
    height: int = 320,
    sample_rate: int = 32000,
) -> Path:
    """モック用の実再生可能な動画（H.264/yuv420p + AAC）を生成する（§16.3）。"""
    duration = num_frames / fps
    partial = partial_path(out_path)
    args = [
        ffmpeg, "-y", "-nostdin",
        "-f", "lavfi",
        "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration:.6f}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate={sample_rate}:duration={duration:.6f}",
        "-frames:v", str(num_frames),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", str(sample_rate), "-ac", "2",
        "-f", "mp4", str(partial),
    ]
    _run(args)
    return promote(partial, out_path, (video_validator(ffmpeg, duration),))


def extract_frame_exact(
    ffmpeg: str, video: Path, frame_index: int, out_png: Path
) -> Path:
    """指定フレーム番号（0始まり）を正確に PNG 抽出する（§10.1 v1.1改訂）。

    56f 動画は index 55、124f 動画は index 123。時刻ベース（-sseof等）は使わない。
    """
    if frame_index < 0:
        raise FfmpegError("frame_index は 0 以上を指定してください")
    partial = partial_path(out_png)
    args = [
        ffmpeg, "-y", "-nostdin",
        "-i", str(video),
        "-vf", f"select=eq(n\\,{frame_index})",
        "-frames:v", "1",
        "-f", "image2", "-c:v", "png",
        str(partial),
    ]
    _run(args)
    return promote(partial, out_png, (verify_png,))


@dataclass(frozen=True)
class FrameDiff:
    """2枚の PNG の画素差（§10.6.1 の重複フレーム判定用）。

    `mean_diff` / `max_diff` は 0〜255 スケール（RGB 全チャンネル）。
    サイズが違う場合は比較自体が成立しないため `same_size=False` とし、
    差分値には無限大を入れて**必ず「非一致」**になるようにする。
    """

    same_size: bool
    mean_diff: float
    max_diff: float
    identical: bool
    size_a: tuple[int, int]
    size_b: tuple[int, int]

    def matches(self, max_mean_diff: float, max_max_diff: float) -> bool:
        """閾値以下＝「同じ絵」とみなせるか（安全側: サイズ違いは常に False）。"""
        if not self.same_size:
            return False
        return self.mean_diff <= max_mean_diff and self.max_diff <= max_max_diff

    def describe(self) -> str:
        if not self.same_size:
            return f"サイズ不一致（{self.size_a} / {self.size_b}）"
        return f"平均差 {self.mean_diff:.3f} / 最大差 {self.max_diff:.0f}"


def compare_frames(png_a: Path, png_b: Path) -> FrameDiff:
    """2枚の PNG を RGB で比較する（§10.6.1-2）。

    **無条件にフレームを削除しないための唯一の判定材料**。呼び出し側は
    `FrameDiff.matches()` が True の境界だけを除去候補にすること。
    """
    try:
        from PIL import Image, ImageChops, ImageStat
    except Exception as e:  # pragma: no cover - 依存欠落時のみ
        raise FfmpegError(f"PIL を読み込めません（{e}）") from e

    def _open(path: Path):
        try:
            with Image.open(path) as img:
                return img.convert("RGB")
        except FileNotFoundError as e:
            raise FfmpegError(f"比較する画像が見つかりません: {path}") from e
        except Exception as e:
            raise FfmpegError(f"画像として開けません: {path}（{e}）") from e

    a = _open(Path(png_a))
    b = _open(Path(png_b))
    if a.size != b.size:
        return FrameDiff(
            same_size=False,
            mean_diff=float("inf"),
            max_diff=float("inf"),
            identical=False,
            size_a=a.size,
            size_b=b.size,
        )

    diff = ImageChops.difference(a, b)
    stat = ImageStat.Stat(diff)
    mean_diff = sum(stat.mean) / len(stat.mean)
    max_diff = float(max(hi for _lo, hi in diff.getextrema()))
    return FrameDiff(
        same_size=True,
        mean_diff=float(mean_diff),
        max_diff=max_diff,
        identical=max_diff == 0.0,
        size_a=a.size,
        size_b=b.size,
    )


def concat_reencode(
    ffmpeg: str,
    inputs: list[Path],
    out_path: Path,
    fps: int = 24,
    sample_rate: int = 32000,
    expected_duration_sec: float | None = None,
    timeout: int = 600,
    trim_first_frame_of: set[int] | None = None,
    trim_audio_with_video: bool = False,
    warnings_out: list[str] | None = None,
    expected_frames: int | None = None,
) -> Path:
    """既定の連結方式: 各入力の PTS を 0 起点に正規化し filter_complex concat で
    再エンコード連結する（§10.6 v1.1改訂。libx264/yuv420p/AAC・24fps維持）。
    **V1 で唯一の連結方式**（`concat.reencode` は config から排除済み。P5）。

    `expected_frames`（= 各入力のフレーム数の合計 − 除去枚数）を渡すと、
    昇格前の検証がフレーム数主体になる（P5・`concat_validator()`）。
    省略した場合は `expected_duration_sec` による従来の検証だけを行うが、
    許容差は本数に応じて `concat_duration_tolerance_sec()` で広げる。

    `trim_first_frame_of` に入力インデックスを渡すと、その入力の**映像だけ**
    先頭1フレームを落とす（§10.6.1 の重複フレーム除去）。**無条件除去は禁止**で、
    呼び出し側が `compare_frames()` の判定結果に基づいて渡した場合のみ効く。
    音声は既定で切らない（語頭欠けを避けるため。§10.6.1-4）。
    `trim_audio_with_video=True` にすると同じ 1/fps 秒を音声側からも削るが、
    A/V 同期と語頭欠けの影響を実素材で確認したうえでのみ使うこと。
    """
    n = len(inputs)
    if n < 2:
        raise FfmpegError("連結には2本以上の動画が必要です")
    for p in inputs:
        if not Path(p).is_file():
            raise FfmpegError(f"連結対象の動画が見つかりません: {p}")

    trim = set(trim_first_frame_of or ())
    for i in sorted(trim):
        if not isinstance(i, int) or isinstance(i, bool) or not (0 <= i < n):
            raise FfmpegError(
                f"重複フレーム除去の入力インデックスが範囲外です: {i}（入力{n}本）"
            )
    if fps <= 0:
        raise FfmpegError(f"fps は 1 以上を指定してください（指定: {fps}）")
    if expected_frames is not None:
        # 検証条件の不備は ffmpeg を走らせる前に弾く（無駄な再エンコードを避ける）
        if isinstance(expected_frames, bool) or not isinstance(expected_frames, int):
            raise FfmpegError(
                f"expected_frames は整数で指定してください（指定: {expected_frames!r}）"
            )
        if expected_frames <= 0:
            raise FfmpegError(
                f"expected_frames は 1 以上を指定してください（指定: {expected_frames}）"
            )
    frame_sec = 1.0 / fps

    parts: list[str] = []
    concat_inputs = ""
    for i in range(n):
        if i in trim:
            # trim で先頭1フレームを落としてから PTS を 0 起点へ正規化する
            parts.append(f"[{i}:v]trim=start_frame=1,setpts=PTS-STARTPTS[v{i}]")
        else:
            parts.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
        if i in trim and trim_audio_with_video:
            parts.append(
                f"[{i}:a]atrim=start={frame_sec:.6f},asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
        concat_inputs += f"[v{i}][a{i}]"
    filter_complex = (
        ";".join(parts) + f";{concat_inputs}concat=n={n}:v=1:a=1[v][a]"
    )

    if trim:
        log.info(
            "重複フレーム除去つき連結: 対象入力 %s（音声も切る: %s）",
            sorted(trim),
            trim_audio_with_video,
        )
    if trim and warnings_out is not None:
        target = "映像と音声" if trim_audio_with_video else "映像のみ"
        warnings_out.append(
            f"連結境界 {len(trim)} 箇所で重複フレームを除去しました"
            f"（除去 {len(trim)} フレーム・{target}・"
            f"対象クリップ {', '.join(str(i + 1) for i in sorted(trim))}本目）"
        )

    partial = partial_path(out_path)
    args: list[str] = [ffmpeg, "-y", "-nostdin"]
    for p in inputs:
        args += ["-i", str(p)]
    args += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-ar", str(sample_rate),
        "-f", "mp4", str(partial),
    ]
    _run(args, timeout=timeout)

    if expected_frames is not None:
        validator = concat_validator(
            ffmpeg,
            clips=n,
            expected_frames=expected_frames,
            fps=fps,
            sample_rate=sample_rate,
            trimmed_frames=len(trim_first_frame_of or ()),
        )
    else:
        validator = video_validator(
            ffmpeg,
            expected_duration_sec,
            tolerance_sec=concat_duration_tolerance_sec(n, fps, sample_rate),
        )
    return promote(partial, out_path, (validator,))


def concat_copy(
    ffmpeg: str,
    inputs: list[Path],
    out_path: Path,
    expected_duration_sec: float | None = None,
    timeout: int = 300,
) -> Path:
    """concat demuxer + `-c copy` による無再エンコード連結。

    **V1 のアプリからは到達しない**（P5 で `concat.reencode` を config から排除し、
    `ConcatService` は常に `concat_reencode()` を使う）。実機で Non-monotonic DTS
    警告が確認されており、タイムスタンプの安定性を優先したため。
    比較検証・将来の実験用に関数だけ残してある。**設定からは有効化できない。**
    """
    if len(inputs) < 2:
        raise FfmpegError("連結には2本以上の動画が必要です")
    list_path = out_path.parent / (out_path.name + ".concat.txt")
    lines = []
    for p in inputs:
        escaped = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    partial = partial_path(out_path)
    try:
        args = [
            ffmpeg, "-y", "-nostdin",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-f", "mp4", str(partial),
        ]
        _run(args, timeout=timeout)
        return promote(
            partial, out_path, (video_validator(ffmpeg, expected_duration_sec),)
        )
    finally:
        list_path.unlink(missing_ok=True)
