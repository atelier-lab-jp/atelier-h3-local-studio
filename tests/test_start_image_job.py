"""開始画像つき生成（P8）の JobSpec・履歴・キュー配線の検証。

**この試験で確認できること／できないこと**（正直に書く）:

- Execution Engine は必ず MockEngine（**実モデルは絶対に起動しない**）。
- ディスパッチャは起動しない（`queue.start()` を呼ばない）ので、投入したジョブは
  QUEUED のまま残る。ここで見るのは「何がキュー・履歴へどう登録されたか」だけで、
  生成そのものは見ない（ワイヤ検証は test_real_engine / test_p2_integration）。
- 書き込み先は `tmp_path` のみ（プロジェクトの `data/` には一切触れない）。
- 画像の正規化そのもの（クロップ・透過・拒否文言）は `tests/test_start_image.py` の
  担当。ここでは「正規化済みの画像がジョブへどう結び付くか」だけを見る。
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
from datetime import datetime
from pathlib import Path

import pytest

from app.core.app_service import (
    START_IMAGE_PREFIX,
    AppService,
    with_start_image_prefix,
)
from app.core.config import load_config
from app.core.contracts import JobSpec, JobStatus, ValidationError, validate_job_spec
from app.core.history import _JOB_TYPES, HistoryRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]

T0 = datetime(2026, 8, 10, 12, 0, 0).astimezone()


# ---------------------------------------------------------------- 準備


@pytest.fixture()
def start_image_mod():
    """開始画像の純粋層（P8）。未実装の間はこのファイルの試験を飛ばす。"""
    return pytest.importorskip("app.core.start_image")


def _staging_dir(data_root: Path) -> Path:
    return data_root / "start_images" / "staging"


def _final_path(data_root: Path, start_image_id: str) -> Path:
    return data_root / "start_images" / f"{start_image_id}.png"


def stage_image(data_root: Path, color=(20, 120, 200)) -> tuple[str, Path]:
    """正規化済み相当の PNG（576×320・RGB）を staging へ置き、そのIDを返す。

    IDの決め方（`si_` ＋ PNGバイト列の sha256 先頭12桁）は P8 契約 §2 の手順10。
    """
    from PIL import Image

    buffer = io.BytesIO()
    with Image.new("RGB", (576, 320), color) as image:
        image.save(buffer, format="PNG")
    data = buffer.getvalue()
    start_image_id = "si_" + hashlib.sha256(data).hexdigest()[:12]
    staging = _staging_dir(data_root)
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / f"{start_image_id}.png"
    staged.write_bytes(data)
    return start_image_id, staged


def _make_service(tmp: Path):
    """モックの AppService を作る（`data_root` は tmp のみ。実モデルは使わない）。"""
    cfg = load_config(PROJECT_ROOT)
    cfg = dataclasses.replace(cfg, data_root=tmp)
    for d in (cfg.outputs_dir, cfg.concat_dir, cfg.tmp_dir, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    _staging_dir(tmp).mkdir(parents=True, exist_ok=True)

    from app.engine.mock_engine import MockEngine

    engine = MockEngine.from_config(cfg, sleep_fn=lambda s: None)
    service = AppService.build(cfg, "mock", engine=engine)
    service.history.load()
    return cfg, service


@pytest.fixture()
def service(tmp_path: Path):
    """待機列だけを使う AppService（ディスパッチャは起動しない）。"""
    _cfg, svc = _make_service(tmp_path)
    try:
        yield svc
    finally:
        svc.shutdown(timeout=5.0)


def write_png(path: Path, size: tuple[int, int] = (576, 320)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", size, (12, 34, 56)) as image:
        image.save(path, format="PNG")
    return path


def make_spec(data_root: Path, job_id: str = "v_20260810_120000_aaaa", **overrides):
    """contracts の検証だけを見るための素の JobSpec。"""
    params = {
        "job_id": job_id,
        "prompt": "開始画像の検証用プロンプト",
        "num_frames": 56,
        "steps": 4,
        "seed_requested": 42,
        "output_path": data_root / "outputs" / f"{job_id}.mp4",
        "last_frame_path": data_root / "outputs" / f"{job_id}_last.png",
    }
    params.update(overrides)
    return JobSpec(**params)


def succeed(service, job_id: str) -> None:
    """モック成果物を実ファイルとして置き、履歴を SUCCESS まで進める。"""
    outputs = service.cfg.outputs_dir
    video = outputs / f"{job_id}.mp4"
    video.write_bytes(b"\x00mock mp4\x00")
    last_frame = write_png(outputs / f"{job_id}_last.png")
    service.history.mark_running(job_id, T0)
    service.history.mark_success(
        job_id,
        output_path=video,
        last_frame_path=last_frame,
        seed_used=42,
        elapsed_sec=1.0,
        finished_at=T0,
    )


# ---------------------------------------------------------------- 非回帰（画像なし）


def test_plain_generation_is_unchanged_without_a_start_image(service):
    """画像を指定しない通常生成は従来どおり（keyframe を持たない single）。"""
    spec = service.build_spec(
        prompt="画像なしの通常生成", num_frames=56, steps=4, seed_requested=1
    )
    assert spec.job_type == "single"
    assert spec.parent_id is None
    assert spec.keyframe_path is None

    view = service.submit_generation(
        prompt="画像なしの通常生成", num_frames=56, steps=4, seed_requested=1
    )
    record = service.history.get(view.job_id)
    assert record.type == "single"
    assert record.parent_id is None
    assert record.keyframe_path is None
    assert record.prompt == "画像なしの通常生成"  # 定型は付かない


def test_empty_start_image_id_behaves_like_a_plain_generation(service):
    """未選択（空文字・None）は通常生成とまったく同じ経路を通る。"""
    for value in (None, "", "   "):
        view = service.submit_generation(
            prompt=f"未選択の確認 {value!r}",
            num_frames=56,
            steps=4,
            seed_requested=1,
            start_image_id=value,
        )
        record = service.history.get(view.job_id)
        assert record.type == "single"
        assert record.keyframe_path is None
    # 開始画像を1枚も確定していない（ディレクトリを汚していない）
    assert not list((service.cfg.data_root / "start_images").glob("si_*.png"))


# ---------------------------------------------------------------- 開始画像つき


def test_start_image_job_carries_its_own_job_type(service, start_image_mod):
    """開始画像つきは job_type="start_image" ／ 親なし ／ 正式パスの keyframe。"""
    data_root = service.cfg.data_root
    start_image_id, staged = stage_image(data_root)

    view = service.submit_generation(
        prompt="開始画像から動かす",
        num_frames=56,
        steps=4,
        seed_requested=7,
        start_image_id=start_image_id,
    )

    final = _final_path(data_root, start_image_id)
    assert final.is_file(), "ジョブ用の正式パスへ確定していません"
    assert final.read_bytes() == staged.read_bytes()

    # spec の形（下位層が見るもの）
    spec = service.build_spec(
        prompt="開始画像から動かす",
        num_frames=56,
        steps=4,
        seed_requested=7,
        start_image_path=final,
    )
    assert spec.job_type == "start_image"
    assert spec.parent_id is None
    assert spec.keyframe_path == final

    # 履歴の形（上位層が見るもの）
    record = service.history.get(view.job_id)
    assert record.parent_id is None
    assert record.keyframe_path == f"start_images/{start_image_id}.png"


def test_history_type_of_a_start_image_job_stays_single(service, start_image_mod):
    """履歴上は「個別動画」（P8・決定D26）。_JOB_TYPES もスキーマも変えない。"""
    start_image_id, _staged = stage_image(service.cfg.data_root)
    view = service.submit_generation(
        prompt="履歴の種別を確認",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )

    record = service.history.get(view.job_id)
    assert record.type == "single"
    assert record.type in _JOB_TYPES
    assert _JOB_TYPES == ("single", "continuation")  # 増やさない
    # 保存 → 読み直しでも壊れない（過去のコミットへ戻しても読める形）
    assert HistoryRecord.from_dict(record.to_dict()).type == "single"


def test_start_image_job_is_identifiable_from_history(service, start_image_mod):
    """履歴からの識別条件は `parent_id is None and keyframe_path is not None`。"""
    data_root = service.cfg.data_root
    start_image_id, _staged = stage_image(data_root)

    plain = service.submit_generation(
        prompt="ふつうの生成", num_frames=56, steps=4, seed_requested=1
    )
    with_image = service.submit_generation(
        prompt="画像つきの生成",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    succeed(service, with_image.job_id)
    child = service.submit_generation(
        prompt="その続き",
        num_frames=56,
        steps=4,
        seed_requested=1,
        parent_id=with_image.job_id,
    )

    def is_start_image(job_id: str) -> bool:
        record = service.history.get(job_id)
        return record.parent_id is None and record.keyframe_path is not None

    assert is_start_image(with_image.job_id) is True
    assert is_start_image(plain.job_id) is False
    assert is_start_image(child.job_id) is False  # 継続生成は親を持つ


# ---------------------------------------------------------------- 入力検証


def test_validate_job_spec_accepts_a_start_image_job(tmp_path):
    """start_image ＋ 開始画像あり ＋ 親なし は通る。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    image = write_png(data_root / "start_images" / "si_0123456789ab.png")
    spec = make_spec(data_root, job_type="start_image", keyframe_path=image)
    validate_job_spec(spec, data_root=data_root)


def test_validate_job_spec_rejects_start_image_with_parent(tmp_path):
    """start_image に継続元IDは付けられない（継続生成とは別物）。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    image = write_png(data_root / "start_images" / "si_0123456789ab.png")
    spec = make_spec(
        data_root,
        job_type="start_image",
        keyframe_path=image,
        parent_id="v_20260101_000000_old1",
    )
    with pytest.raises(ValidationError, match="継続元ID"):
        validate_job_spec(spec, data_root=data_root)


def test_validate_job_spec_rejects_start_image_without_image(tmp_path):
    """start_image なのに画像が無いのは中途半端な指定なので弾く。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    spec = make_spec(data_root, job_type="start_image")
    with pytest.raises(ValidationError, match="開始画像が必要"):
        validate_job_spec(spec, data_root=data_root)


def test_validate_job_spec_still_rejects_single_with_keyframe(tmp_path):
    """**非回帰**: 単発生成にキーフレームを付ける条件は一切緩めていない。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    image = write_png(data_root / "outputs" / "v_parent_last.png")
    spec = make_spec(data_root, keyframe_path=image)
    with pytest.raises(ValidationError, match="キーフレーム"):
        validate_job_spec(spec, data_root=data_root)


def test_validate_job_spec_still_rejects_continuation_without_parent(tmp_path):
    """**非回帰**: 継続生成に継続元IDが要る条件も変えていない。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    image = write_png(data_root / "outputs" / "v_parent_last.png")
    spec = make_spec(data_root, job_type="continuation", keyframe_path=image)
    with pytest.raises(ValidationError, match="継続元"):
        validate_job_spec(spec, data_root=data_root)


def test_validate_job_spec_rejects_unknown_job_type(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    with pytest.raises(ValidationError, match="ジョブ種別"):
        validate_job_spec(make_spec(data_root, job_type="ref2va"), data_root=data_root)


@pytest.mark.parametrize("job_type", ["start_image", "continuation"])
def test_keyframe_outside_data_root_is_rejected_for_every_job_type(tmp_path, job_type):
    """アプリのデータ領域の外の画像は、種別によらず拒否する（§15）。"""
    data_root = tmp_path / "data"
    (data_root / "outputs").mkdir(parents=True)
    outside = write_png(tmp_path / "outside" / "stolen.png")
    spec = make_spec(
        data_root,
        job_type=job_type,
        keyframe_path=outside,
        parent_id="v_20260101_000000_old1" if job_type == "continuation" else None,
    )
    with pytest.raises(ValidationError, match="データ領域の外"):
        validate_job_spec(spec, data_root=data_root)


def test_build_spec_rejects_parent_and_start_image_together(service, tmp_path):
    """継続元と開始画像は同時に指定できない（spec を作る層でも止める）。"""
    image = write_png(service.cfg.data_root / "start_images" / "si_0123456789ab.png")
    with pytest.raises(ValidationError, match="同時に指定できません"):
        service.build_spec(
            prompt="同時指定",
            num_frames=56,
            steps=4,
            seed_requested=1,
            parent_id="v_20260101_000000_old1",
            start_image_path=image,
        )


def test_submit_rejects_parent_and_start_image_together(service, start_image_mod):
    """投入口でも同時指定を断る（画像を確定する前に止める）。"""
    data_root = service.cfg.data_root
    start_image_id, _staged = stage_image(data_root)
    with pytest.raises(ValidationError, match="同時に指定できません"):
        service.submit_generation(
            prompt="同時指定",
            num_frames=56,
            steps=4,
            seed_requested=1,
            parent_id="v_20260101_000000_old1",
            start_image_id=start_image_id,
        )
    # 画像は確定していない（継続元の検証より先に作らない）
    assert not _final_path(data_root, start_image_id).exists()


# ---------------------------------------------------------------- 確定した画像の不変性


def test_queued_job_keeps_its_image_when_staging_changes(service, start_image_mod):
    """投入後に staging の画像を差し替えても、登録済みジョブの画像は変わらない。"""
    data_root = service.cfg.data_root
    start_image_id, staged = stage_image(data_root, color=(10, 10, 10))

    view = service.submit_generation(
        prompt="投入後に画像を差し替える",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    record = service.history.get(view.job_id)
    final = _final_path(data_root, start_image_id)
    committed = final.read_bytes()

    # 利用者が別の画像を選び直した（同じ staging の名前を上書きされた）状況
    from PIL import Image

    with Image.new("RGB", (576, 320), (250, 0, 0)) as other:
        other.save(staged, format="PNG")
    assert staged.read_bytes() != committed

    assert final.read_bytes() == committed
    assert service.history.get(view.job_id).keyframe_path == record.keyframe_path


def test_the_same_start_image_can_feed_several_jobs(service, start_image_mod):
    """同じ画像で何本でも作れる（同じ正式パスを共有し、二重に作らない）。"""
    data_root = service.cfg.data_root
    start_image_id, _staged = stage_image(data_root)
    final = _final_path(data_root, start_image_id)

    first = service.submit_generation(
        prompt="同じ画像から1本目",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    stat_after_first = final.stat()
    second = service.submit_generation(
        prompt="同じ画像から2本目",
        num_frames=124,
        steps=4,
        seed_requested=2,
        start_image_id=start_image_id,
    )

    a = service.history.get(first.job_id)
    b = service.history.get(second.job_id)
    assert a.keyframe_path == b.keyframe_path == f"start_images/{start_image_id}.png"
    assert first.job_id != second.job_id
    # 2回目は作り直さない（既にある正式ファイルを上書きしない）
    assert final.stat().st_mtime_ns == stat_after_first.st_mtime_ns
    assert len(list((data_root / "start_images").glob("si_*.png"))) == 1


# ---------------------------------------------------------------- 二重投入（P5 §6.2）


def test_double_tap_with_the_same_image_is_deduplicated(service, start_image_mod):
    """同じ画像・同じ内容の連続投入は1件にまとまる。"""
    service.submit_idempotency_sec = 5.0
    start_image_id, _staged = stage_image(service.cfg.data_root)
    kwargs = dict(
        prompt="二重タップの確認",
        num_frames=56,
        steps=4,
        seed_requested=7,
        start_image_id=start_image_id,
    )

    first = service.submit_generation_ex(**kwargs)
    second = service.submit_generation_ex(**kwargs)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.view.job_id == first.view.job_id
    assert len(service.history.list_records()) == 1


def test_changing_only_the_image_creates_another_job(service, start_image_mod):
    """画像だけを選び直した場合は**別のジョブ**として通す。"""
    service.submit_idempotency_sec = 5.0
    data_root = service.cfg.data_root
    first_id, _a = stage_image(data_root, color=(1, 2, 3))
    second_id, _b = stage_image(data_root, color=(200, 100, 50))
    assert first_id != second_id

    first = service.submit_generation_ex(
        prompt="画像だけ差し替える",
        num_frames=56,
        steps=4,
        seed_requested=7,
        start_image_id=first_id,
    )
    second = service.submit_generation_ex(
        prompt="画像だけ差し替える",
        num_frames=56,
        steps=4,
        seed_requested=7,
        start_image_id=second_id,
    )

    assert second.duplicate is False
    assert second.view.job_id != first.view.job_id
    assert service.history.get(first.view.job_id).keyframe_path != service.history.get(
        second.view.job_id
    ).keyframe_path


def test_dropping_the_image_creates_another_job(service, start_image_mod):
    """画像を外して押し直したときも別のジョブになる（同じ文面でも内容が違う）。"""
    service.submit_idempotency_sec = 5.0
    start_image_id, _staged = stage_image(service.cfg.data_root)

    with_image = service.submit_generation_ex(
        prompt="画像を外して押し直す",
        num_frames=56,
        steps=4,
        seed_requested=7,
        start_image_id=start_image_id,
    )
    without = service.submit_generation_ex(
        prompt="画像を外して押し直す", num_frames=56, steps=4, seed_requested=7
    )
    assert without.duplicate is False
    assert without.view.job_id != with_image.view.job_id


# ---------------------------------------------------------------- 投入に失敗したとき


def test_failed_submission_cleans_up_the_image_it_created(service, start_image_mod):
    """キュー登録に失敗したら、この呼び出しで作った正式画像を片づける。"""
    data_root = service.cfg.data_root
    start_image_id, _staged = stage_image(data_root)
    final = _final_path(data_root, start_image_id)

    def _boom(spec):
        raise RuntimeError("待機列がいっぱいです（試験用）")

    original = service.queue.submit
    service.queue.submit = _boom
    try:
        with pytest.raises(RuntimeError):
            service.submit_generation(
                prompt="登録に失敗する",
                num_frames=56,
                steps=4,
                seed_requested=1,
                start_image_id=start_image_id,
            )
    finally:
        service.queue.submit = original

    assert not final.exists(), "投入できなかった画像が残っています"
    assert service.history.list_records() == []


def test_failed_submission_keeps_an_image_other_jobs_use(service, start_image_mod):
    """既に他のジョブが使っている画像は、後続の失敗で消してはいけない。"""
    data_root = service.cfg.data_root
    start_image_id, _staged = stage_image(data_root)
    final = _final_path(data_root, start_image_id)

    service.submit_generation(
        prompt="先に成功する1本",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    assert final.is_file()

    def _boom(spec):
        raise RuntimeError("待機列がいっぱいです（試験用）")

    original = service.queue.submit
    service.queue.submit = _boom
    try:
        with pytest.raises(RuntimeError):
            service.submit_generation(
                prompt="あとから失敗する1本",
                num_frames=56,
                steps=4,
                seed_requested=2,
                start_image_id=start_image_id,
            )
    finally:
        service.queue.submit = original

    assert final.is_file(), "他のジョブが使っている画像を消してしまいました"


# ---------------------------------------------------------------- プロンプトの定型


def test_start_image_prefix_is_applied_once(service, start_image_mod):
    """定型は先頭へ1回だけ付く（既に付いていれば足さない）。"""
    start_image_id, _staged = stage_image(service.cfg.data_root)

    view = service.submit_generation(
        prompt="花が咲く",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    prompt = service.history.get(view.job_id).prompt
    assert prompt.startswith(START_IMAGE_PREFIX)
    assert prompt.count(START_IMAGE_PREFIX) == 1
    assert "花が咲く" in prompt

    again = service.submit_generation(
        prompt=prompt,
        num_frames=56,
        steps=4,
        seed_requested=2,
        start_image_id=start_image_id,
    )
    assert service.history.get(again.job_id).prompt.count(START_IMAGE_PREFIX) == 1


def test_start_image_prefix_is_not_applied_without_an_image(service):
    """画像なしのプロンプトには定型を付けない（従来どおり）。"""
    view = service.submit_generation(
        prompt="定型を付けない", num_frames=56, steps=4, seed_requested=1
    )
    assert service.history.get(view.job_id).prompt == "定型を付けない"
    assert with_start_image_prefix("") == ""
    assert with_start_image_prefix("   ") == "   "  # 空欄の検証を素通りさせない


# ---------------------------------------------------------------- チェーン・連結


def test_start_image_job_is_a_chain_root(service, start_image_mod):
    """**非回帰**: 開始画像ジョブはチェーン長1の根、その子は長さ2になる。"""
    start_image_id, _staged = stage_image(service.cfg.data_root)
    root = service.submit_generation(
        prompt="連なりの根",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    succeed(service, root.job_id)

    child = service.submit_generation(
        prompt="その続き",
        num_frames=56,
        steps=4,
        seed_requested=1,
        parent_id=root.job_id,
    )

    assert [r.id for r in service.history.resolve_chain(root.job_id)] == [root.job_id]
    assert [r.id for r in service.history.resolve_chain(child.job_id)] == [
        root.job_id,
        child.job_id,
    ]
    assert service.history.get(child.job_id).type == "continuation"


def test_start_image_job_can_be_concat_material(service, start_image_mod):
    """開始画像ジョブも任意順連結の素材にできる（履歴上は個別動画）。"""
    start_image_id, _staged = stage_image(service.cfg.data_root)
    view = service.submit_generation(
        prompt="連結の素材",
        num_frames=56,
        steps=4,
        seed_requested=1,
        start_image_id=start_image_id,
    )
    succeed(service, view.job_id)

    record = service.history.get(view.job_id)
    assert record.status is JobStatus.SUCCESS
    assert record.type in _JOB_TYPES
    candidates = [row.job_id for row in service.concat_candidates()]
    assert view.job_id in candidates
