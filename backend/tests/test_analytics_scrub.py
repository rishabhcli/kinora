"""Unit tests for PII-safe scrubbing (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.events import EventName, RawEvent, ReadMode
from app.analytics.scrub import (
    anonymize,
    redact_text,
    scrub_event,
    scrub_props,
    session_key,
)

SALT = "test-salt"


def _now() -> datetime:
    return datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# anonymization
# --------------------------------------------------------------------------- #


def test_anonymize_is_deterministic_and_opaque() -> None:
    a = anonymize("user@example.com", salt=SALT)
    b = anonymize("user@example.com", salt=SALT)
    assert a == b
    assert a is not None
    assert a.startswith("u_")
    assert "example.com" not in a
    assert "user" not in a[2:]  # raw id not embedded


def test_anonymize_salt_changes_output() -> None:
    assert anonymize("user@example.com", salt="a") != anonymize("user@example.com", salt="b")


def test_anonymize_none_and_empty() -> None:
    assert anonymize(None, salt=SALT) is None
    assert anonymize("   ", salt=SALT) is None


def test_session_key_prefix() -> None:
    key = session_key("sess-123", salt=SALT)
    assert key is not None
    assert key.startswith("s_")
    assert session_key(None, salt=SALT) is None


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


def test_redact_email() -> None:
    out = redact_text("contact jane.doe@example.com please")
    assert "jane.doe@example.com" not in out
    assert "[redacted]" in out


def test_redact_bearer_and_sk_key() -> None:
    assert "abc.def.ghi" not in redact_text("Authorization: Bearer abc.def.ghi")
    assert "sk-abcdef123456" not in redact_text("key sk-abcdef123456 here")


def test_redact_url_and_path() -> None:
    assert "secret" not in redact_text("see https://x.io/secret?token=zzz now")
    assert "jane" not in redact_text("opened /Users/jane/Documents/book.pdf")


def test_redact_truncates_long_text() -> None:
    out = redact_text("x" * 1000)
    assert len(out) <= 241 + 1  # cap + ellipsis


# --------------------------------------------------------------------------- #
# prop allow/deny
# --------------------------------------------------------------------------- #


def test_scrub_props_keeps_allowed() -> None:
    out = scrub_props({"page": 5, "velocity_wps": 4.0, "feature": "karaoke"})
    assert out == {"page": 5, "velocity_wps": 4.0, "feature": "karaoke"}


def test_scrub_props_drops_unknown_keys() -> None:
    out = scrub_props({"page": 5, "mystery_field": "value"})
    assert "mystery_field" not in out
    assert out["page"] == 5


def test_scrub_props_drops_denied_even_if_allowed_listed_name_collision() -> None:
    # "query" is denied; "note", "title", "email", etc.
    out = scrub_props({"search_query": "harry potter", "user_email": "x@y.com", "page": 3})
    assert "search_query" not in out
    assert "user_email" not in out
    assert out == {"page": 3}


def test_scrub_props_redacts_allowed_string_values() -> None:
    # 'stage' is allowed and a string; an embedded email is still redacted.
    out = scrub_props({"stage": "email me at a@b.com"})
    assert "a@b.com" not in out["stage"]


def test_scrub_props_drops_nested_structures() -> None:
    out = scrub_props({"page": {"nested": 1}, "feature": [1, 2, 3]})
    assert out == {}


def test_scrub_props_caps_count() -> None:
    # Every allowed key present; the result is bounded by the cap.
    from app.analytics.scrub import ALLOWED_PROP_KEYS

    big = dict.fromkeys(ALLOWED_PROP_KEYS, 1)
    out = scrub_props(big)
    assert len(out) <= 24
    assert len(out) > 0


# --------------------------------------------------------------------------- #
# end-to-end scrub_event
# --------------------------------------------------------------------------- #


def test_scrub_event_pseudonymises_and_scrubs() -> None:
    raw = RawEvent(
        event_id="e1",
        name="director.comment",
        occurred_at=_now(),
        user_ref="alice@example.com",
        session_ref="sess-9",
        book_id="book_1",
        mode=ReadMode.DIRECTOR,
        props={"agent": "continuity", "note": "make her coat red", "page": 4},
    )
    tracked = scrub_event(raw, salt=SALT, received_at=_now())
    assert tracked.anon_user_id is not None
    assert "alice" not in str(tracked.anon_user_id)
    assert tracked.session_key is not None and tracked.session_key.startswith("s_")
    assert tracked.name is EventName.DIRECTOR_COMMENT
    assert tracked.book_id == "book_1"
    # note dropped (denied), agent + page kept
    assert "note" not in tracked.props
    assert tracked.props["agent"] == "continuity"
    assert tracked.props["page"] == 4


def test_scrub_event_clamps_future_clock_skew() -> None:
    received = _now()
    future = received + timedelta(hours=3)
    raw = RawEvent(event_id="e1", name="app.opened", occurred_at=future)
    tracked = scrub_event(raw, salt=SALT, received_at=received)
    assert tracked.occurred_at == received  # clamped


def test_scrub_event_keeps_small_skew() -> None:
    received = _now()
    slightly = received + timedelta(minutes=1)
    raw = RawEvent(event_id="e1", name="app.opened", occurred_at=slightly)
    tracked = scrub_event(raw, salt=SALT, received_at=received)
    assert tracked.occurred_at == slightly
