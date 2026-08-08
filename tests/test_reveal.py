"""Finder 表示のユニットテスト（設計書 §15、P4契約 §6）。

実際に Finder を開かないよう `runner` を注入してコマンドを記録する。
すべて `tmp_path` 上で完結させる（プロジェクトの `data/` には一切書き込まない）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.core.reveal import RevealError, reveal_in_finder


class RecordingRunner:
    """subprocess.run の代わり: 引数を記録して成功を返す。"""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=self.returncode,
            stdout="",
            stderr=self.stderr,
        )


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "outputs").mkdir(parents=True)
    (root / "concat").mkdir()
    return root


@pytest.fixture()
def video(data_root: Path) -> Path:
    p = data_root / "concat" / "c_v_20260807_101530_ab3f_2clips.mp4"
    p.write_bytes(b"fake mp4")
    return p


# ---------------------------------------------------------------- 正常系


def test_reveal_uses_argument_array_without_shell(video: Path, data_root: Path):
    runner = RecordingRunner()
    reveal_in_finder(video, data_root=data_root, runner=runner)

    assert len(runner.calls) == 1
    args, kwargs = runner.calls[0]
    argv = args[0]
    # 引数配列であること（文字列連結ではない）
    assert isinstance(argv, list)
    assert argv[:2] == ["open", "-R"]
    assert argv[2] == str(video.resolve())
    assert len(argv) == 3
    # shell=True を使わない（明示指定も、既定 True への依存もしない）
    assert "shell" not in kwargs or kwargs["shell"] is False


def test_reveal_accepts_relative_path_under_data_root(video: Path, data_root: Path):
    runner = RecordingRunner()
    relative = video.relative_to(data_root)
    reveal_in_finder(relative, data_root=data_root, runner=runner)
    assert runner.calls[0][0][0][2] == str(video.resolve())


def test_reveal_default_runner_is_subprocess_run():
    """既定の runner が subprocess.run であること（誤って shell 経由にしない）。"""
    import inspect

    sig = inspect.signature(reveal_in_finder)
    assert sig.parameters["runner"].default is subprocess.run


# ---------------------------------------------------------------- 境界


def test_reveal_rejects_path_outside_data_root(tmp_path: Path, data_root: Path):
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    runner = RecordingRunner()
    with pytest.raises(RevealError, match="データ領域の外"):
        reveal_in_finder(outside, data_root=data_root, runner=runner)
    assert runner.calls == []


def test_reveal_rejects_traversal(data_root: Path, tmp_path: Path):
    (tmp_path / "secret.mp4").write_bytes(b"x")
    runner = RecordingRunner()
    with pytest.raises(RevealError, match="データ領域の外"):
        reveal_in_finder("../secret.mp4", data_root=data_root, runner=runner)
    with pytest.raises(RevealError, match="データ領域の外"):
        reveal_in_finder("/etc/passwd", data_root=data_root, runner=runner)
    assert runner.calls == []


def test_reveal_rejects_missing_file(data_root: Path):
    runner = RecordingRunner()
    with pytest.raises(RevealError, match="見つかりません"):
        reveal_in_finder(data_root / "outputs" / "no_such.mp4", data_root=data_root, runner=runner)
    assert runner.calls == []


def test_reveal_rejects_directory(data_root: Path):
    runner = RecordingRunner()
    with pytest.raises(RevealError, match="ファイルではありません"):
        reveal_in_finder(data_root / "outputs", data_root=data_root, runner=runner)
    assert runner.calls == []


def test_reveal_rejects_empty(data_root: Path):
    runner = RecordingRunner()
    for value in (None, "", "   "):
        with pytest.raises(RevealError, match="指定されていません"):
            reveal_in_finder(value, data_root=data_root, runner=runner)
    assert runner.calls == []


def test_reveal_rejects_symlink_escaping_data_root(tmp_path: Path, data_root: Path):
    """data_root 内のシンボリックリンクで外へ出られないこと（resolve 後に判定）。"""
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    link = data_root / "outputs" / "link.mp4"
    link.symlink_to(outside)
    runner = RecordingRunner()
    with pytest.raises(RevealError, match="データ領域の外"):
        reveal_in_finder(link, data_root=data_root, runner=runner)
    assert runner.calls == []


# ---------------------------------------------------------------- 注入対策


@pytest.mark.parametrize(
    "name",
    [
        "c_v_1; rm -rf ~.mp4",
        "c_$(whoami).mp4",
        "c_`id`.mp4",
        "c_&& open -a Calculator.mp4",
        "c_'quoted' \"double\".mp4",
        "c_日本語 スペース.mp4",
        "c_|pipe>redirect.mp4",
    ],
)
def test_reveal_is_injection_safe(name: str, data_root: Path):
    """シェルメタ文字を含むファイル名でも、そのまま1引数として渡るだけ。"""
    target = data_root / "concat" / name
    target.write_bytes(b"x")
    runner = RecordingRunner()
    reveal_in_finder(target, data_root=data_root, runner=runner)

    argv = runner.calls[0][0][0]
    assert argv[0] == "open" and argv[1] == "-R"
    assert argv[2] == str(target.resolve())
    assert argv[2].endswith(name)
    # コマンド文字列に連結されていない（引数は常に3個）
    assert len(argv) == 3


# ---------------------------------------------------------------- 失敗系


def test_reveal_reports_nonzero_exit(video: Path, data_root: Path):
    runner = RecordingRunner(returncode=1, stderr="open: cannot open")
    with pytest.raises(RevealError, match="終了コード 1"):
        reveal_in_finder(video, data_root=data_root, runner=runner)


def test_reveal_reports_missing_open_command(video: Path, data_root: Path):
    def boom(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'open'")

    with pytest.raises(RevealError, match="macOS 以外"):
        reveal_in_finder(video, data_root=data_root, runner=boom)


def test_reveal_reports_os_error(video: Path, data_root: Path):
    def boom(*_a, **_kw):
        raise OSError(13, "Permission denied")

    with pytest.raises(RevealError, match="Finder を開けませんでした"):
        reveal_in_finder(video, data_root=data_root, runner=boom)
