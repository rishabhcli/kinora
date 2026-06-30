"""Pure-logic tests for the JSONPath selector + the request-template engine."""

from __future__ import annotations

from app.providers.types import WanMode, WanSpec
from app.video.adapters.open.jsonpath import jsonpath_all, jsonpath_first, select
from app.video.adapters.open.template import MISSING, build_context, render_template

# --------------------------- jsonpath ----------------------------------- #


def test_jsonpath_dotted_and_root() -> None:
    doc = {"data": {"generation": {"handle": "gen-1"}}}
    assert select(doc, "data.generation.handle") == "gen-1"
    assert select(doc, "$.data.generation.handle") == "gen-1"
    assert select(doc, "data.missing.path") is None


def test_jsonpath_indices() -> None:
    doc = {"output": ["a", "b", "c"]}
    assert select(doc, "output[0]") == "a"
    assert select(doc, "output[-1]") == "c"
    assert select(doc, "output[2]") == "c"
    assert select(doc, "output[9]") is None  # out of range → None, never raises


def test_jsonpath_bare_list_value() -> None:
    # ``output`` may itself be the URL list — select returns the whole node.
    doc = {"output": ["http://x/clip.mp4"]}
    assert select(doc, "output[-1]") == "http://x/clip.mp4"
    assert select(doc, "output") == ["http://x/clip.mp4"]


def test_jsonpath_wildcard_fans_over_list() -> None:
    doc = {"items": [{"url": "u1"}, {"url": "u2"}]}
    assert jsonpath_all(doc, "items[*].url") == ["u1", "u2"]
    assert jsonpath_first(doc, "items[*].url") == "u1"


def test_jsonpath_fallback_chain_first_non_null_wins() -> None:
    doc = {"a": None, "b": {"c": "value"}}
    assert select(doc, "a || b.c || z") == "value"
    assert select(doc, "missing || alsomissing") is None


def test_jsonpath_never_raises_on_type_mismatch() -> None:
    doc = {"x": "scalar"}
    assert select(doc, "x.deeper.path") is None
    assert select(doc, "x[0]") is None


# --------------------------- template ------------------------------------ #


def test_whole_string_placeholder_preserves_type() -> None:
    ctx = {"seed": 42, "duration_s": 5, "items": [1, 2]}
    assert render_template("{{seed}}", ctx) == 42
    assert render_template("{{duration_s}}", ctx) == 5
    assert render_template("{{items}}", ctx) == [1, 2]


def test_missing_key_omits_field() -> None:
    template = {"prompt": "{{prompt}}", "seed": "{{seed}}", "neg": "{{negative_prompt}}"}
    out = render_template(template, {"prompt": "hi", "seed": None, "negative_prompt": None})
    assert out == {"prompt": "hi"}  # None values dropped entirely (no nulls sent)


def test_default_used_when_missing() -> None:
    assert render_template("{{seed|7}}", {"seed": None}) == 7
    assert render_template("{{flag|true}}", {"flag": None}) is True
    assert render_template("{{name|anon}}", {"name": None}) == "anon"
    assert render_template("{{ratio|1.5}}", {"ratio": None}) == 1.5


def test_inline_interpolation_stringifies() -> None:
    out = render_template("seed-{{seed}}-end", {"seed": 9})
    assert out == "seed-9-end"


def test_nested_objects_and_list_drop_missing() -> None:
    template = {"a": {"b": "{{x}}", "c": "{{y}}"}, "list": ["{{x}}", "{{y}}", "lit"]}
    out = render_template(template, {"x": "X", "y": None})
    assert out == {"a": {"b": "X"}, "list": ["X", "lit"]}


def test_render_top_level_missing_returns_empty_dict() -> None:
    assert render_template("{{nope}}", {"nope": None}) == {}


def test_build_context_flattens_wanspec() -> None:
    spec = WanSpec(
        mode=WanMode.REFERENCE_TO_VIDEO,
        prompt="a hero",
        negative_prompt="blurry",
        reference_image_urls=["r1", "r2"],
        seed=11,
        duration_s=6,
        resolution="720P",
    )
    ctx = build_context(spec, extra={"model": "native-id"})
    assert ctx["prompt"] == "a hero"
    assert ctx["negative_prompt"] == "blurry"
    assert ctx["mode"] == "reference_to_video"
    assert ctx["reference_image_urls"] == ["r1", "r2"]
    assert ctx["reference_image_url"] == "r1"
    assert ctx["seed"] == 11
    assert ctx["model"] == "native-id"


def test_missing_sentinel_is_distinct() -> None:
    assert MISSING is not None
