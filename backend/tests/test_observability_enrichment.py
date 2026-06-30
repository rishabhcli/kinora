"""Observability log enrichment — domain ids ride the contextvars spine.

The book/session/shot/provider/render-state ids bind onto contextvars and a
structlog processor injects them into every log line. These tests assert the
bind/restore discipline (no leak into a sibling scope), the set-only-when-given
inheritance rule, and that an explicit per-call value is never overwritten.
"""

from __future__ import annotations

import json

import pytest

from app.core.logging import _build_processors
from app.observability import enrichment as enr


@pytest.fixture(autouse=True)
def _clear_context() -> None:
    enr.clear_render_context()
    yield
    enr.clear_render_context()


def test_no_ids_bound_means_empty_context() -> None:
    assert enr.current_render_context() == {}


def test_bind_sets_all_given_ids_and_reset_restores() -> None:
    tokens = enr.bind_render_context(
        book_id="b1", session_id="se1", shot_id="sh1", provider="wan", render_state="GENERATING"
    )
    assert enr.get_book_id() == "b1"
    assert enr.get_session_id() == "se1"
    assert enr.get_shot_id() == "sh1"
    assert enr.get_provider() == "wan"
    assert enr.get_render_state() == "GENERATING"
    enr.reset_render_context(tokens)
    assert enr.current_render_context() == {}


def test_set_only_when_given_keeps_inherited_values() -> None:
    enr.bind_render_context(book_id="b1", session_id="se1")
    # A deeper bind sets only the shot id — book/session must persist.
    inner = enr.bind_render_context(shot_id="sh1")
    assert enr.get_book_id() == "b1"
    assert enr.get_session_id() == "se1"
    assert enr.get_shot_id() == "sh1"
    enr.reset_render_context(inner)
    # The shot id is gone; the outer book/session remain.
    assert enr.get_shot_id() is None
    assert enr.get_book_id() == "b1"


def test_render_log_context_scopes_and_yields_merged_dict() -> None:
    with enr.render_log_context(book_id="b1", shot_id="sh1") as merged:
        assert merged == {"book_id": "b1", "shot_id": "sh1"}
        assert enr.current_render_context() == {"book_id": "b1", "shot_id": "sh1"}
    # Restored on exit — a sibling never sees these.
    assert enr.current_render_context() == {}


def test_nested_scopes_restore_cleanly() -> None:
    with enr.render_log_context(book_id="b1"):
        with enr.render_log_context(shot_id="sh1"):
            assert enr.current_render_context() == {"book_id": "b1", "shot_id": "sh1"}
        assert enr.current_render_context() == {"book_id": "b1"}
    assert enr.current_render_context() == {}


def test_processor_injects_bound_ids() -> None:
    with enr.render_log_context(book_id="b1", session_id="se1", provider="wan"):
        out = enr.merge_render_context(None, "info", {"event": "render.start"})
    assert out["book_id"] == "b1"
    assert out["session_id"] == "se1"
    assert out["provider"] == "wan"


def test_processor_does_not_overwrite_explicit_event_values() -> None:
    with enr.render_log_context(shot_id="bound-shot"):
        out = enr.merge_render_context(None, "info", {"event": "x", "shot_id": "explicit-shot"})
    # An explicit binding at the call site wins over the contextvar.
    assert out["shot_id"] == "explicit-shot"


def test_processor_is_noop_outside_any_scope() -> None:
    out = enr.merge_render_context(None, "info", {"event": "x"})
    assert out == {"event": "x"}


def test_render_context_processors_returns_the_processor() -> None:
    procs = enr.render_context_processors()
    assert enr.merge_render_context in procs


def test_enrichment_slots_into_the_real_processor_chain() -> None:
    # Splice the enrichment processor before the renderer and run the chain.
    procs = _build_processors(json_logs=True)
    procs.insert(len(procs) - 1, enr.merge_render_context)
    with enr.render_log_context(book_id="b1", shot_id="sh1"):
        event: object = {"event": "rendering"}
        for proc in procs:
            event = proc(None, "info", event)  # type: ignore[arg-type]
    assert isinstance(event, str)
    rendered = json.loads(event)
    assert rendered["book_id"] == "b1"
    assert rendered["shot_id"] == "sh1"
