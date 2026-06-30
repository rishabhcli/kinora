"""Reusable pytest helpers for the golden video-adapter contract suite.

A new adapter earns trust by passing this suite — not by hand-writing bespoke
tests. Two entry points cover both styles:

* :func:`run_provider_conformance` — run the full suite and return the scored
  :class:`~app.video.conformance.report.ConformanceReport` for fine-grained
  assertions (e.g. "this adapter is allowed to skip cancellation, but must pass
  capability honesty").
* :func:`assert_conformant` — the one-liner: run the suite and ``assert`` the
  report passes, raising :class:`AssertionError` with the full per-check report
  on failure so a CI log points straight at the violated guarantee.

To plug a real adapter in, supply a ``rebuild(script) -> provider`` that
constructs it bound to a :class:`~app.video.conformance.transport.ScriptedTransport`
(see :mod:`.fakes` for the pattern), plus an optional gate-closed ``rebuild_gated``
so the spend-gate check can verify the deliberate ``LiveVideoDisabled`` path.

These helpers contain **no** ``@pytest.mark`` themselves — they are building
blocks a test module calls (often parametrised over several adapters), so the
suite stays a library, importable without pytest collecting it twice.
"""

from __future__ import annotations

from datetime import datetime

from .protocol import ConformantVideoProvider
from .report import ConformanceCheck, ConformanceReport
from .runner import ProviderFactory, run_conformance


async def run_provider_conformance(
    provider: ConformantVideoProvider,
    *,
    rebuild: ProviderFactory | None = None,
    rebuild_gated: ProviderFactory | None = None,
    now: datetime | None = None,
) -> ConformanceReport:
    """Run the golden contract suite against ``provider`` and return the report.

    A thin, intention-revealing alias for
    :func:`~app.video.conformance.runner.run_conformance` so test modules import
    the suite, not the runner internals. See that function for the argument
    semantics.
    """
    return await run_conformance(
        provider, rebuild=rebuild, rebuild_gated=rebuild_gated, now=now
    )


async def assert_conformant(
    provider: ConformantVideoProvider,
    *,
    rebuild: ProviderFactory | None = None,
    rebuild_gated: ProviderFactory | None = None,
    required: set[ConformanceCheck] | None = None,
) -> ConformanceReport:
    """Assert ``provider`` passes the contract; raise with the full report if not.

    Args:
        provider / rebuild / rebuild_gated: As for
            :func:`~app.video.conformance.runner.run_conformance`.
        required: When given, only these checks are *required* to pass — any
            other failure is reported but tolerated. Use this for an adapter that
            legitimately does not implement an optional capability it never
            claims (though a well-formed adapter SKIPs those rather than FAILing,
            so this is rarely needed). When ``None`` (the default) every executed
            check must pass.

    Returns:
        The :class:`ConformanceReport` (so callers can make extra assertions).

    Raises:
        AssertionError: if a required check did not pass, with the rendered
            multi-line report as the message.
    """
    report = await run_conformance(provider, rebuild=rebuild, rebuild_gated=rebuild_gated)
    if required is None:
        offending = report.failures
    else:
        offending = [r for r in report.failures if r.check in required]
    if offending:
        names = ", ".join(sorted({r.check.value for r in offending}))
        raise AssertionError(
            f"video adapter {report.provider_id!r} failed conformance "
            f"({names}):\n{report.render_text()}"
        )
    return report


__all__ = [
    "assert_conformant",
    "run_provider_conformance",
]
