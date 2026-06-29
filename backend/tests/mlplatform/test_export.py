"""Export adapters: JSONL (record/SFT/preference) + columnar + round-trip."""

from __future__ import annotations

import json

from app.mlplatform.datasets.contracts import (
    AgentRole,
    Dataset,
    QAVerdict,
    Split,
    TaskType,
    TraceExample,
)
from app.mlplatform.datasets.export import (
    ColumnarExporter,
    ExportShape,
    JSONLExporter,
    export_csv,
    read_jsonl_records,
    shape_for_task,
)


def _ex(
    ex_id: str,
    *,
    output: str = '{"beats":[1]}',
    reward: float | None = None,
    passed: bool | None = None,
    book: str = "bk0",
    split: Split = Split.UNASSIGNED,
    label: str | None = None,
) -> TraceExample:
    qa = None if passed is None else QAVerdict(passed=passed, score=0.9 if passed else 0.1)
    ex = TraceExample(
        id=ex_id,
        role=AgentRole.ADAPTER,
        task=TaskType.SFT,
        prompt_key="adapter@v3",
        prompt_version="3.0.0",
        model="qwen-plus",
        input={"page_text": ex_id},
        output=output,
        reward=reward,
        qa=qa,
        book_id=book,
        split=split,
    )
    return ex.with_labels({"quality": label}) if label else ex


def test_jsonl_record_shape_roundtrips() -> None:
    ds = Dataset.from_examples("d", [_ex("a"), _ex("b")])
    text = JSONLExporter(shape=ExportShape.RECORD).to_jsonl(ds)
    rows = list(read_jsonl_records(text))
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"a", "b"}
    assert "content_hash" in rows[0]


def test_sft_shape_filters_to_good() -> None:
    ds = Dataset.from_examples(
        "d",
        [
            _ex("good", passed=True),
            _ex("bad", passed=False),
            _ex("labeled_good", label="good"),
        ],
    )
    rows = JSONLExporter(shape=ExportShape.SFT).rows(ds)
    ids = {r["id"] for r in rows}
    assert "good" in ids and "labeled_good" in ids
    assert "bad" not in ids
    # not-good-only keeps everything
    all_rows = JSONLExporter(shape=ExportShape.SFT, sft_good_only=False).rows(ds)
    assert len(all_rows) == 3
    assert "messages" in rows[0] and "completion" in rows[0]


def test_preference_shape_builds_pairs() -> None:
    ds = Dataset.from_examples(
        "d",
        [
            _ex("hi", reward=0.9, book="bk1", output="good take"),
            _ex("lo", reward=0.1, book="bk1", output="bad take"),
        ],
    )
    rows = JSONLExporter(shape=ExportShape.PREFERENCE).rows(ds)
    assert len(rows) == 1
    assert rows[0]["chosen"] == "good take"
    assert rows[0]["rejected"] == "bad take"
    assert rows[0]["margin"] > 0


def test_preference_falls_back_to_point() -> None:
    ds = Dataset.from_examples("d", [_ex("solo", reward=0.5, book="bkX")])
    rows = JSONLExporter(shape=ExportShape.PREFERENCE).rows(ds)
    assert "output" in rows[0]
    assert "chosen" not in rows[0]


def test_columnar_frame_is_rectangular() -> None:
    ds = Dataset.from_examples("d", [_ex("a", passed=True), _ex("b")])
    cols = ColumnarExporter().to_columns(ds)
    lengths = {len(v) for v in cols.values()}
    assert lengths == {2}
    # nested fields are JSON-encoded strings
    assert isinstance(cols["input"][0], str)
    assert json.loads(cols["input"][0])["page_text"] == "a"


def test_csv_export_has_header() -> None:
    ds = Dataset.from_examples("d", [_ex("a")])
    csv_text = export_csv(ds)
    header = csv_text.splitlines()[0]
    assert "id" in header and "role" in header


def test_empty_dataset_exports_empty() -> None:
    ds = Dataset(name="d", examples=())
    assert JSONLExporter().to_jsonl(ds) == ""


def test_shape_for_task() -> None:
    assert shape_for_task(TaskType.SFT) is ExportShape.SFT
    assert shape_for_task(TaskType.PREFERENCE) is ExportShape.PREFERENCE
    assert shape_for_task(TaskType.EVAL) is ExportShape.RECORD
