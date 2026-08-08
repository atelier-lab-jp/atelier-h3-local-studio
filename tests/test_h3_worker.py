"""h3_worker.py のユニットテスト（実モデルを一切起動しない）。

このテストはアプリ venv（torch / diffsynth が入っていない）で動く。
`h3_worker.py` を importlib でファイルから直接読み込み、torch / diffsynth を
import せずに純粋関数を検証できることそのものを設計要件として確認する。
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

WORKER_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "engine"
    / "backends"
    / "minimax_h3"
    / "h3_worker.py"
)


def _load_worker_module():
    spec = importlib.util.spec_from_file_location("h3_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses は cls.__module__ を sys.modules から引くため、exec 前に登録する
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker = _load_worker_module()


# ---------------------------------------------------------------- 共通フィクスチャ


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "outputs").mkdir(parents=True)
    return root


@pytest.fixture()
def config(data_root: Path) -> object:
    return worker.WorkerConfig(
        data_root=data_root.resolve(),
        backend_id="minimax_h3",
        model_id="DiffSynth-Studio/MiniMax-H3-NF4",
        model_revision="nf4-turbo4step-ckpt500",
        processor_id="MiniMax/MiniMax-H3",
        lora_path=Path("/dev/null"),
        lora_alpha=1.0,
    )


def make_command(data_root: Path, **overrides) -> dict:
    """契約 §2 の正常な generate コマンドを作り、params を上書きできるようにする。"""
    outputs = data_root / "outputs"
    params = {
        "prompt": "緑の恐竜の魔法使い",
        "num_frames": 56,
        "num_inference_steps": 4,
        "seed": 42,
        "width": 576,
        "height": 320,
        "fps": 24,
        "audio_sample_rate": 32000,
        "keyframe_path": None,
        "output_partial_path": str(outputs / "v_test.mp4.partial"),
        "last_frame_partial_path": str(outputs / "v_test_last.png.partial"),
    }
    command = {
        "cmd": "generate",
        "job_id": "v_20260807_101530_ab3f",
        "backend_id": "minimax_h3",
        "params": params,
    }
    for key, value in overrides.items():
        if key in ("cmd", "job_id", "backend_id"):
            command[key] = value
        else:
            params[key] = value
    return command


def _write_png(path: Path, size: tuple[int, int] = (576, 320), mode: str = "RGB") -> Path:
    """継続生成のキーフレーム用に本物の PNG を書く（tmp_path 配下のみ）。"""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new(mode, size, (12, 34, 56) if mode == "RGB" else 128) as image:
        image.save(path, format="PNG")
    return path


def parse_events(stream: io.StringIO) -> list[dict]:
    events = []
    for line in stream.getvalue().splitlines():
        assert line.startswith(worker.EVENT_PREFIX)
        events.append(json.loads(line[len(worker.EVENT_PREFIX) :]))
    return events


# ---------------------------------------------------------------- 依存関係（設計要件）


def test_worker_module_does_not_import_torch_or_diffsynth():
    """torch / diffsynth をトップレベルで import していないこと（設計要件）。"""
    assert "torch" not in sys.modules
    assert "diffsynth" not in sys.modules


def test_worker_module_does_not_import_app_package():
    """アプリ側パッケージ（app.*）に依存しないこと。"""
    source = WORKER_PATH.read_text(encoding="utf-8")
    assert "import app." not in source
    assert "from app." not in source


def test_capabilities_matches_contract():
    assert worker.CAPABILITIES == {
        "audio": True,
        "continuation": True,
        "seed": True,
        "num_frames": [56, 124],
        "steps": [4, 8],
        "width": 576,
        "height": 320,
        "fps": 24,
        "last_frame_output": True,
        "references": {"image": False, "video": False, "audio": False},
    }


# ---------------------------------------------------------------- パラメータ検証


def test_validate_generate_command_accepts_valid_request(data_root, config):
    job = worker.validate_generate_command(make_command(data_root), config)
    assert job.job_id == "v_20260807_101530_ab3f"
    assert job.num_frames == 56
    assert job.num_inference_steps == 4
    assert job.seed == 42
    assert job.output_partial_path == (data_root / "outputs" / "v_test.mp4.partial").resolve()
    assert job.last_frame_partial_path == (
        data_root / "outputs" / "v_test_last.png.partial"
    ).resolve()


def test_validate_accepts_missing_keyframe_key(data_root, config):
    command = make_command(data_root)
    del command["params"]["keyframe_path"]
    job = worker.validate_generate_command(command, config)
    assert job.num_frames == 56


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t ", None, 123])
def test_validate_rejects_empty_prompt(data_root, config, prompt):
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(make_command(data_root, prompt=prompt), config)


@pytest.mark.parametrize("num_frames", [243, 55, 125, 0, -56, "56", 56.0, True])
def test_validate_rejects_disallowed_num_frames(data_root, config, num_frames):
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(
            make_command(data_root, num_frames=num_frames), config
        )


@pytest.mark.parametrize("steps", [5, 20, 0, 3, "4", 4.0, True])
def test_validate_rejects_disallowed_steps(data_root, config, steps):
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(
            make_command(data_root, num_inference_steps=steps), config
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 1280),
        ("width", 512),
        ("height", 720),
        ("height", 321),
        ("fps", 30),
        ("fps", 25),
        ("audio_sample_rate", 44100),
        ("audio_sample_rate", 16000),
    ],
)
def test_validate_rejects_non_v1_fixed_values(data_root, config, field, value):
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(make_command(data_root, **{field: value}), config)


@pytest.mark.parametrize("seed", [-1, 2147483648, 10**12, "42", 42.5, None])
def test_validate_rejects_out_of_range_seed(data_root, config, seed):
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(make_command(data_root, seed=seed), config)


@pytest.mark.parametrize("seed", [0, 1, 2147483647])
def test_validate_accepts_seed_bounds(data_root, config, seed):
    job = worker.validate_generate_command(make_command(data_root, seed=seed), config)
    assert job.seed == seed


def test_validate_accepts_keyframe_path(data_root, config):
    """継続生成（P4）: data_root 配下の実在ファイルなら受理して解決済みパスを返す。"""
    keyframe = _write_png(data_root / "outputs" / "prev_last.png")
    job = worker.validate_generate_command(
        make_command(data_root, keyframe_path=str(keyframe)), config
    )
    assert job.keyframe_path == keyframe.resolve()
    assert job.is_continuation is True


def test_validate_treats_null_keyframe_as_single_job(data_root, config):
    job = worker.validate_generate_command(make_command(data_root), config)
    assert job.keyframe_path is None
    assert job.is_continuation is False


@pytest.mark.parametrize("backend_id", ["other_backend", "", None, "MINIMAX_H3"])
def test_validate_rejects_backend_id_mismatch(data_root, config, backend_id):
    command = make_command(data_root)
    command["backend_id"] = backend_id
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(command, config)


@pytest.mark.parametrize("job_id", ["", "   ", None, 42])
def test_validate_rejects_bad_job_id(data_root, config, job_id):
    command = make_command(data_root)
    command["job_id"] = job_id
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(command, config)


@pytest.mark.parametrize("params", [None, "x", 42, []])
def test_validate_rejects_non_object_params(data_root, config, params):
    command = make_command(data_root)
    command["params"] = params
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(command, config)


def test_validate_rejects_identical_output_paths(data_root, config):
    same = str(data_root / "outputs" / "v_test.mp4.partial")
    command = make_command(
        data_root, output_partial_path=same, last_frame_partial_path=same
    )
    with pytest.raises(worker.InputValidationError):
        worker.validate_generate_command(command, config)


# ---------------------------------------------------------------- data_root 境界


def test_partial_path_inside_data_root_is_accepted(data_root):
    target = data_root / "outputs" / "v_ok.mp4.partial"
    resolved = worker.validate_partial_path(str(target), data_root, "output_partial_path")
    assert resolved == target.resolve()


def test_partial_path_outside_data_root_is_rejected(data_root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(
            str(outside / "v.mp4.partial"), data_root, "output_partial_path"
        )


def test_partial_path_with_parent_escape_is_rejected(data_root):
    escape = data_root / "outputs" / ".." / ".." / "escaped.mp4.partial"
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(str(escape), data_root, "output_partial_path")


def test_partial_path_via_symlink_outside_root_is_rejected(data_root, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    link = data_root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 環境依存
        pytest.skip("シンボリックリンクを作成できない環境")
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(
            str(link / "v.mp4.partial"), data_root, "output_partial_path"
        )


@pytest.mark.parametrize("name", ["v.mp4", "v.png", "v.partial.mp4", "v.PARTIAL", "v"])
def test_partial_path_requires_partial_suffix(data_root, name):
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(
            str(data_root / "outputs" / name), data_root, "output_partial_path"
        )


def test_partial_path_requires_absolute_path(data_root):
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(
            "outputs/v.mp4.partial", data_root, "output_partial_path"
        )


@pytest.mark.parametrize("value", [None, "", "   ", 42, []])
def test_partial_path_requires_string(data_root, value):
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(value, data_root, "output_partial_path")


def test_partial_path_requires_existing_parent_directory(data_root):
    with pytest.raises(worker.InputValidationError):
        worker.validate_partial_path(
            str(data_root / "missing_dir" / "v.mp4.partial"),
            data_root,
            "output_partial_path",
        )


def test_partial_path_equal_to_data_root_is_rejected(data_root):
    assert worker.is_within_root(data_root, data_root) is False


# ---------------------------------------------------------------- キーフレーム（継続生成・P4）


def test_keyframe_none_means_single_generation(data_root):
    assert worker.validate_keyframe_path(None, data_root) is None


def test_keyframe_inside_data_root_is_accepted(data_root):
    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")
    assert worker.validate_keyframe_path(str(keyframe), data_root) == keyframe.resolve()


def test_keyframe_relative_path_is_rejected(data_root):
    with pytest.raises(worker.InputValidationError, match="絶対パス"):
        worker.validate_keyframe_path("outputs/v_parent_last.png", data_root)


def test_keyframe_outside_data_root_is_rejected(data_root, tmp_path):
    outside = _write_png(tmp_path / "elsewhere" / "stolen.png")
    with pytest.raises(worker.InputValidationError, match="データフォルダの外"):
        worker.validate_keyframe_path(str(outside), data_root)


def test_keyframe_parent_escape_is_rejected(data_root):
    escape = data_root / "outputs" / ".." / ".." / "escaped.png"
    with pytest.raises(worker.InputValidationError, match="データフォルダの外"):
        worker.validate_keyframe_path(str(escape), data_root)


def test_keyframe_via_symlink_outside_root_is_rejected(data_root, tmp_path):
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    _write_png(outside / "v.png")
    link = data_root / "linked_keyframes"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - 環境依存
        pytest.skip("シンボリックリンクを作成できない環境")
    with pytest.raises(worker.InputValidationError, match="データフォルダの外"):
        worker.validate_keyframe_path(str(link / "v.png"), data_root)


def test_keyframe_missing_file_is_rejected(data_root):
    with pytest.raises(worker.InputValidationError, match="見つかりません"):
        worker.validate_keyframe_path(
            str(data_root / "outputs" / "nope_last.png"), data_root
        )


def test_keyframe_directory_is_rejected(data_root):
    (data_root / "outputs" / "a_dir.png").mkdir()
    with pytest.raises(worker.InputValidationError, match="見つかりません"):
        worker.validate_keyframe_path(str(data_root / "outputs" / "a_dir.png"), data_root)


@pytest.mark.parametrize("value", ["", "   ", 42, [], {}])
def test_keyframe_non_string_is_rejected(data_root, value):
    with pytest.raises(worker.InputValidationError):
        worker.validate_keyframe_path(value, data_root)


def test_open_keyframe_image_returns_rgb(data_root):
    keyframe = _write_png(data_root / "outputs" / "gray_last.png", mode="L")
    image = worker.open_keyframe_image(keyframe)
    try:
        assert image.mode == "RGB"  # 実証スクリプトと同じ .convert("RGB")
        assert image.size == (576, 320)
    finally:
        worker.close_image(image)


def test_open_keyframe_image_rejects_non_png_content(data_root):
    broken = data_root / "outputs" / "not_an_image.png"
    broken.write_bytes("これは PNG ではありません\n".encode("utf-8") * 8)
    with pytest.raises(worker.InputValidationError, match="開けませんでした"):
        worker.open_keyframe_image(broken)


def test_open_keyframe_image_rejects_truncated_png(data_root):
    keyframe = _write_png(data_root / "outputs" / "truncated_last.png")
    data = keyframe.read_bytes()
    keyframe.write_bytes(data[: max(16, len(data) // 3)])  # 途中で切れた PNG
    with pytest.raises(worker.InputValidationError, match="開けませんでした"):
        worker.open_keyframe_image(keyframe)


# ------------------------------------------- P5: キーフレーム寸法・形式の再検証（§5.1）


def _write_image(path: Path, size=(576, 320), mode="RGB", fmt="PNG") -> Path:
    """任意の形式・寸法の画像を書く（tmp_path 配下のみ）。"""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new(mode, size, (12, 34, 56) if mode == "RGB" else 128) as image:
        image.save(path, format=fmt)
    return path


def test_open_keyframe_image_accepts_exact_576x320(data_root):
    keyframe = _write_image(data_root / "outputs" / "ok_last.png")
    image = worker.open_keyframe_image(keyframe)
    try:
        assert image.mode == "RGB"
        assert image.size == (576, 320)
    finally:
        worker.close_image(image)


@pytest.mark.parametrize(
    "size", [(575, 320), (577, 320), (576, 319), (576, 321), (320, 576), (1152, 640)]
)
def test_open_keyframe_image_rejects_wrong_size(data_root, size):
    """576×320 ちょうど以外は input エラー（拡大・縮小で救わない）。"""
    keyframe = _write_image(data_root / "outputs" / "wrong_size.png", size=size)
    with pytest.raises(worker.InputValidationError, match="大きさが違います") as excinfo:
        worker.open_keyframe_image(keyframe)
    assert "576×320 の画像が必要です" in str(excinfo.value)
    assert f"{size[0]}×{size[1]}" in str(excinfo.value)


@pytest.mark.parametrize("fmt", ["JPEG", "BMP", "GIF", "TIFF"])
def test_open_keyframe_image_rejects_non_png_format(data_root, fmt):
    """中身が別形式の画像は、拡張子が .png でも拒否する。"""
    keyframe = _write_image(data_root / "outputs" / "other_format.png", fmt=fmt)
    with pytest.raises(worker.InputValidationError, match="PNG ではありません"):
        worker.open_keyframe_image(keyframe)


def test_open_keyframe_image_uses_the_shared_japanese_label(data_root):
    """real / mock エンジンと同じ語彙（継続元のキーフレーム画像）で説明する。"""
    assert worker.KEYFRAME_LABEL == "継続元のキーフレーム画像"
    keyframe = _write_image(data_root / "outputs" / "small.png", size=(64, 64))
    with pytest.raises(worker.InputValidationError, match=worker.KEYFRAME_LABEL):
        worker.open_keyframe_image(keyframe)
    broken = data_root / "outputs" / "broken.png"
    broken.write_bytes(b"nope" * 8)
    with pytest.raises(worker.InputValidationError, match=worker.KEYFRAME_LABEL):
        worker.open_keyframe_image(broken)


def test_open_keyframe_image_respects_expected_size_argument(data_root):
    """検証する寸法は job の width / height に紐づく（定数を二重管理しない）。"""
    keyframe = _write_image(data_root / "outputs" / "tiny.png", size=(64, 64))
    image = worker.open_keyframe_image(keyframe, expected_size=(64, 64))
    try:
        assert image.size == (64, 64)
    finally:
        worker.close_image(image)


def test_open_keyframe_image_releases_image_when_rejected(data_root):
    """検証で弾いた画像を掴んだままにしない。"""
    import gc
    import weakref

    from PIL import Image

    keyframe = _write_image(data_root / "outputs" / "bad_size.png", size=(100, 100))
    holder = {}
    original_convert = Image.Image.convert

    def spy_convert(self, *args, **kwargs):
        converted = original_convert(self, *args, **kwargs)
        holder["ref"] = weakref.ref(converted)
        return converted

    Image.Image.convert = spy_convert
    try:
        with pytest.raises(worker.InputValidationError):
            worker.open_keyframe_image(keyframe)
    finally:
        Image.Image.convert = original_convert

    gc.collect()
    assert holder["ref"]() is None, "検証で弾いた画像が解放されていません"


@pytest.mark.parametrize(
    ("size", "expected"),
    [((640, 360), "大きさが違います"), ((576, 320), None)],
)
def test_handle_generate_rejects_wrong_size_keyframe_without_dying(
    data_root, config, size, expected
):
    """寸法違反は input / 非fatal。ワーカーは生存し、モデルを捨てない（契約 §5.1）。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    keyframe = _write_image(data_root / "outputs" / "kf_last.png", size=size)

    fatal = worker.handle_generate(
        _continuation_command(data_root, keyframe), pipe, FakeRuntime(), config, emitter
    )
    events = parse_events(stream)

    assert fatal is False
    if expected is None:
        assert events[-1]["type"] == "done"
        return
    error = [e for e in events if e["type"] == "error"][0]
    assert error["category"] == "input"      # pipeline / fatal へ化けさせない
    assert error["fatal"] is False
    assert expected in error["message"]
    assert pipe.seen == {}                   # pipe は呼ばない＝モデルを触らない
    assert [e["type"] for e in events] == ["stage", "error"]


def test_handle_generate_rejects_non_png_keyframe_without_dying(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    keyframe = _write_image(data_root / "outputs" / "jpeg_last.png", fmt="JPEG")

    fatal = worker.handle_generate(
        _continuation_command(data_root, keyframe), pipe, FakeRuntime(), config, emitter
    )
    error = [e for e in parse_events(stream) if e["type"] == "error"][0]

    assert fatal is False
    assert error["category"] == "input"
    assert error["fatal"] is False
    assert "PNG ではありません" in error["message"]
    assert pipe.seen == {}
    assert list((data_root / "outputs").iterdir()) == [keyframe]  # 成果物を作らない


def test_worker_survives_bad_keyframe_and_accepts_the_next_job(data_root, config):
    """不正キーフレームの後も同じワーカー（同じ pipe）で次のジョブを処理できる。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    bad = _write_image(data_root / "outputs" / "bad_last.png", size=(100, 100))
    good = _write_image(data_root / "outputs" / "good_last.png")

    assert (
        worker.handle_generate(
            _continuation_command(data_root, bad), pipe, FakeRuntime(), config, emitter
        )
        is False
    )
    assert pipe.seen == {}

    fatal = worker.handle_generate(
        _continuation_command(data_root, good), pipe, FakeRuntime(), config, emitter
    )
    assert fatal is False
    assert pipe.seen["keyframe_indices"] == [0]
    assert [e["type"] for e in parse_events(stream)][-1] == "done"


def test_close_image_is_safe_for_none_and_broken_objects():
    class Broken:
        def close(self):
            raise RuntimeError("close failed")

    worker.close_image(None)
    worker.close_image(Broken())  # 例外を投げない


# ---------------------------------------------------------------- イベント整形


def test_format_event_has_prefix_and_single_line():
    line = worker.format_event({"type": "stage", "stage": "loading_model"})
    assert line.startswith("@@EVT ")
    assert "\n" not in line
    assert json.loads(line[len("@@EVT ") :]) == {
        "type": "stage",
        "stage": "loading_model",
    }


def test_format_event_keeps_japanese_unescaped():
    line = worker.format_event({"type": "error", "message": "メモリ不足です"})
    assert "メモリ不足です" in line
    assert "\\u" not in line


def test_format_event_flattens_newlines():
    line = worker.format_event({"type": "error", "detail": "line1\nline2\r\nline3"})
    assert "\n" not in line
    payload = json.loads(line[len("@@EVT ") :])
    assert "line1" in payload["detail"]


def test_format_event_survives_unserializable_values():
    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    line = worker.format_event({"type": "done", "value": Weird()})
    payload = json.loads(line[len("@@EVT ") :])
    assert payload["type"] == "done"
    assert payload["value"] == "<weird>"


def test_format_event_survives_circular_reference():
    payload: dict = {"type": "done"}
    payload["self"] = payload
    line = worker.format_event(payload)
    parsed = json.loads(line[len("@@EVT ") :])
    assert parsed["type"] == "done"
    assert parsed.get("serialization_error") is True


def test_format_event_survives_unencodable_keys():
    line = worker.format_event({"type": "progress", ("a", "b"): 1})
    parsed = json.loads(line[len("@@EVT ") :])
    assert parsed["type"] == "progress"
    assert parsed.get("serialization_error") is True


def test_emitter_writes_and_flushes():
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    assert emitter.emit({"type": "pong"}) is True
    assert stream.getvalue() == '@@EVT {"type": "pong"}\n'


def test_emitter_returns_false_when_stream_fails():
    class BrokenStream:
        def write(self, _text):
            raise OSError("broken pipe")

        def flush(self):
            raise OSError("broken pipe")

    assert worker.EventEmitter(BrokenStream()).emit({"type": "pong"}) is False


def test_emitter_error_truncates_message():
    stream = io.StringIO()
    worker.EventEmitter(stream).error(
        message="あ" * 1000, category="pipeline", fatal=True, job_id="j1", detail="d"
    )
    payload = parse_events(stream)[0]
    assert payload["type"] == "error"
    assert payload["fatal"] is True
    assert payload["category"] == "pipeline"
    assert payload["job_id"] == "j1"
    assert len(payload["message"]) <= worker.MESSAGE_MAX_CHARS


def test_tail_detail_keeps_only_last_lines():
    text = "\n".join(f"line{i}" for i in range(50))
    detail = worker.tail_detail(text, lines=3)
    assert detail.splitlines() == ["line47", "line48", "line49"]


def test_tail_detail_limits_length():
    detail = worker.tail_detail("x" * 5000, lines=5)
    assert len(detail) <= worker.DETAIL_MAX_CHARS


# ---------------------------------------------------------------- エラー分類


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (worker.InputValidationError("bad"), ("input", False)),
        (worker.CommandError("bad json"), ("input", False)),
        (MemoryError("boom"), ("oom", True)),
        (RuntimeError("MPS backend out of memory"), ("oom", True)),
        (RuntimeError("can't allocate 12GB"), ("oom", True)),
        (RuntimeError("Cannot allocate memory"), ("oom", True)),
        (RuntimeError("MPS: Internal error"), ("mps", True)),
        (RuntimeError("Metal command buffer failed"), ("mps", True)),
        (ValueError("something else"), ("pipeline", True)),
        (OSError("disk full"), ("pipeline", False)),
    ],
)
def test_classify_exception_mapping(exc, expected):
    assert worker.classify_exception(exc) == expected


def test_classify_exception_uses_default_category():
    assert worker.classify_exception(ValueError("x"), default_category="model_state") == (
        "model_state",
        True,
    )


def test_classify_exception_does_not_false_positive_on_mps_substring():
    assert worker.classify_exception(ValueError("temps are high")) == ("pipeline", True)


def test_classify_exception_input_stays_non_fatal_with_other_default():
    assert worker.classify_exception(
        worker.InputValidationError("x"), default_category="model_state"
    ) == ("input", False)


# ---------------------------------------------------------------- 進捗ラッパ


def test_progress_bar_passes_items_through_and_emits_1_to_n():
    calls: list[tuple] = []
    bar = worker.make_progress_bar("job1", lambda job_id, step, total: calls.append((job_id, step, total)))
    items = ["t0", "t1", "t2", "t3"]
    assert list(bar(items)) == items
    assert calls == [("job1", i + 1, 4) for i in range(4)]


def test_progress_bar_accepts_extra_arguments():
    bar = worker.make_progress_bar("job1", lambda job_id, step, total: None)
    assert list(bar([1, 2], desc="denoise", total=2)) == [1, 2]


def test_progress_bar_continues_when_emit_raises():
    def broken(job_id, step, total):
        raise RuntimeError("emit failed")

    bar = worker.make_progress_bar("job1", broken)
    assert list(bar([1, 2, 3])) == [1, 2, 3]


def test_progress_bar_handles_iterable_without_len():
    calls: list[tuple] = []
    missing: list[bool] = []
    bar = worker.make_progress_bar(
        "job1",
        lambda job_id, step, total: calls.append((step, total)),
        on_missing_total=lambda: missing.append(True),
    )
    assert list(bar(iter([1, 2, 3]))) == [1, 2, 3]
    assert calls == []
    assert missing == [True]


def test_progress_bar_handles_empty_iterable():
    calls: list[tuple] = []
    bar = worker.make_progress_bar("job1", lambda job_id, step, total: calls.append(step))
    assert list(bar([])) == []
    assert calls == []


def test_progress_bar_survives_broken_missing_total_callback():
    def broken():
        raise RuntimeError("callback failed")

    bar = worker.make_progress_bar("job1", lambda **_: None, on_missing_total=broken)
    assert list(bar(iter([1, 2]))) == [1, 2]


# ---------------------------------------------------------------- コマンド解析


def test_parse_command_valid():
    assert worker.parse_command('{"cmd": "ping"}') == {"cmd": "ping"}


def test_parse_command_strips_whitespace():
    assert worker.parse_command('  {"cmd": " ping "}  \n') == {"cmd": "ping"}


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "{broken",
        "[1, 2, 3]",
        '"just a string"',
        "123",
        "{}",
        '{"command": "ping"}',
        '{"cmd": 42}',
        '{"cmd": ""}',
        "   ",
    ],
)
def test_parse_command_rejects_invalid_lines(line):
    with pytest.raises(worker.CommandError):
        worker.parse_command(line)


# ---------------------------------------------------------------- コマンドループ


def _run_loop(lines: list[str], generate_fn=None):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    stdin = io.StringIO("".join(line + "\n" for line in lines))
    calls: list[dict] = []

    def default_generate(command):
        calls.append(command)
        return False

    code = worker.run_command_loop(stdin, emitter, generate_fn or default_generate)
    return code, parse_events(stream), calls


def test_loop_ping_returns_pong():
    code, events, _ = _run_loop(['{"cmd": "ping"}', '{"cmd": "shutdown"}'])
    assert code == worker.EXIT_OK
    assert events == [{"type": "pong"}]


def test_loop_shutdown_exits_cleanly():
    code, events, calls = _run_loop(['{"cmd": "shutdown"}', '{"cmd": "ping"}'])
    assert code == worker.EXIT_OK
    assert events == []  # shutdown 以降は処理しない
    assert calls == []


def test_loop_eof_exits_cleanly():
    code, events, _ = _run_loop([])
    assert code == worker.EXIT_OK
    assert events == []


def test_loop_ignores_blank_lines():
    code, events, _ = _run_loop(["", "   ", '{"cmd": "ping"}', '{"cmd": "shutdown"}'])
    assert code == worker.EXIT_OK
    assert events == [{"type": "pong"}]


def test_loop_reports_invalid_json_as_non_fatal_input_error():
    code, events, _ = _run_loop(["not json", '{"cmd": "shutdown"}'])
    assert code == worker.EXIT_OK
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["category"] == "input"
    assert events[0]["fatal"] is False


def test_loop_reports_unknown_command_as_non_fatal_input_error():
    code, events, calls = _run_loop(
        ['{"cmd": "explode", "job_id": "j1"}', '{"cmd": "shutdown"}']
    )
    assert code == worker.EXIT_OK
    assert calls == []
    assert events[0]["category"] == "input"
    assert events[0]["fatal"] is False
    assert events[0]["job_id"] == "j1"


def test_loop_dispatches_generate():
    code, _events, calls = _run_loop(
        ['{"cmd": "generate", "job_id": "j1"}', '{"cmd": "shutdown"}']
    )
    assert code == worker.EXIT_OK
    assert len(calls) == 1
    assert calls[0]["job_id"] == "j1"


def test_loop_stops_after_fatal_generate():
    seen: list[dict] = []

    def generate_fn(command):
        seen.append(command)
        return True  # fatal

    code, _events, _ = _run_loop(
        ['{"cmd": "generate", "job_id": "j1"}', '{"cmd": "generate", "job_id": "j2"}'],
        generate_fn=generate_fn,
    )
    assert code == worker.EXIT_FATAL_ERROR
    assert [c["job_id"] for c in seen] == ["j1"]


def test_loop_continues_after_non_fatal_generate():
    seen: list[dict] = []

    def generate_fn(command):
        seen.append(command)
        return False

    code, _events, _ = _run_loop(
        [
            '{"cmd": "generate", "job_id": "j1"}',
            '{"cmd": "generate", "job_id": "j2"}',
            '{"cmd": "shutdown"}',
        ],
        generate_fn=generate_fn,
    )
    assert code == worker.EXIT_OK
    assert [c["job_id"] for c in seen] == ["j1", "j2"]


# ---------------------------------------------------------------- 起動時設定


def _env(tmp_path: Path, data_root: Path, **overrides) -> dict:
    lora = tmp_path / "turbo.safetensors"
    lora.write_bytes(b"x" * 16)
    env = {
        "ATELIER_DATA_ROOT": str(data_root),
        "ATELIER_BACKEND_ID": "minimax_h3",
        "ATELIER_MODEL_ID": "DiffSynth-Studio/MiniMax-H3-NF4",
        "ATELIER_MODEL_REVISION": "nf4-turbo4step-ckpt500",
        "ATELIER_PROCESSOR_ID": "MiniMax/MiniMax-H3",
        "ATELIER_LORA_PATH": str(lora),
        "ATELIER_LORA_ALPHA": "1.0",
    }
    env.update(overrides)
    return env


def test_load_worker_config_valid(tmp_path, data_root):
    cfg = worker.load_worker_config(_env(tmp_path, data_root))
    assert cfg.backend_id == "minimax_h3"
    assert cfg.data_root == data_root.resolve()
    assert cfg.lora_alpha == 1.0


@pytest.mark.parametrize("missing", worker.REQUIRED_ENV_VARS)
def test_load_worker_config_rejects_missing_env(tmp_path, data_root, missing):
    env = _env(tmp_path, data_root)
    del env[missing]
    with pytest.raises(worker.WorkerConfigError) as excinfo:
        worker.load_worker_config(env)
    assert missing in str(excinfo.value)


@pytest.mark.parametrize("missing", worker.REQUIRED_ENV_VARS)
def test_load_worker_config_rejects_blank_env(tmp_path, data_root, missing):
    env = _env(tmp_path, data_root, **{missing: "   "})
    with pytest.raises(worker.WorkerConfigError):
        worker.load_worker_config(env)


def test_load_worker_config_rejects_missing_lora(tmp_path, data_root):
    env = _env(tmp_path, data_root, ATELIER_LORA_PATH=str(tmp_path / "nope.safetensors"))
    with pytest.raises(worker.WorkerConfigError):
        worker.load_worker_config(env)


def test_load_worker_config_rejects_empty_lora(tmp_path, data_root):
    empty = tmp_path / "empty.safetensors"
    empty.write_bytes(b"")
    env = _env(tmp_path, data_root, ATELIER_LORA_PATH=str(empty))
    with pytest.raises(worker.WorkerConfigError):
        worker.load_worker_config(env)


def test_load_worker_config_rejects_missing_data_root(tmp_path, data_root):
    env = _env(tmp_path, data_root, ATELIER_DATA_ROOT=str(tmp_path / "nope"))
    with pytest.raises(worker.WorkerConfigError):
        worker.load_worker_config(env)


def test_load_worker_config_rejects_bad_alpha(tmp_path, data_root):
    env = _env(tmp_path, data_root, ATELIER_LORA_ALPHA="strong")
    with pytest.raises(worker.WorkerConfigError):
        worker.load_worker_config(env)


# ---------------------------------------------------------------- generate 処理（偽 pipe）


class FakeImage:
    """PIL.Image の最小代替（convert / save のみ）。"""

    def __init__(self, index: int) -> None:
        self.index = index
        self.converted_to: str | None = None

    def convert(self, mode: str) -> "FakeImage":
        clone = FakeImage(self.index)
        clone.converted_to = mode
        return clone

    def save(self, path, format=None):  # noqa: A002 - PIL の引数名に合わせる
        assert format == "PNG"
        Path(path).write_bytes(b"PNG-BYTES")


class FakeRuntime:
    """diffsynth.write_video_audio と torch の最小代替。"""

    def __init__(self) -> None:
        self.torch = None
        self.calls: list[dict] = []

    def write_video_audio(self, video, audio, output_path, fps, audio_sample_rate):
        self.calls.append(
            {
                "frames": len(video),
                "audio": audio,
                "output_path": output_path,
                "fps": fps,
                "audio_sample_rate": audio_sample_rate,
            }
        )
        Path(output_path).write_bytes(b"MP4-BYTES")


def make_fake_pipe(num_frames: int = 56, raise_exc: BaseException | None = None):
    seen: dict = {}

    def fake_pipe(**kwargs):
        seen.update(kwargs)
        bar = kwargs["progress_bar_cmd"]
        for _ in bar(list(range(kwargs["num_inference_steps"]))):
            if raise_exc is not None:
                raise raise_exc
        return [FakeImage(i) for i in range(num_frames)], "AUDIO-TENSOR"

    fake_pipe.seen = seen  # type: ignore[attr-defined]
    return fake_pipe


def test_handle_generate_happy_path(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    runtime = FakeRuntime()
    pipe = make_fake_pipe()
    command = make_command(data_root)

    fatal = worker.handle_generate(command, pipe, runtime, config, emitter)
    events = parse_events(stream)

    assert fatal is False
    assert [e["type"] for e in events] == [
        "stage",  # preparing（生成開始の通知）
        "progress",
        "progress",
        "progress",
        "progress",
        "stage",
        "done",
    ]
    assert [(e["step"], e["total"]) for e in events[1:5]] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert events[0] == {"type": "stage", "stage": "preparing", "job_id": command["job_id"]}
    assert events[5] == {"type": "stage", "stage": "saving", "job_id": command["job_id"]}

    done = events[6]
    assert done["job_id"] == command["job_id"]
    assert done["seed_used"] == 42
    assert done["num_frames"] == 56
    assert done["backend_id"] == "minimax_h3"
    assert done["model_id"] == config.model_id
    assert done["model_revision"] == config.model_revision
    assert done["warnings"] == []
    assert done["output_partial_path"].endswith(".mp4.partial")
    assert done["last_frame_partial_path"].endswith("_last.png.partial")
    assert isinstance(done["elapsed_sec"], (int, float))

    # 成果物は partial にだけ書かれ、正式名は作られない
    assert Path(done["output_partial_path"]).stat().st_size > 0
    assert Path(done["last_frame_partial_path"]).stat().st_size > 0
    assert not (data_root / "outputs" / "v_test.mp4").exists()
    assert not (data_root / "outputs" / "v_test_last.png").exists()

    # 実証済みの呼び出し形（契約 §9）
    assert pipe.seen["height"] == 320
    assert pipe.seen["width"] == 576
    assert pipe.seen["num_frames"] == 56
    assert pipe.seen["num_inference_steps"] == 4
    assert pipe.seen["seed"] == 42
    assert runtime.calls[0]["fps"] == 24
    assert runtime.calls[0]["audio_sample_rate"] == 32000
    assert runtime.calls[0]["audio"] == "AUDIO-TENSOR"


def test_temp_encode_path_ends_with_mp4_and_is_hidden(data_root):
    partial = data_root / "outputs" / "v_test.mp4.partial"
    temp = worker.temp_encode_path(partial)
    # PyAV は拡張子でコンテナ形式を決めるため .mp4 で終わる必要がある
    assert temp.name.endswith(".mp4")
    assert temp.name.startswith(".")  # 隠しファイル（Finder の一覧に出さない）
    assert temp.parent == partial.parent  # os.replace の原子性のため同一ディレクトリ
    assert temp != partial
    assert temp.name != "v_test.mp4"  # 正式名を横取りしない


def test_handle_generate_encodes_via_mp4_temp_then_replaces(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    runtime = FakeRuntime()

    worker.handle_generate(make_command(data_root), make_fake_pipe(), runtime, config, emitter)

    encode_target = Path(runtime.calls[0]["output_path"])
    assert encode_target.name.endswith(".mp4")
    assert not encode_target.name.endswith(".partial")
    assert not encode_target.exists()  # 一時ファイルは残さない
    assert (data_root / "outputs" / "v_test.mp4.partial").stat().st_size > 0
    assert sorted(p.name for p in (data_root / "outputs").iterdir()) == [
        "v_test.mp4.partial",
        "v_test_last.png.partial",
    ]


def test_handle_generate_cleans_temp_when_encoding_fails(data_root, config):
    class BrokenRuntime(FakeRuntime):
        def write_video_audio(self, video, audio, output_path, fps, audio_sample_rate):
            Path(output_path).write_bytes(b"partially written")
            raise OSError("encoder exploded")

    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    fatal = worker.handle_generate(
        make_command(data_root), make_fake_pipe(), BrokenRuntime(), config, emitter
    )
    assert fatal is False
    assert list((data_root / "outputs").iterdir()) == []  # 一時ファイルも partial も残らない
    assert [e for e in parse_events(stream) if e["type"] == "done"] == []


def test_handle_generate_reports_input_error_without_dying(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    command = make_command(data_root, num_frames=243)

    fatal = worker.handle_generate(command, pipe, FakeRuntime(), config, emitter)
    events = parse_events(stream)

    assert fatal is False
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["category"] == "input"
    assert events[0]["fatal"] is False
    assert events[0]["job_id"] == command["job_id"]
    assert pipe.seen == {}  # pipe は呼ばれない


def test_handle_generate_classifies_mps_failure_as_fatal(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe(raise_exc=RuntimeError("MPS backend internal error"))

    fatal = worker.handle_generate(
        make_command(data_root), pipe, FakeRuntime(), config, emitter
    )
    events = parse_events(stream)

    assert fatal is True
    error = [e for e in events if e["type"] == "error"][0]
    assert error["category"] == "mps"
    assert error["fatal"] is True
    assert error["detail"]  # traceback 末尾が入る


def test_handle_generate_classifies_oom_as_fatal(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe(raise_exc=MemoryError("out of memory"))

    fatal = worker.handle_generate(
        make_command(data_root), pipe, FakeRuntime(), config, emitter
    )
    error = [e for e in parse_events(stream) if e["type"] == "error"][0]
    assert fatal is True
    assert error["category"] == "oom"


def test_handle_generate_fails_when_artifact_is_empty(data_root, config):
    class EmptyWriteRuntime(FakeRuntime):
        def write_video_audio(self, video, audio, output_path, fps, audio_sample_rate):
            Path(output_path).write_bytes(b"")

    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    fatal = worker.handle_generate(
        make_command(data_root), make_fake_pipe(), EmptyWriteRuntime(), config, emitter
    )
    events = parse_events(stream)
    assert fatal is False
    assert [e for e in events if e["type"] == "done"] == []
    assert [e for e in events if e["type"] == "error"][0]["category"] == "pipeline"


def test_handle_generate_warns_on_frame_count_mismatch(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe(num_frames=57)

    fatal = worker.handle_generate(
        make_command(data_root), pipe, FakeRuntime(), config, emitter
    )
    done = [e for e in parse_events(stream) if e["type"] == "done"][0]
    assert fatal is False
    assert done["num_frames"] == 56
    assert done["warnings"] and "フレーム数" in done["warnings"][0]


def test_handle_generate_records_warning_when_total_unavailable(data_root, config):
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)

    def pipe(**kwargs):
        bar = kwargs["progress_bar_cmd"]
        list(bar(iter(range(4))))  # len() の無い iterable
        return [FakeImage(i) for i in range(56)], "AUDIO"

    fatal = worker.handle_generate(
        make_command(data_root), pipe, FakeRuntime(), config, emitter
    )
    events = parse_events(stream)
    done = [e for e in events if e["type"] == "done"][0]
    assert fatal is False
    assert [e for e in events if e["type"] == "progress"] == []
    assert done["warnings"] and "ステップ進捗" in done["warnings"][0]


def test_handle_generate_survives_emit_failures(data_root, config):
    class FlakyStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.count = 0

        def write(self, text):
            self.count += 1
            if self.count <= 2:
                raise OSError("temporary failure")
            return super().write(text)

    stream = FlakyStream()
    emitter = worker.EventEmitter(stream)
    fatal = worker.handle_generate(
        make_command(data_root), make_fake_pipe(), FakeRuntime(), config, emitter
    )
    assert fatal is False
    # 出力に失敗した進捗があっても、生成と保存は完走する
    assert (data_root / "outputs" / "v_test.mp4.partial").stat().st_size > 0


# ---------------------------------------------------------------- generate（継続生成・P4）


def _continuation_command(data_root: Path, keyframe: Path) -> dict:
    return make_command(data_root, keyframe_path=str(keyframe))


def test_handle_generate_passes_keyframes_to_pipe(data_root, config):
    """実証スクリプトと同じ形: keyframes=[RGB画像] / keyframe_indices=[0]。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")

    fatal = worker.handle_generate(
        _continuation_command(data_root, keyframe), pipe, FakeRuntime(), config, emitter
    )
    events = parse_events(stream)

    assert fatal is False
    assert events[-1]["type"] == "done"
    assert pipe.seen["keyframe_indices"] == [0]
    keyframes = pipe.seen["keyframes"]
    assert isinstance(keyframes, list) and len(keyframes) == 1
    assert keyframes[0].mode == "RGB"
    assert keyframes[0].size == (576, 320)
    # 継続生成でも他の生成パラメータは単発とまったく同じ
    assert pipe.seen["width"] == 576 and pipe.seen["height"] == 320
    assert pipe.seen["num_frames"] == 56
    assert pipe.seen["num_inference_steps"] == 4
    assert pipe.seen["seed"] == 42


def test_handle_generate_without_keyframe_does_not_pass_keyframes(data_root, config):
    """通常生成の pipe 呼び出しは P2 と1ミリも変わらない（キーが増えない）。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()

    worker.handle_generate(make_command(data_root), pipe, FakeRuntime(), config, emitter)

    assert "keyframes" not in pipe.seen
    assert "keyframe_indices" not in pipe.seen
    assert sorted(pipe.seen) == [
        "height",
        "num_frames",
        "num_inference_steps",
        "progress_bar_cmd",
        "prompt",
        "seed",
        "width",
    ]


def test_handle_generate_produces_the_same_artifacts_for_continuation(data_root, config):
    """継続生成でも成果物の形（partial 2本・正式名なし）は単発と同じ。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")

    worker.handle_generate(
        _continuation_command(data_root, keyframe),
        make_fake_pipe(),
        FakeRuntime(),
        config,
        emitter,
    )
    done = [e for e in parse_events(stream) if e["type"] == "done"][0]

    assert Path(done["output_partial_path"]).stat().st_size > 0
    assert Path(done["last_frame_partial_path"]).stat().st_size > 0
    assert not (data_root / "outputs" / "v_test.mp4").exists()
    assert not (data_root / "outputs" / "v_test_last.png").exists()
    # 親のキーフレームは読むだけ（書き換えない）
    assert keyframe.is_file() and keyframe.stat().st_size > 0


@pytest.mark.parametrize(
    ("keyframe_factory", "expected"),
    [
        (lambda root, tmp: "outputs/v_parent_last.png", "絶対パス"),
        (
            lambda root, tmp: str(_write_png(tmp / "outside" / "v.png")),
            "データフォルダの外",
        ),
        (lambda root, tmp: str(root / "outputs" / "missing_last.png"), "見つかりません"),
    ],
)
def test_handle_generate_rejects_bad_keyframe_without_dying(
    data_root, config, tmp_path, keyframe_factory, expected
):
    """UI を迂回した不正キーフレームは input エラー。ワーカーは生存する（契約 §2）。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    command = make_command(data_root, keyframe_path=keyframe_factory(data_root, tmp_path))

    fatal = worker.handle_generate(command, pipe, FakeRuntime(), config, emitter)
    events = parse_events(stream)

    assert fatal is False  # 次のコマンドを受け付けられる
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["category"] == "input"
    assert events[0]["fatal"] is False
    assert events[0]["job_id"] == command["job_id"]
    assert expected in events[0]["message"]
    assert pipe.seen == {}  # pipe は呼ばれない
    assert list((data_root / "outputs").iterdir()) == []  # 成果物も作らない


@pytest.mark.parametrize("content", [b"not a png at all\n" * 8, b"", b"\x89PNG\r\n\x1a\n"])
def test_handle_generate_rejects_unreadable_keyframe_without_dying(
    data_root, config, content
):
    """PNG でない／壊れた画像は input エラー（fatal にしない）。"""
    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    broken = data_root / "outputs" / "broken_last.png"
    broken.write_bytes(content)

    fatal = worker.handle_generate(
        _continuation_command(data_root, broken), pipe, FakeRuntime(), config, emitter
    )
    events = parse_events(stream)
    error = [e for e in events if e["type"] == "error"][0]

    assert fatal is False
    assert error["category"] == "input"
    assert error["fatal"] is False
    assert "開けませんでした" in error["message"]
    assert pipe.seen == {}  # pipe は呼ばれない
    # preparing までは出るが、progress / done は出ない
    assert [e["type"] for e in events] == ["stage", "error"]
    assert [e for e in events if e["type"] == "done"] == []


def test_handle_generate_releases_keyframe_image_after_job(data_root, config):
    """ジョブ後にキーフレーム画像の参照が残らない（常駐プロセスで溜め込まない）。"""
    import gc
    import weakref

    captured: dict = {}

    def pipe(**kwargs):
        image = kwargs["keyframes"][0]
        captured["ref"] = weakref.ref(image)
        captured["indices"] = list(kwargs["keyframe_indices"])
        bar = kwargs["progress_bar_cmd"]
        list(bar(list(range(kwargs["num_inference_steps"]))))
        return [FakeImage(i) for i in range(56)], "AUDIO"

    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")

    fatal = worker.handle_generate(
        _continuation_command(data_root, keyframe), pipe, FakeRuntime(), config, emitter
    )

    assert fatal is False
    assert captured["indices"] == [0]
    gc.collect()
    assert captured["ref"]() is None, "キーフレーム画像が解放されていません"


def test_handle_generate_closes_keyframe_image(data_root, config, monkeypatch):
    """開いた画像は close() して解放する（release_job_memory と同じ思想）。"""

    class SpyImage:
        mode = "RGB"
        size = (576, 320)

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    spy = SpyImage()
    monkeypatch.setattr(worker, "open_keyframe_image", lambda path, **kw: spy)

    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    pipe = make_fake_pipe()
    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")

    worker.handle_generate(
        _continuation_command(data_root, keyframe), pipe, FakeRuntime(), config, emitter
    )

    assert pipe.seen["keyframes"] == [spy]
    assert spy.closed is True, "キーフレーム画像が close() されていません"


def test_handle_generate_releases_keyframe_image_after_failure(data_root, config):
    """生成が失敗した場合もキーフレーム画像を解放する。"""
    import gc
    import weakref

    captured: dict = {}

    def pipe(**kwargs):
        captured["ref"] = weakref.ref(kwargs["keyframes"][0])
        raise ValueError("something went wrong inside the pipeline")

    stream = io.StringIO()
    emitter = worker.EventEmitter(stream)
    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")

    fatal = worker.handle_generate(
        _continuation_command(data_root, keyframe), pipe, FakeRuntime(), config, emitter
    )

    assert fatal is True  # pipeline エラーは fatal（分類は従来どおり）
    gc.collect()
    assert captured["ref"]() is None, "失敗時にキーフレーム画像が解放されていません"


def test_worker_reuses_model_across_continuation_jobs(tmp_path, data_root, monkeypatch):
    """継続生成でモデル・LoRA を再初期化しない（常駐再利用。契約 §2）。"""
    for key, value in _env(tmp_path, data_root).items():
        monkeypatch.setenv(key, value)

    runtime = FakeRuntime()
    pipe = make_fake_pipe()
    build_calls: list[int] = []
    lora_calls: list[int] = []

    monkeypatch.setattr(worker, "load_runtime", lambda: runtime)
    monkeypatch.setattr(
        worker, "build_pipeline", lambda rt, cfg: (build_calls.append(1), pipe)[1]
    )
    monkeypatch.setattr(worker, "apply_lora", lambda p, cfg: lora_calls.append(1))

    keyframe = _write_png(data_root / "outputs" / "v_parent_last.png")
    single = make_command(data_root)
    continuation = make_command(
        data_root,
        keyframe_path=str(keyframe),
        output_partial_path=str(data_root / "outputs" / "v_child.mp4.partial"),
        last_frame_partial_path=str(data_root / "outputs" / "v_child_last.png.partial"),
    )
    stdin = io.StringIO(
        json.dumps(single)
        + "\n"
        + json.dumps(continuation)
        + "\n"
        + '{"cmd": "shutdown"}\n'
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert worker.main([]) == worker.EXIT_OK
    events = parse_events(stdout)
    assert [e["type"] for e in events if e["type"] == "done"] == ["done", "done"]
    assert build_calls == [1], "モデルを作り直しています"
    assert lora_calls == [1], "LoRA を読み直しています"
    assert pipe.seen["keyframe_indices"] == [0]  # 2本目（継続生成）の呼び出し


# ---------------------------------------------------------------- main（偽ランタイム）


def test_main_handshake_and_loop_with_stubbed_runtime(tmp_path, data_root, monkeypatch):
    """モデルを一切読み込まずに起動〜ready〜generate〜shutdown を通す。"""
    for key, value in _env(tmp_path, data_root).items():
        monkeypatch.setenv(key, value)

    runtime = FakeRuntime()
    pipe = make_fake_pipe()
    lora_calls: list[tuple] = []

    monkeypatch.setattr(worker, "load_runtime", lambda: runtime)
    monkeypatch.setattr(worker, "build_pipeline", lambda rt, cfg: pipe)
    monkeypatch.setattr(
        worker, "apply_lora", lambda p, cfg: lora_calls.append((p, cfg.lora_alpha))
    )

    command = make_command(data_root)
    stdin = io.StringIO(
        '{"cmd": "ping"}\n' + json.dumps(command) + "\n" + '{"cmd": "shutdown"}\n'
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    code = worker.main([])

    assert code == worker.EXIT_OK
    events = parse_events(stdout)
    assert [e["type"] for e in events] == [
        "stage",   # loading_model
        "stage",   # loading_lora
        "ready",
        "pong",
        "stage",   # preparing（生成開始）
        "progress",
        "progress",
        "progress",
        "progress",
        "stage",   # saving
        "done",
    ]
    assert events[0]["stage"] == "loading_model"
    assert events[1]["stage"] == "loading_lora"
    ready = events[2]
    assert ready["backend_id"] == "minimax_h3"
    assert ready["model_id"] == "DiffSynth-Studio/MiniMax-H3-NF4"
    assert ready["model_revision"] == "nf4-turbo4step-ckpt500"
    assert ready["capabilities"] == worker.CAPABILITIES
    assert lora_calls == [(pipe, 1.0)]
    assert events[-1]["type"] == "done"


def test_main_returns_config_error_without_env(monkeypatch):
    for key in worker.REQUIRED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert worker.main([]) == worker.EXIT_CONFIG_ERROR
    event = parse_events(stdout)[0]
    assert event["type"] == "error"
    assert event["category"] == "model_state"
    assert event["fatal"] is True


def test_main_reports_model_load_failure(tmp_path, data_root, monkeypatch):
    for key, value in _env(tmp_path, data_root).items():
        monkeypatch.setenv(key, value)

    def boom():
        raise RuntimeError("MPS device unavailable")

    monkeypatch.setattr(worker, "load_runtime", boom)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert worker.main([]) == worker.EXIT_INIT_ERROR
    events = parse_events(stdout)
    assert events[0] == {"type": "stage", "stage": "loading_model"}
    assert events[1]["type"] == "error"
    assert events[1]["category"] == "mps"
    assert events[1]["fatal"] is True


def test_main_reports_lora_failure(tmp_path, data_root, monkeypatch):
    for key, value in _env(tmp_path, data_root).items():
        monkeypatch.setenv(key, value)

    def boom(pipe, cfg):
        raise ValueError("lora key mismatch")

    monkeypatch.setattr(worker, "load_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(worker, "build_pipeline", lambda rt, cfg: make_fake_pipe())
    monkeypatch.setattr(worker, "apply_lora", boom)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    assert worker.main([]) == worker.EXIT_INIT_ERROR
    events = parse_events(stdout)
    assert [e["type"] for e in events[:2]] == ["stage", "stage"]
    assert events[2]["type"] == "error"
    assert events[2]["category"] == "model_state"
    assert events[2]["fatal"] is True


def test_main_exits_fatal_after_pipeline_error(tmp_path, data_root, monkeypatch):
    for key, value in _env(tmp_path, data_root).items():
        monkeypatch.setenv(key, value)

    pipe = make_fake_pipe(raise_exc=RuntimeError("unexpected kernel failure"))
    monkeypatch.setattr(worker, "load_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(worker, "build_pipeline", lambda rt, cfg: pipe)
    monkeypatch.setattr(worker, "apply_lora", lambda p, cfg: None)

    stdin = io.StringIO(json.dumps(make_command(data_root)) + "\n" + '{"cmd": "ping"}\n')
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert worker.main([]) == worker.EXIT_FATAL_ERROR
    events = parse_events(stdout)
    assert events[-1]["type"] == "error"
    assert events[-1]["category"] == "pipeline"
    assert events[-1]["fatal"] is True
    assert [e for e in events if e["type"] == "pong"] == []  # fatal 後は処理しない


# ---------------------------------------------------------------- 後始末ヘルパ


def test_release_job_memory_without_torch():
    worker.release_job_memory(None)  # 例外を投げない


def test_release_job_memory_calls_mps_empty_cache():
    calls: list[str] = []

    class FakeMps:
        @staticmethod
        def empty_cache():
            calls.append("empty_cache")

    class FakeTorch:
        mps = FakeMps

    worker.release_job_memory(FakeTorch)
    assert calls == ["empty_cache"]


def test_release_job_memory_swallows_mps_errors():
    class FakeMps:
        @staticmethod
        def empty_cache():
            raise RuntimeError("mps unavailable")

    class FakeTorch:
        mps = FakeMps

    worker.release_job_memory(FakeTorch)  # 例外を投げない


def test_format_max_rss_units():
    assert "unavailable" in worker.format_max_rss(None)
    assert "MiB" in worker.format_max_rss(1024 * 1024, platform="darwin")
    assert "MiB" in worker.format_max_rss(1024, platform="linux")


def test_verify_partial_artifact(tmp_path):
    ok = tmp_path / "a.mp4.partial"
    ok.write_bytes(b"data")
    assert worker.verify_partial_artifact(ok, "動画ファイル") == 4

    with pytest.raises(worker.ArtifactError):
        worker.verify_partial_artifact(tmp_path / "missing.partial", "動画ファイル")

    empty = tmp_path / "empty.partial"
    empty.write_bytes(b"")
    with pytest.raises(worker.ArtifactError):
        worker.verify_partial_artifact(empty, "動画ファイル")
