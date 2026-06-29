"""The self-contained dataset CLI: build / export / inspect (offline, hermetic)."""

from __future__ import annotations

import json

import pytest

from app.mlplatform.datasets.cli import demo_source, main, raw_from_row


def test_raw_from_row_permissive() -> None:
    row = {
        "trace_id": "t1",
        "prompt_key": "adapter@v3",
        "inputs": {"page_text": "p"},
        "output": "o",
        "qa": {"verdict": "pass"},
    }
    raw = raw_from_row(row, ordinal=0)
    assert raw.trace_id == "t1"
    assert raw.qa == {"verdict": "pass"}
    # missing fields fall back gracefully
    bare = raw_from_row({}, ordinal=5)
    assert bare.trace_id == "t5"
    assert bare.prompt_key == "unknown@v0"


def test_demo_source_size() -> None:
    assert demo_source(25).count() == 25


def test_cli_build(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["build", "--demo", "20", "--name", "d"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "d"
    assert out["n"] > 0
    assert out["lineage"]
    assert out["ingest"]["seen"] == 20


def test_cli_export_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["export", "--demo", "20", "--name", "d", "--shape", "sft", "--split", "train"])
    assert rc == 0
    out = capsys.readouterr().out
    # every emitted line is valid JSON and PII is scrubbed
    assert "@mail.com" not in out
    for line in out.splitlines():
        if line.strip():
            json.loads(line)


def test_cli_export_csv(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["export", "--demo", "10", "--name", "d", "--format", "csv"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "id" in out.splitlines()[0]


def test_cli_inspect(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["inspect", "--demo", "20", "--name", "d"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["latest_version"]
    assert [n["operation"] for n in out["lineage"]][0] == "ingest"


def test_cli_stage_toggles(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        ["build", "--demo", "10", "--name", "d", "--no-scrub", "--no-dedup", "--no-split"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scrub"] is None
    assert out["dedup"] is None


def test_cli_requires_input_or_demo() -> None:
    with pytest.raises(SystemExit):
        main(["build", "--name", "d"])


def test_cli_build_from_dump(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    dump = tmp_path / "traces.jsonl"
    rows = [
        {
            "trace_id": f"t{i}",
            "prompt_key": "adapter@v3",
            "inputs": {"page_text": f"beat {i} mail a@b.com"},
            "output": '{"beats":[1]}',
            "book_id": f"bk{i % 3}",
            "qa": {"verdict": "pass", "score": 0.9},
        }
        for i in range(15)
    ]
    dump.write_text("\n".join(json.dumps(r) for r in rows))
    rc = main(["build", "--input", str(dump), "--name", "fromfile"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n"] > 0
    assert out["scrub"]["by_rule"].get("email", 0) > 0
