"""Typed findings the config-management plane produces.

Every check in :mod:`app.configmgmt` reports its result as a
:class:`ConfigFinding` rather than raising — so a single pass can collect *all*
problems and present them together (a partial failure that aborts on the first
bad setting hides the rest). The only place this plane *raises* is the
production-safety gate (:mod:`app.configmgmt.safety`), which converts a fatal
finding into a hard :class:`ProdSafetyError` so an unsafe process refuses to
boot.

The vocabulary is deliberately small and stable:

* :class:`Severity` — ordered ``INFO < WARNING < ERROR < FATAL``.
* :class:`ConfigFinding` — one immutable observation: a code, a human message,
  the offending settings field(s), and a remediation hint.
* :class:`ProdSafetyError` — the exception the safety gate raises.

Nothing here touches the network, a clock, or global state; findings are values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Severity",
    "ConfigFinding",
    "ProdSafetyError",
]


class Severity(IntEnum):
    """Ordered finding severity.

    ``IntEnum`` so findings sort and ``max(...)`` works for a roll-up verdict.
    ``INFO`` is advisory, ``WARNING`` is "boot but be aware", ``ERROR`` means the
    config is misconfigured for the requested mode (readiness fails), and
    ``FATAL`` is a production-safety violation that must refuse to boot.
    """

    INFO = 10
    WARNING = 20
    ERROR = 30
    FATAL = 40

    @property
    def label(self) -> str:
        """Lower-case name (e.g. ``"warning"``) for logs / JSON."""
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class ConfigFinding:
    """One immutable observation about the live configuration.

    Args:
        code: A stable machine code (e.g. ``"live_video.missing_key"``). Tests
            and dashboards key off this, so it never changes wording-by-wording.
        severity: How bad this is (see :class:`Severity`).
        message: A human-readable, single-sentence explanation.
        fields: The settings field name(s) the finding concerns (for pointing an
            operator at the exact knob). Empty when cross-cutting.
        hint: Optional remediation guidance ("set X" / "unset Y").
    """

    code: str
    severity: Severity
    message: str
    fields: tuple[str, ...] = ()
    hint: str | None = None

    @property
    def is_blocking(self) -> bool:
        """True when this finding blocks a clean boot (``ERROR``/``FATAL``)."""
        return self.severity >= Severity.ERROR

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly projection (for ``redacted_dump`` / report surfaces)."""
        out: dict[str, object] = {
            "code": self.code,
            "severity": self.severity.label,
            "message": self.message,
        }
        if self.fields:
            out["fields"] = list(self.fields)
        if self.hint:
            out["hint"] = self.hint
        return out

    def __str__(self) -> str:
        where = f" [{', '.join(self.fields)}]" if self.fields else ""
        tail = f" — {self.hint}" if self.hint else ""
        return f"{self.severity.label.upper()} {self.code}{where}: {self.message}{tail}"


class ProdSafetyError(RuntimeError):
    """Raised by the production-safety gate to refuse an unsafe boot.

    Carries the fatal findings so the operator sees *every* reason at once rather
    than fixing one and re-discovering the next.
    """

    def __init__(self, findings: tuple[ConfigFinding, ...] = ()) -> None:
        self.findings = findings
        lines = "\n".join(f"  - {f}" for f in findings)
        super().__init__(
            f"refusing to boot: {len(findings)} production-safety " f"violation(s):\n{lines}"
        )
