"""Reliability engineering toolkit (kinora.md §4, §12).

Reusable, **infra-free**, deterministic models and probes for load generation,
chaos injection, synthetic monitoring, capacity planning, and runbooks-as-code.
The CLI load *runner* lives in the top-level ``loadtest/`` package and drives
these models against an explicitly-provided target URL; everything here is pure
given its collaborators so it can be unit-tested with a fake transport, an
injected clock, and a seeded RNG — no Redis/Postgres/DashScope, zero model spend.

Public surface (imported lazily by callers to keep import cost low):

* :mod:`app.reliability.latency` — a streaming latency digest (percentiles).
* :mod:`app.reliability.metrics_report` — throughput / error / latency reports.
* :mod:`app.reliability.reader_model` — the §4.3/§4.7 reader as a state machine.
* :mod:`app.reliability.workload` — open/closed workload models + ramp profiles.
* :mod:`app.reliability.transport` — the request transport seam (real + fake).
* :mod:`app.reliability.scenarios` — named realistic-reader load scenarios.
* :mod:`app.reliability.chaos` — latency/fault/partition injection at the seams.
* :mod:`app.reliability.capacity` — Little's-law / queueing capacity math.
* :mod:`app.reliability.canary` — synthetic critical-journey probes + SLA asserts.
* :mod:`app.reliability.slo` — SLO / error-budget / burn-rate math.
* :mod:`app.reliability.runbook` — runbooks-as-code + the Kinora incident registry.
"""

from __future__ import annotations

__all__: list[str] = []
