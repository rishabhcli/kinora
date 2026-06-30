"""A provider conformance test harness + golden contract suite for video adapters.

Every video adapter Kinora trusts — the hosted DashScope Wan provider, the
MiniMax (Hailuo) provider, a future self-hosted lane, or any adapter from any
model family — must pass the **same** golden contract before it is wired into
the render pipeline. This package is that contract, made executable.

What it verifies (one :class:`~app.video.conformance.report.ConformanceCheck`
each):

* **capability-declaration honesty** — an adapter actually supports every
  mode/duration/resolution it claims, and rejects everything it does not, as
  verified against a fake transport scripted to its declared profile;
* **canonical ↔ native request mapping** — a :class:`~app.providers.types.WanSpec`
  round-trips through the adapter's native request body without losing fields;
* **error taxonomy** — transport / HTTP / timeout / quota faults map onto the
  shared :mod:`app.providers.errors` classes with correct retryability;
* **asset handling** — ``render`` returns real bytes and eagerly downloads
  expiring URLs, extracting a last frame when it claims to;
* **idempotency / retry safety**, **cancellation**, and **timeout** behaviour;
* the **spend gate** (``LiveVideoDisabled``) is honoured and never miscounted.

Public surface:

* :func:`~app.video.conformance.runner.run_conformance` — the programmatic
  runner: ``await run_conformance(provider) -> ConformanceReport``.
* :class:`~app.video.conformance.report.ConformanceReport` /
  :class:`~app.video.conformance.report.ConformanceCheck` — the scored verdict.
* :class:`~app.video.conformance.protocol.ConformantVideoProvider` /
  :class:`~app.video.conformance.protocol.VideoCapabilities` — the local
  provider contract the harness verifies (mirrors ``VideoBackend`` + the hosted
  submit→poll→fetch lifecycle, owned here so the harness never hard-blocks on a
  sibling agent's in-flight provider refactor).
* :func:`~app.video.conformance.suite.run_provider_conformance` /
  :func:`~app.video.conformance.suite.assert_conformant` — the pytest helpers.
* :mod:`~app.video.conformance.fakes` — a reference fake that PASSES and a family
  of deliberately-broken fakes that each FAIL one specific check, used to prove
  the harness catches every violation.

CLI: ``python -m app.video.conformance <provider_id>`` runs the suite against a
named built-in fake and prints a human report (exit 0 = pass, 1 = fail).
"""

from __future__ import annotations

from .protocol import (
    ConformantVideoProvider,
    DurationBounds,
    SubmittedTask,
    TaskStatus,
    VideoCapabilities,
)
from .report import (
    CheckOutcome,
    CheckResult,
    ConformanceCheck,
    ConformanceReport,
)
from .runner import ProviderFactory, run_conformance
from .suite import assert_conformant, run_provider_conformance

__all__ = [
    "CheckOutcome",
    "CheckResult",
    "ConformanceCheck",
    "ConformanceReport",
    "ConformantVideoProvider",
    "DurationBounds",
    "ProviderFactory",
    "SubmittedTask",
    "TaskStatus",
    "VideoCapabilities",
    "assert_conformant",
    "run_conformance",
    "run_provider_conformance",
]
