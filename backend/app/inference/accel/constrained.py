"""Structured-output / constrained-decoding helpers.

The Kinora agents emit strict JSON contracts (§10). Two complementary mechanisms
keep generations on-spec:

* **Constraints** (this module's :class:`Constraint` hierarchy) describe the legal
  output set: a fixed *choice* set, a *regex*, a JSON *type* (object / array /
  number / …), or a lightweight JSON *schema* (required keys + per-key types).
  Each constraint can both **validate** a finished string and, for grammar-style
  constraints, **project a token mask** — given the text generated so far, which
  next tokens keep the output on a path that can still satisfy the constraint.
  The mask is what a serving engine applies to the logits to *guarantee* valid
  output; here it is exposed as a pure function over a candidate vocabulary so
  it is fully testable.

* **Constrained generation** (:func:`constrain` / :class:`ConstrainedDecoder`)
  wraps any :class:`~app.inference.accel.protocol.InferenceBackend`: it generates,
  validates against the constraint, and on failure issues a bounded number of
  *repair* rounds (appending a terse "your output was invalid because …" turn)
  before giving up with a :class:`~app.inference.accel.errors.ConstrainedDecodeError`.
  This is the model-agnostic fallback for backends that cannot apply a hard mask.

Everything is deterministic and dependency-free (stdlib ``json`` + ``re``).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from .errors import ConstrainedDecodeError
from .protocol import GenerationRequest, GenerationResult

# --------------------------------------------------------------------------- #
# Constraints
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating a candidate string against a constraint."""

    ok: bool
    reason: str = ""
    #: For JSON constraints, the parsed value when ``ok``.
    value: object | None = None


class Constraint(ABC):
    """A description of the legal output set for a generation."""

    @abstractmethod
    def validate(self, text: str) -> ValidationResult:
        """Whether ``text`` (a complete output) satisfies the constraint."""

    def allowed_next(self, prefix: str, vocabulary: Sequence[str]) -> tuple[str, ...]:
        """Tokens from ``vocabulary`` that keep ``prefix`` on a satisfiable path.

        Default: allow any token whose addition does not already make the prefix
        unsatisfiable, falling back to "all" when the constraint cannot reason
        incrementally. Grammar-style constraints override this for a real mask.
        """
        return tuple(vocabulary)

    def describe(self) -> str:
        """A short, human-readable description used in repair prompts."""
        return self.__class__.__name__


class ChoiceConstraint(Constraint):
    """Output must be exactly one of a fixed set of strings."""

    def __init__(self, choices: Sequence[str]) -> None:
        if not choices:
            raise ValueError("ChoiceConstraint needs at least one choice")
        self._choices = tuple(choices)
        self._set = set(self._choices)

    @property
    def choices(self) -> tuple[str, ...]:
        return self._choices

    def validate(self, text: str) -> ValidationResult:
        stripped = text.strip()
        if stripped in self._set:
            return ValidationResult(ok=True, value=stripped)
        return ValidationResult(ok=False, reason=f"must be one of {self._choices!r}")

    def allowed_next(self, prefix: str, vocabulary: Sequence[str]) -> tuple[str, ...]:
        # A token may follow ``prefix`` iff ``prefix + token`` is still a prefix
        # of some legal choice (token-as-string append; vocabulary-agnostic).
        out = [
            tok
            for tok in vocabulary
            if any(c.startswith(prefix + tok) or (prefix + tok) == c for c in self._choices)
        ]
        return tuple(out)

    def describe(self) -> str:
        return f"one of {list(self._choices)}"


class RegexConstraint(Constraint):
    """Output must fully match a regular expression."""

    def __init__(self, pattern: str) -> None:
        self._pattern = pattern
        self._re = re.compile(pattern)

    def validate(self, text: str) -> ValidationResult:
        if self._re.fullmatch(text.strip()):
            return ValidationResult(ok=True, value=text.strip())
        return ValidationResult(ok=False, reason=f"must fully match /{self._pattern}/")

    def allowed_next(self, prefix: str, vocabulary: Sequence[str]) -> tuple[str, ...]:
        # Allow a token if ``prefix + token`` is still a *partial* match (can be
        # extended to a full match). We approximate "can still match" by checking
        # the candidate against an anchored partial matcher.
        out = []
        for tok in vocabulary:
            cand = prefix + tok
            if self._could_match(cand):
                out.append(tok)
        return tuple(out)

    def _could_match(self, candidate: str) -> bool:
        # A prefix can still lead to a full match if it fully matches OR matches
        # with the regex's partial flag (re does not expose partial matching, so
        # we test fullmatch and a permissive "match a prefix" heuristic).
        if self._re.fullmatch(candidate):
            return True
        m = self._re.match(candidate)
        return m is not None and m.end() == len(candidate)

    def describe(self) -> str:
        return f"matching /{self._pattern}/"


class JsonValueConstraint(Constraint):
    """Output must parse as JSON of an expected top-level type."""

    _TYPES: Mapping[str, type | tuple[type, ...]] = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
        "any": object,
    }

    def __init__(self, expected: str = "object") -> None:
        if expected not in self._TYPES:
            raise ValueError(f"unknown JSON type {expected!r}")
        self._expected = expected

    def validate(self, text: str) -> ValidationResult:
        parsed, err = _try_parse_json(text)
        if err is not None:
            return ValidationResult(ok=False, reason=err)
        expected_type = self._TYPES[self._expected]
        if self._expected == "number" and isinstance(parsed, bool):
            # bool is a subclass of int; a JSON "number" should not be a bool.
            return ValidationResult(ok=False, reason="expected a number, got boolean")
        if self._expected != "any" and not isinstance(parsed, expected_type):
            return ValidationResult(
                ok=False, reason=f"expected JSON {self._expected}, got {type(parsed).__name__}"
            )
        return ValidationResult(ok=True, value=parsed)

    def describe(self) -> str:
        return f"a JSON {self._expected}"


class JsonSchemaConstraint(Constraint):
    """Output must be a JSON object with required keys of expected primitive types.

    A deliberately small schema dialect (not full JSON-Schema): ``required`` keys
    must be present; ``types`` maps key -> one of the
    :class:`JsonValueConstraint` type names; unknown keys are allowed unless
    ``additional`` is False.
    """

    def __init__(
        self,
        *,
        required: Sequence[str] = (),
        types: Mapping[str, str] | None = None,
        additional: bool = True,
    ) -> None:
        self._required = tuple(required)
        self._types = dict(types or {})
        for t in self._types.values():
            if t not in JsonValueConstraint._TYPES:
                raise ValueError(f"unknown type {t!r} in schema")
        self._additional = additional

    def validate(self, text: str) -> ValidationResult:
        parsed, err = _try_parse_json(text)
        if err is not None:
            return ValidationResult(ok=False, reason=err)
        if not isinstance(parsed, dict):
            return ValidationResult(ok=False, reason="expected a JSON object")
        for key in self._required:
            if key not in parsed:
                return ValidationResult(ok=False, reason=f"missing required key {key!r}")
        if not self._additional:
            allowed = set(self._required) | set(self._types)
            extra = set(parsed) - allowed
            if extra:
                return ValidationResult(ok=False, reason=f"unexpected keys {sorted(extra)!r}")
        for key, type_name in self._types.items():
            if key not in parsed:
                continue
            sub = JsonValueConstraint(type_name).validate(json.dumps(parsed[key]))
            if not sub.ok:
                return ValidationResult(ok=False, reason=f"key {key!r}: {sub.reason}")
        return ValidationResult(ok=True, value=parsed)

    def describe(self) -> str:
        bits = []
        if self._required:
            bits.append(f"required keys {list(self._required)}")
        if self._types:
            bits.append(f"types {self._types}")
        if not self._additional:
            bits.append("no additional keys")
        return "a JSON object with " + (", ".join(bits) if bits else "no constraints")


def _try_parse_json(text: str) -> tuple[object, str | None]:
    """Parse JSON, tolerating code fences / surrounding prose; (value, error)."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        if candidate.endswith("```"):
            candidate = candidate[:-3]
        candidate = candidate.strip()
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1]), None
            except json.JSONDecodeError:
                continue
    return None, "not valid JSON"


# --------------------------------------------------------------------------- #
# Token-mask projection
# --------------------------------------------------------------------------- #


def project_mask(
    constraint: Constraint, prefix: str, vocabulary: Sequence[str]
) -> tuple[str, ...]:
    """The set of next tokens that keep ``prefix`` on a constraint-satisfiable path.

    A thin façade over :meth:`Constraint.allowed_next` so call sites do not need
    to know which constraints reason incrementally. Returns a (possibly empty)
    tuple — an empty mask means the prefix is already a dead end.
    """
    return constraint.allowed_next(prefix, vocabulary)


# --------------------------------------------------------------------------- #
# Constrained generation (model-agnostic repair loop)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConstrainedResult:
    """A validated constrained generation."""

    result: GenerationResult
    value: object
    repairs: int


GenerateFn = Callable[[GenerationRequest], Awaitable[GenerationResult]]


class ConstrainedDecoder:
    """Generate-validate-repair against a :class:`Constraint`.

    Wraps a generate function; on an invalid output it appends a repair turn
    quoting the failure reason and retries, up to ``max_repairs`` times.
    """

    def __init__(
        self,
        generate: GenerateFn,
        *,
        max_repairs: int = 2,
    ) -> None:
        if max_repairs < 0:
            raise ValueError("max_repairs must be >= 0")
        self._generate = generate
        self._max_repairs = max_repairs

    async def decode(
        self, request: GenerationRequest, constraint: Constraint
    ) -> ConstrainedResult:
        """Produce an output for ``request`` satisfying ``constraint``."""
        attempt_req = request
        last_text = ""
        for repair in range(self._max_repairs + 1):
            result = await self._generate(attempt_req)
            last_text = result.text
            verdict = constraint.validate(result.text)
            if verdict.ok:
                return ConstrainedResult(
                    result=result.with_meta(
                        accelerator="constrained",
                        constraint=constraint.describe(),
                        repairs=repair,
                    ),
                    value=verdict.value,
                    repairs=repair,
                )
            if repair < self._max_repairs:
                attempt_req = self._repair_request(request, result.text, verdict.reason, constraint)
        raise ConstrainedDecodeError(
            f"output never satisfied constraint ({constraint.describe()})",
            raw_text=last_text,
            attempts=self._max_repairs + 1,
        )

    @staticmethod
    def _repair_request(
        original: GenerationRequest, bad_text: str, reason: str, constraint: Constraint
    ) -> GenerationRequest:
        repair_turn = (
            f"Your previous reply was invalid: {reason}. "
            f"Return ONLY output that is {constraint.describe()} — no prose, no fences."
        )
        messages = [
            *({"role": r, "content": c} for r, c in original.messages),
            {"role": "assistant", "content": bad_text},
            {"role": "user", "content": repair_turn},
        ]
        return GenerationRequest.from_messages(
            messages,
            model=original.model,
            temperature=original.temperature,
            max_tokens=original.max_tokens,
            tags=dict(original.tags),
        )


async def constrain(
    generate: GenerateFn,
    request: GenerationRequest,
    constraint: Constraint,
    *,
    max_repairs: int = 2,
) -> ConstrainedResult:
    """One-shot convenience wrapper around :class:`ConstrainedDecoder`."""
    return await ConstrainedDecoder(generate, max_repairs=max_repairs).decode(request, constraint)


__all__ = [
    "ChoiceConstraint",
    "ConstrainedDecoder",
    "ConstrainedResult",
    "Constraint",
    "JsonSchemaConstraint",
    "JsonValueConstraint",
    "RegexConstraint",
    "ValidationResult",
    "constrain",
    "project_mask",
]
