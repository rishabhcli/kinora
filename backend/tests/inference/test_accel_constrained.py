"""Constrained-decoding tests — constraints, mask projection, repair loop."""

from __future__ import annotations

import pytest

from app.inference.accel.constrained import (
    ChoiceConstraint,
    ConstrainedDecoder,
    JsonSchemaConstraint,
    JsonValueConstraint,
    RegexConstraint,
    constrain,
    project_mask,
)
from app.inference.accel.errors import ConstrainedDecodeError
from app.inference.accel.protocol import GenerationRequest, GenerationResult

REQ = GenerationRequest.from_prompt("classify this")


# --------------------------------------------------------------------------- #
# Choice constraint
# --------------------------------------------------------------------------- #


def test_choice_validate() -> None:
    c = ChoiceConstraint(["yes", "no", "maybe"])
    assert c.validate("yes").ok
    assert c.validate("  no ").ok
    assert not c.validate("perhaps").ok
    with pytest.raises(ValueError):
        ChoiceConstraint([])


def test_choice_mask_projection() -> None:
    c = ChoiceConstraint(["cat", "car", "dog"])
    vocab = ["c", "d", "x", "ca", "do"]
    # from empty prefix, tokens that begin a legal choice
    allowed = project_mask(c, "", vocab)
    assert "c" in allowed  # starts cat/car
    assert "d" in allowed  # starts dog
    assert "x" not in allowed
    assert "ca" in allowed
    # after 'ca', only tokens extending toward cat/car
    allowed2 = project_mask(c, "ca", ["t", "r", "x"])
    assert set(allowed2) == {"t", "r"}


# --------------------------------------------------------------------------- #
# Regex constraint
# --------------------------------------------------------------------------- #


def test_regex_validate() -> None:
    c = RegexConstraint(r"\d{3}-\d{4}")
    assert c.validate("123-4567").ok
    assert not c.validate("12-4567").ok
    assert not c.validate("123-4567x").ok


def test_regex_mask_partial() -> None:
    c = RegexConstraint(r"[ab]+")
    allowed = project_mask(c, "a", ["a", "b", "c"])
    assert "a" in allowed and "b" in allowed
    assert "c" not in allowed  # 'ac' cannot extend to a full [ab]+ match


# --------------------------------------------------------------------------- #
# JSON value + schema
# --------------------------------------------------------------------------- #


def test_json_value_types() -> None:
    assert JsonValueConstraint("object").validate('{"a": 1}').ok
    assert not JsonValueConstraint("object").validate("[1, 2]").ok
    assert JsonValueConstraint("array").validate("[1, 2]").ok
    assert JsonValueConstraint("number").validate("3.14").ok
    assert not JsonValueConstraint("number").validate("true").ok  # bool != number
    assert JsonValueConstraint("boolean").validate("true").ok
    assert JsonValueConstraint("any").validate('"hi"').ok


def test_json_value_tolerates_fences_and_prose() -> None:
    c = JsonValueConstraint("object")
    assert c.validate('```json\n{"x": 1}\n```').ok
    assert c.validate('here you go: {"x": 1} done').ok
    assert not c.validate("not json at all").ok


def test_json_schema_required_and_types() -> None:
    c = JsonSchemaConstraint(required=["mood", "score"], types={"score": "number"})
    assert c.validate('{"mood": "dark", "score": 0.8}').ok
    assert not c.validate('{"mood": "dark"}').ok  # missing score
    assert not c.validate('{"mood": "dark", "score": "high"}').ok  # wrong type


def test_json_schema_no_additional() -> None:
    c = JsonSchemaConstraint(required=["a"], additional=False)
    assert c.validate('{"a": 1}').ok
    assert not c.validate('{"a": 1, "b": 2}').ok


def test_json_schema_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        JsonSchemaConstraint(types={"x": "bogus"})


def test_json_value_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        JsonValueConstraint("bogus")


# --------------------------------------------------------------------------- #
# Constrained decoder repair loop
# --------------------------------------------------------------------------- #


class _ScriptedGen:
    """A generate fn returning successive scripted outputs; records calls.

    NB: ``text`` is space-joined tokens, so tests use space-free JSON payloads
    that survive the tokenize/join round-trip unchanged.
    """

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self._i = 0
        self.requests: list[GenerationRequest] = []

    async def __call__(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        i = self._i
        self._i = min(i + 1, len(self._outputs) - 1)
        return GenerationResult.from_tokens(self._outputs[i].split(), model="t")


def _scripted_generate(outputs: list[str]) -> tuple[_ScriptedGen, _ScriptedGen]:
    gen = _ScriptedGen(outputs)
    return gen, gen


async def test_valid_on_first_try_no_repair() -> None:
    gen, state = _scripted_generate(['{"ok":true}'])
    out = await constrain(gen, REQ, JsonValueConstraint("object"))
    assert out.repairs == 0
    assert out.value == {"ok": True}
    assert len(state.requests) == 1
    assert out.result.meta["accelerator"] == "constrained"


async def test_repairs_then_succeeds() -> None:
    gen, state = _scripted_generate(["not-json", "still-bad", '{"fixed":1}'])
    out = await constrain(gen, REQ, JsonValueConstraint("object"), max_repairs=2)
    assert out.repairs == 2
    assert out.value == {"fixed": 1}
    # 3 generate calls: initial + 2 repairs.
    assert len(state.requests) == 3
    # The repair request carries the failure context.
    last = state.requests[-1]
    assert any("invalid" in c.lower() for _r, c in last.messages)


async def test_exhausts_repairs_raises() -> None:
    gen, _state = _scripted_generate(["bad", "bad", "bad"])
    with pytest.raises(ConstrainedDecodeError) as ei:
        await constrain(gen, REQ, JsonValueConstraint("object"), max_repairs=2)
    assert ei.value.attempts == 3
    assert ei.value.raw_text == "bad"


async def test_zero_repairs_one_shot() -> None:
    gen, state = _scripted_generate(["bad", '{"x":1}'])
    with pytest.raises(ConstrainedDecodeError):
        await constrain(gen, REQ, JsonValueConstraint("object"), max_repairs=0)
    assert len(state.requests) == 1  # no repair attempted


async def test_negative_max_repairs_rejected() -> None:
    gen, _ = _scripted_generate(["x"])
    with pytest.raises(ValueError):
        ConstrainedDecoder(gen, max_repairs=-1)


async def test_choice_constrained_decode() -> None:
    gen, _ = _scripted_generate(["yes"])
    out = await constrain(gen, REQ, ChoiceConstraint(["yes", "no"]))
    assert out.value == "yes"
