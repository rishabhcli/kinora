"""PII scrubbing: detection, masking vs hashing, determinism, idempotency."""

from __future__ import annotations

from app.mlplatform.datasets.contracts import AgentRole, DirectorEdit, TaskType, TraceExample
from app.mlplatform.datasets.scrub import (
    Scrubber,
    ScrubStrategy,
    scrub_example,
    scrub_examples,
)


def _ex(inp: dict, output: str = "ok", **kw: object) -> TraceExample:
    return TraceExample(
        id=kw.get("id", "ex_1"),  # type: ignore[arg-type]
        role=AgentRole.ADAPTER,
        task=TaskType.SFT,
        prompt_key="adapter@v3",
        prompt_version="3.0.0",
        model="qwen-plus",
        input=inp,
        output=output,
        director_edits=tuple(kw.get("edits", ())),  # type: ignore[arg-type]
    )


def test_email_is_hashed_consistently() -> None:
    sc = Scrubber()
    a, _ = sc.scrub_text("contact a@b.com please")
    b, _ = sc.scrub_text("write a@b.com again")
    assert "a@b.com" not in a
    assert "[EMAIL:" in a

    def _token(text: str) -> str:
        return text[text.index("[EMAIL:") : text.index("]", text.index("[EMAIL:")) + 1]

    # same value → same hash token (identity-preserving)
    assert _token(a) == _token(b)
    # a different email → a different token
    c, _ = sc.scrub_text("mail x@y.com")
    assert _token(c) != _token(a)


def test_secrets_phone_card_ssn_ip() -> None:
    sc = Scrubber()
    text = (
        "key sk-ABCDEFGHIJ1234567890 phone +1 415 555 1234 "
        "card 4111 1111 1111 1111 ssn 123-45-6789 ip 10.0.0.1"
    )
    out, hits = sc.scrub_text(text)
    assert "sk-ABCDEFGHIJ" not in out
    assert "[SECRET" in out
    assert "[PHONE]" in out
    assert "[CARD]" in out
    assert "[SSN]" in out
    assert "[IP]" in out
    assert hits["api_key"] == 1


def test_years_are_not_phone_numbers() -> None:
    sc = Scrubber()
    out, hits = sc.scrub_text("In 2026 she walked. Page 12, beat 3.")
    assert "2026" in out
    assert "phone" not in hits


def test_scrub_example_is_idempotent_and_marks_flag() -> None:
    ex = _ex({"page_text": "mail me x@y.com"}, output="see z@w.com")
    once = scrub_example(ex)
    twice = scrub_example(once)
    assert once.scrubbed is True
    assert once.content_hash == twice.content_hash  # idempotent
    assert "@y.com" not in once.input["page_text"]
    assert "@w.com" not in once.output


def test_scrub_nested_structures() -> None:
    ex = _ex({"meta": {"notes": ["call 9876543210", "ok"]}, "n": 5})
    out = scrub_example(ex)
    assert "[PHONE]" in str(out.input["meta"])
    assert out.input["n"] == 5  # non-strings untouched


def test_scrub_director_edit_text() -> None:
    ex = _ex({"page_text": "ok"}, edits=(DirectorEdit(instruction="email me at a@b.com"),))
    out = scrub_example(ex)
    assert "@b.com" not in out.director_edits[0].instruction


def test_mask_strategy() -> None:
    sc = Scrubber(
        rules=(
            __import__(
                "app.mlplatform.datasets.scrub", fromlist=["ScrubRule"]
            ).ScrubRule.make("phone", r"\d{10}", "[PHONE]", strategy=ScrubStrategy.MASK),
        )
    )
    out, _ = sc.scrub_text("9876543210")
    assert out == "[PHONE]"


def test_scrub_examples_report() -> None:
    exs = [_ex({"page_text": "a@b.com"}, id=f"ex_{i}") for i in range(3)]
    scrubbed, report = scrub_examples(exs)
    assert report.examples_scrubbed == 3
    assert report.by_rule.get("email") == 3
    assert all(e.scrubbed for e in scrubbed)
