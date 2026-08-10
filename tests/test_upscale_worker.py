"""1080p高品質化ワーカーの試験（P6・設計書 §26）。

**モデルの読込・推論は行わない。** 重い部分（torch / MPS）に触れずに済む
純粋な部分だけを見る:
- 出力サイズの決め方（引き伸ばさず、左右を均等に切って 1920×1080）
- 進捗・結果の1行フォーマット
- 引数の受け取りと、危ない状況（出力先が既にある等）の拒否
- `app.*` に依存していないこと（既存 venv の Python で動かすため）

実機での通し確認は `scripts/` ではなく手動の検証で行い、ここには含めない。
"""

from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from app.postprocess import upscale_worker as w

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = PROJECT_ROOT / "app" / "postprocess" / "upscale_worker.py"


# ============================================================ 出力サイズ


def np_image(width: int, height: int):
    np = pytest.importorskip("numpy")
    return np.zeros((height, width, 3), dtype=np.uint8)


def test_x4_output_becomes_exactly_1920x1080():
    """576×320 を x4 した 2304×1280 → 高さ1080 → 左右を切って 1920×1080。"""
    pytest.importorskip("PIL")
    result = w.to_1080p_center_crop(np_image(2304, 1280))
    assert (result.width, result.height) == (w.OUT_WIDTH, w.OUT_HEIGHT)


def test_the_crop_is_centred_not_one_sided():
    """切り取りは**左右均等**（片側だけ切って構図をずらさない）。"""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")

    # 中央に縦帯を置き、切り取り後も中央に残ることを見る
    array = np.zeros((1280, 2304, 3), dtype=np.uint8)
    array[:, 1140:1164] = 255

    result = np.asarray(w.to_1080p_center_crop(array))
    columns = result.max(axis=(0, 2))
    bright = np.flatnonzero(columns > 128)
    assert bright.size, "目印の帯が消えている"
    centre = (bright[0] + bright[-1]) / 2
    assert abs(centre - w.OUT_WIDTH / 2) <= 12, f"中央からずれている: {centre}"


def test_aspect_ratio_is_preserved_before_cropping():
    """縦横比を変えない（引き伸ばさない）。正方形なら高さ基準で 1080×1080 相当。"""
    pytest.importorskip("PIL")
    with pytest.raises(w.UpscaleWorkerError, match="幅が足りません"):
        # 1:1 を高さ1080に合わせると 1080×1080 で、1920 に足りない
        w.to_1080p_center_crop(np_image(1024, 1024))


def test_a_wider_source_is_cropped_more():
    """横長すぎる素材でも 1920×1080 に収まる（上下を足さない）。"""
    pytest.importorskip("PIL")
    result = w.to_1080p_center_crop(np_image(4096, 1280))
    assert (result.width, result.height) == (w.OUT_WIDTH, w.OUT_HEIGHT)


# ============================================================ 出力フォーマット


def test_progress_and_result_lines_are_single_line_json():
    """上位層が1行ずつ解析できる形で出す（改行を挟まない）。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        w.emit(w.PROGRESS_PREFIX, {"frame": 3, "total": 124})
        w.emit(w.RESULT_PREFIX, {"ok": True})

    lines = buffer.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(w.PROGRESS_PREFIX)
    assert json.loads(lines[0][len(w.PROGRESS_PREFIX):]) == {"frame": 3, "total": 124}
    assert json.loads(lines[1][len(w.RESULT_PREFIX):]) == {"ok": True}


def test_japanese_messages_are_not_escaped():
    """日本語をそのまま出す（ログで読めるように）。"""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        w.emit(w.RESULT_PREFIX, {"ok": False, "error": "モデルが見つかりません"})
    assert "モデルが見つかりません" in buffer.getvalue()


# ============================================================ 引数と拒否


def test_missing_source_is_refused_without_loading_the_model(tmp_path):
    """元の動画が無ければ、モデルを読む前に終了コード2で断る。"""
    code = w.main([
        "--source", str(tmp_path / "absent.mp4"),
        "--destination", str(tmp_path / "out.mp4"),
        "--weights", str(tmp_path / "w.pth"),
    ])
    assert code == 2


def test_existing_destination_is_refused(tmp_path):
    """書き出し先がすでにあるときは上書きしない（原子的昇格の前提を守る）。"""
    source = tmp_path / "src.mp4"
    source.write_bytes(b"x")
    destination = tmp_path / "out.mp4"
    destination.write_bytes(b"already here")

    code = w.main([
        "--source", str(source),
        "--destination", str(destination),
        "--weights", str(tmp_path / "w.pth"),
    ])
    assert code == 2
    assert destination.read_bytes() == b"already here", "既存ファイルを壊している"


def test_missing_model_is_reported_as_a_japanese_error(tmp_path):
    """重みが無いときは日本語のエラーにする（内部の例外文を出さない）。

    確認は torch を読み込む**前**に行うので、torch の無い環境でも通る。
    """
    with pytest.raises(w.UpscaleWorkerError, match="モデルファイルが見つかりません"):
        w.load_model(tmp_path / "absent.pth")


# ============================================================ 独立性


def test_the_worker_does_not_import_app_modules():
    """既存 venv の Python で動かすので、`app.*` に依存しない（§26.3）。"""
    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name == "app" or name.startswith("app.")]
    assert not offenders, f"ワーカーが app に依存しています: {offenders}"


def test_the_worker_runs_under_a_bare_python(tmp_path):
    """`--help` が通る＝構文とargparseだけで起動できる（重い import を先にしない）。"""
    result = subprocess.run(
        [sys.executable, str(WORKER_PATH), "--help"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    for flag in ("--source", "--destination", "--weights", "--expected-frames"):
        assert flag in result.stdout


def test_fixed_output_size_is_1920x1080():
    """V1 の再生互換のため出力は固定（設定で変えられるようにしない）。"""
    assert (w.OUT_WIDTH, w.OUT_HEIGHT) == (1920, 1080)
    assert w.UPSCALE_FACTOR == 4
