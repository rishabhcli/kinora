"""Kinora load-test runner (kinora.md §4/§12).

The CLI surface for the reliability toolkit: it drives the
:mod:`app.reliability` models (reader behaviour, workload, scenarios) against an
**explicitly-provided** target URL via a real HTTP transport, collecting a
latency-percentile + throughput + error report and gating it against SLOs.

Hard rule: load is only ever run by a human via ``python -m loadtest --target
<url>``. The unit tests drive the runner with a :class:`~app.reliability.transport.FakeTransport`
and a virtual clock, so the test process issues no network traffic.

Modules:

* :mod:`loadtest.runner` — the async load engine (open + closed models).
* :mod:`loadtest.profiles` — named run profiles (scenario + workload + ramp).
* :mod:`loadtest.report` — console + JSON rendering and the SLO gate.
* :mod:`loadtest.__main__` — the ``python -m loadtest`` CLI.
* :mod:`loadtest.canary_cli` — the synthetic-monitoring journey CLI.
"""

from __future__ import annotations

__all__: list[str] = []
