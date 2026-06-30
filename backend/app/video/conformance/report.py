"""The conformance report model: checks, outcomes, and a scored verdict.

:func:`~app.video.conformance.runner.run_conformance` returns a
:class:`ConformanceReport` — a pydantic v2 model that is the durable, serialisable
record of whether an adapter is trustworthy. It is engineered to be:

* **Stable.** Each :class:`CheckResult` names exactly one
  :class:`ConformanceCheck`, so a CI gate can assert on individual checks (e.g.
  "every adapter must pass CAPABILITY_HONESTY") rather than a single boolean.
* **Diagnosable.** A failed check carries a human-readable ``detail`` and, where
  relevant, the offending mode/duration/resolution so the failure points at the
  exact claim the adapter could not back up.
* **Machine-graded.** ``passed`` / ``score`` / ``summary`` drive the CLI exit
  code and any dashboard, without re-deriving anything from prose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConformanceCheck(StrEnum):
    """The categories of behaviour every trusted video adapter must satisfy.

    These map one-to-one onto the task's required guarantees. The ordering is
    the order the runner executes them (cheapest / most foundational first).
    """

    #: The adapter exposes the required surface (``name``/``capabilities``/``render``).
    SURFACE = "surface"
    #: The capability declaration is internally well-formed (non-empty, consistent).
    CAPABILITY_DECLARATION = "capability_declaration"
    #: Every declared mode/duration/resolution is actually supported, and every
    #: *undeclared* one is rejected. The cardinal check.
    CAPABILITY_HONESTY = "capability_honesty"
    #: The canonical ``WanSpec`` round-trips through the adapter's native request
    #: mapping without losing or corrupting fields.
    REQUEST_MAPPING = "request_mapping"
    #: Transport/HTTP/timeout/quota faults map to the shared error taxonomy.
    ERROR_TAXONOMY = "error_taxonomy"
    #: ``render`` returns real bytes and eagerly downloads expiring URLs.
    ASSET_HANDLING = "asset_handling"
    #: A continuation-capable adapter extracts a usable last frame.
    LAST_FRAME = "last_frame"
    #: Re-submitting the same spec does not double-spend (idempotency).
    IDEMPOTENCY = "idempotency"
    #: An in-flight task can be cancelled and reports a canceled terminal state.
    CANCELLATION = "cancellation"
    #: A task that never completes surfaces a ``ProviderTimeout`` (not a hang).
    TIMEOUT = "timeout"
    #: The spend gate (``LiveVideoDisabled``) is honoured and never miscounted.
    SPEND_GATE = "spend_gate"


class CheckOutcome(StrEnum):
    """The result of running one :class:`ConformanceCheck`."""

    PASS = "pass"
    FAIL = "fail"
    #: The check does not apply to this adapter (e.g. cancellation on an adapter
    #: that does not declare ``cancellable``). Skips never fail a report.
    SKIP = "skip"
    #: The harness itself errored running the check (a bug in the harness, not
    #: necessarily the adapter). Treated as a failure for the verdict.
    ERROR = "error"


class CheckResult(BaseModel):
    """The outcome of one conformance check, with enough context to diagnose it."""

    model_config = ConfigDict(frozen=True)

    check: ConformanceCheck
    outcome: CheckOutcome
    #: One-line human summary of what was verified (or why it failed/skipped).
    detail: str = ""
    #: The specific claim under test, when the check iterates claims (e.g.
    #: ``"mode=image_to_video"`` / ``"duration_s=20"`` / ``"resolution=480P"``).
    subject: str | None = None

    @property
    def ok(self) -> bool:
        """True when this result does not count against the verdict."""
        return self.outcome in (CheckOutcome.PASS, CheckOutcome.SKIP)


class ConformanceReport(BaseModel):
    """The full, scored verdict for one adapter across every conformance check."""

    model_config = ConfigDict(frozen=True)

    provider_id: str
    #: ISO-8601 UTC stamp of when the run finished (deterministic in tests via
    #: an injected ``now`` — see the runner).
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    results: list[CheckResult] = Field(default_factory=list)

    # -- verdict --------------------------------------------------------- #

    @property
    def passed(self) -> bool:
        """True iff no check FAILED or ERRORED (skips are fine)."""
        return all(result.ok for result in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        """Every result that counts against the verdict, in run order."""
        return [r for r in self.results if not r.ok]

    @property
    def executed(self) -> list[CheckResult]:
        """Results that actually ran (PASS / FAIL / ERROR — not SKIP)."""
        return [r for r in self.results if r.outcome is not CheckOutcome.SKIP]

    @property
    def score(self) -> float:
        """Fraction of *executed* checks that passed (1.0 when none ran)."""
        ran = self.executed
        if not ran:
            return 1.0
        passed = sum(1 for r in ran if r.outcome is CheckOutcome.PASS)
        return passed / len(ran)

    def result_for(self, check: ConformanceCheck) -> CheckResult | None:
        """The (first) result recorded for ``check``, or ``None`` if never run."""
        for result in self.results:
            if result.check is check:
                return result
        return None

    def summary(self) -> str:
        """A compact one-line verdict for logs / CLI output."""
        verdict = "PASS" if self.passed else "FAIL"
        ran = self.executed
        n_pass = sum(1 for r in ran if r.outcome is CheckOutcome.PASS)
        return (
            f"[{verdict}] {self.provider_id}: {n_pass}/{len(ran)} checks "
            f"({self.score:.0%}); {len(self.failures)} failing"
        )

    def render_text(self) -> str:
        """A multi-line human report (used by the CLI)."""
        lines = [self.summary(), ""]
        glyph = {
            CheckOutcome.PASS: "PASS",
            CheckOutcome.FAIL: "FAIL",
            CheckOutcome.SKIP: "SKIP",
            CheckOutcome.ERROR: " ERR",
        }
        for result in self.results:
            subject = f" ({result.subject})" if result.subject else ""
            lines.append(
                f"  {glyph[result.outcome]}  {result.check.value}{subject}"
                f"{' — ' + result.detail if result.detail else ''}"
            )
        return "\n".join(lines)


__all__ = [
    "CheckOutcome",
    "CheckResult",
    "ConformanceCheck",
    "ConformanceReport",
]
