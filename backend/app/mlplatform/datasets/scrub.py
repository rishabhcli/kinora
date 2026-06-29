"""Deterministic PII scrubbing for training examples.

Run-traces carry whatever the reader typed (director comments, search queries)
and whatever the source book contained, so before an example is frozen into a
dataset its free text is scrubbed of personally-identifying and secret material.
The scrubber is **pure and deterministic** (same input → same redaction), which
keeps the content hash stable across re-scrubs and makes dedup honest.

Two scrubbing strategies per rule:

* **Mask** — replace the match with a stable placeholder token (``[EMAIL]``,
  ``[PHONE]`` …). The default for human-readable PII: the structure of the text
  survives for training while the value is gone.
* **Hash** — replace the match with a short, salted, deterministic hash token
  (``[EMAIL:ab12cd]``). Used when the *identity* of a value matters for grouping
  (the same email should redact to the same token) but the value must not leak.

Rules are applied longest-pattern-first so an API key inside an email-looking
string is caught by the more specific rule. The detectors are conservative
regexes tuned to avoid mangling the prose the Adapter needs (e.g. years and shot
ids are not phone numbers); :func:`scrub_example` walks every string field of an
example's input + output + edits and returns a new, frozen example.

No I/O, no model calls. Salt is configurable and defaults to a fixed package
constant so test redactions are reproducible.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from app.mlplatform.datasets.contracts import DirectorEdit, TraceExample
from app.mlplatform.datasets.errors import ScrubError

_DEFAULT_SALT = "kinora.mldata.scrub.v1"


class ScrubStrategy(StrEnum):
    MASK = "mask"
    HASH = "hash"


@dataclass(frozen=True, slots=True)
class ScrubRule:
    """One named PII detector + how to redact it."""

    name: str
    pattern: re.Pattern[str]
    placeholder: str
    strategy: ScrubStrategy = ScrubStrategy.MASK

    @classmethod
    def make(
        cls,
        name: str,
        pattern: str,
        placeholder: str,
        *,
        strategy: ScrubStrategy = ScrubStrategy.MASK,
        flags: int = 0,
    ) -> ScrubRule:
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ScrubError(f"scrub rule {name!r} has a bad pattern: {exc}") from exc
        return cls(name=name, pattern=compiled, placeholder=placeholder, strategy=strategy)


# --------------------------------------------------------------------------- #
# Default rule set — ordered most-specific → least-specific
# --------------------------------------------------------------------------- #


def default_rules() -> tuple[ScrubRule, ...]:
    """The built-in PII / secret detectors (conservative, prose-safe)."""
    return (
        # Secrets first — they can otherwise look like other things.
        ScrubRule.make(
            "api_key",
            r"\b(?:sk|pk|rk|api|key|token)[-_][A-Za-z0-9]{16,}\b",
            "[SECRET]",
            strategy=ScrubStrategy.HASH,
            flags=re.IGNORECASE,
        ),
        ScrubRule.make(
            "bearer",
            r"\bBearer\s+[A-Za-z0-9._\-]{12,}\b",
            "[SECRET]",
            strategy=ScrubStrategy.HASH,
        ),
        ScrubRule.make(
            "aws_key",
            r"\bAKIA[0-9A-Z]{16}\b",
            "[SECRET]",
            strategy=ScrubStrategy.HASH,
        ),
        ScrubRule.make(
            "jwt",
            r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
            "[SECRET]",
            strategy=ScrubStrategy.HASH,
        ),
        ScrubRule.make(
            "email",
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
            "[EMAIL]",
            strategy=ScrubStrategy.HASH,
        ),
        ScrubRule.make(
            "credit_card",
            r"\b(?:\d[ \-]?){13,16}\b",
            "[CARD]",
        ),
        ScrubRule.make(
            "ssn",
            r"\b\d{3}-\d{2}-\d{4}\b",
            "[SSN]",
        ),
        ScrubRule.make(
            "ipv4",
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
            "[IP]",
        ),
        ScrubRule.make(
            "phone",
            # International / NANP-ish: optional +, groups of digits with sep,
            # 10+ digits total. Excludes lone 4-digit years by the length floor.
            r"\b\+?\d{1,3}[\s.\-]?\(?\d{2,4}\)?[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}\b",
            "[PHONE]",
        ),
    )


# --------------------------------------------------------------------------- #
# The scrubber
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Scrubber:
    """Applies an ordered rule set to strings, masking or hashing matches."""

    rules: tuple[ScrubRule, ...] = field(default_factory=default_rules)
    salt: str = _DEFAULT_SALT

    def _token(self, rule: ScrubRule, match: str) -> str:
        if rule.strategy is ScrubStrategy.MASK:
            return rule.placeholder
        digest = hashlib.sha256(f"{self.salt}:{match}".encode()).hexdigest()[:6]
        # [EMAIL] → [EMAIL:ab12cd]
        return f"{rule.placeholder[:-1]}:{digest}]"

    def scrub_text(self, text: str) -> tuple[str, dict[str, int]]:
        """Scrub one string; return the redacted text + per-rule hit counts."""
        hits: dict[str, int] = {}
        out = text
        for rule in self.rules:
            count = 0

            def _sub(m: re.Match[str], _rule: ScrubRule = rule) -> str:
                nonlocal count
                count += 1
                return self._token(_rule, m.group(0))

            out = rule.pattern.sub(_sub, out)
            if count:
                hits[rule.name] = hits.get(rule.name, 0) + count
        return out, hits

    def scrub_value(self, value: Any, hits: dict[str, int]) -> Any:
        """Recursively scrub strings inside arbitrary JSON-able structures."""
        if isinstance(value, str):
            scrubbed, local = self.scrub_text(value)
            for k, v in local.items():
                hits[k] = hits.get(k, 0) + v
            return scrubbed
        if isinstance(value, Mapping):
            return {k: self.scrub_value(v, hits) for k, v in value.items()}
        if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
            return [self.scrub_value(v, hits) for v in value]
        return value


@dataclass(frozen=True, slots=True)
class ScrubReport:
    """What a scrub pass touched across a dataset."""

    examples_scrubbed: int = 0
    total_redactions: int = 0
    by_rule: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "examples_scrubbed": self.examples_scrubbed,
            "total_redactions": self.total_redactions,
            "by_rule": dict(self.by_rule),
        }


def scrub_example(example: TraceExample, *, scrubber: Scrubber | None = None) -> TraceExample:
    """Return a new example with input + output + director-edit text scrubbed.

    The returned example has ``scrubbed=True``. Its ``content_hash`` reflects the
    redacted payload, so a re-scrub is a no-op (idempotent) and dedup sees the
    clean value.
    """
    sc = scrubber or Scrubber()
    hits: dict[str, int] = {}
    new_input = sc.scrub_value(dict(example.input), hits)
    new_output, out_hits = sc.scrub_text(example.output)
    for k, v in out_hits.items():
        hits[k] = hits.get(k, 0) + v
    new_edits: list[DirectorEdit] = []
    for edit in example.director_edits:
        instr, e_hits = sc.scrub_text(edit.instruction)
        for k, v in e_hits.items():
            hits[k] = hits.get(k, 0) + v
        new_edits.append(replace(edit, instruction=instr))
    return replace(
        example,
        input=new_input,
        output=new_output,
        director_edits=tuple(new_edits),
        scrubbed=True,
    )


def scrub_examples(
    examples: Sequence[TraceExample], *, scrubber: Scrubber | None = None
) -> tuple[list[TraceExample], ScrubReport]:
    """Scrub a sequence of examples, returning the scrubbed list + an aggregate report."""
    sc = scrubber or Scrubber()
    out: list[TraceExample] = []
    by_rule: dict[str, int] = {}
    scrubbed_count = 0
    total = 0
    for ex in examples:
        before = ex.content_hash
        scrubbed = scrub_example(ex, scrubber=sc)
        out.append(scrubbed)
        if scrubbed.content_hash != before:
            scrubbed_count += 1
        # Recompute hit counts for the report.
        hits: dict[str, int] = {}
        sc.scrub_value(dict(ex.input), hits)
        _, oh = sc.scrub_text(ex.output)
        for k, v in oh.items():
            hits[k] = hits.get(k, 0) + v
        for edit in ex.director_edits:
            _, eh = sc.scrub_text(edit.instruction)
            for k, v in eh.items():
                hits[k] = hits.get(k, 0) + v
        for k, v in hits.items():
            by_rule[k] = by_rule.get(k, 0) + v
            total += v
    return out, ScrubReport(
        examples_scrubbed=scrubbed_count, total_redactions=total, by_rule=by_rule
    )


__all__ = [
    "ScrubReport",
    "ScrubRule",
    "ScrubStrategy",
    "Scrubber",
    "default_rules",
    "scrub_example",
    "scrub_examples",
]
