"""Safety / guardrail policy layer — one decision over input + output.

The injection scanner (:mod:`app.llmops.injection`) guards *inputs*; the output
policy (:mod:`app.llmops.output_policy`) guards *outputs*. A real call wants a
single policy object it can ask "is this input safe to send?" and "is this output
safe to return?", with one consistent verdict vocabulary:

* ``ALLOW``    — pass through unchanged;
* ``SANITIZE`` — pass through, but use the sanitized rendition (input only);
* ``BLOCK``    — refuse.

:class:`GuardrailPolicy` ties the two halves together and adds the policy-level
decision (the thresholds at which a scan/report becomes SANITIZE vs BLOCK). It is
the object the service façade exposes, and the object the API can run a piece of
text through without touching a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.llmops.injection import InjectionScan, InjectionScanner, SanitizedText, sanitize
from app.llmops.output_policy import OutputPolicy, OutputReport, Severity


class Decision(StrEnum):
    ALLOW = "allow"
    SANITIZE = "sanitize"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class InputVerdict:
    """The guardrail decision for one untrusted input."""

    decision: Decision
    scan: InjectionScan
    sanitized: SanitizedText
    reasons: tuple[str, ...]

    @property
    def safe_text(self) -> str:
        """The text the caller should actually send to the model."""
        return self.sanitized.text


@dataclass(frozen=True, slots=True)
class OutputVerdict:
    """The guardrail decision for one model output."""

    decision: Decision
    report: OutputReport
    reasons: tuple[str, ...]


@dataclass
class GuardrailPolicy:
    """A composed input+output safety policy."""

    scanner: InjectionScanner = field(default_factory=InjectionScanner)
    output_policy: OutputPolicy = field(default_factory=OutputPolicy)
    #: Input score at/above which we refuse outright (vs merely sanitizing).
    input_block_score: float = 0.85
    #: Always sanitize untrusted inputs even when they look clean (defense in depth).
    always_sanitize_input: bool = True
    #: Redact matched injection spans when sanitizing a suspicious input.
    redact_on_suspicious: bool = True

    # -- input --------------------------------------------------------------- #

    def check_input(self, text: str) -> InputVerdict:
        scan = self.scanner.scan(text)
        reasons: list[str] = []
        if scan.score >= self.input_block_score:
            sanitized = sanitize(text, scanner=self.scanner, redact=True, add_fence=True)
            reasons.append(
                f"injection score {scan.score:.2f} >= block {self.input_block_score:.2f}"
            )
            if scan.top_category is not None:
                reasons.append(f"dominant category: {scan.top_category.value}")
            return InputVerdict(Decision.BLOCK, scan, sanitized, tuple(reasons))

        if scan.is_suspicious:
            sanitized = sanitize(
                text,
                scanner=self.scanner,
                redact=self.redact_on_suspicious,
                add_fence=True,
            )
            reasons.append(f"suspicious (score {scan.score:.2f}); sanitized + fenced")
            return InputVerdict(Decision.SANITIZE, scan, sanitized, tuple(reasons))

        sanitized = sanitize(
            text, scanner=self.scanner, redact=False, add_fence=self.always_sanitize_input
        )
        decision = Decision.SANITIZE if self.always_sanitize_input else Decision.ALLOW
        if self.always_sanitize_input:
            reasons.append("clean; fenced as data (defense in depth)")
        return InputVerdict(decision, scan, sanitized, tuple(reasons))

    # -- output -------------------------------------------------------------- #

    def check_output(self, text: str, *, protected_texts: list[str] | None = None) -> OutputVerdict:
        report = self.output_policy.check(text, protected_texts=protected_texts)
        if report.blocked:
            reasons = tuple(
                f"{v.kind.value} ({v.severity.name}): {v.detail}" for v in report.violations
            )
            return OutputVerdict(Decision.BLOCK, report, reasons)
        if report.violations:
            reasons = tuple(f"{v.kind.value}: {v.detail}" for v in report.violations)
            # Below the block bar but non-empty -> allow with a flag (audit trail).
            return OutputVerdict(Decision.ALLOW, report, reasons)
        return OutputVerdict(Decision.ALLOW, report, ())

    # -- convenience --------------------------------------------------------- #

    def is_input_blocked(self, text: str) -> bool:
        return self.check_input(text).decision is Decision.BLOCK

    def is_output_blocked(self, text: str, *, protected_texts: list[str] | None = None) -> bool:
        return self.check_output(text, protected_texts=protected_texts).decision is Decision.BLOCK


def default_json_policy() -> GuardrailPolicy:
    """A policy tuned for the crew's JSON-strict agents (§10): expect JSON output."""
    return GuardrailPolicy(output_policy=OutputPolicy(expect_json=True, block_at=Severity.HIGH))


__all__ = [
    "Decision",
    "GuardrailPolicy",
    "InputVerdict",
    "OutputVerdict",
    "default_json_policy",
]
