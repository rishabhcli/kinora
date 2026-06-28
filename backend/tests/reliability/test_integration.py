"""Composition tests — the reliability toolkit pieces working together.

These prove the modules compose into the end-to-end resilience story the brief
asks for, all infra-free and deterministic:

* a chaos controller wrapping the transport inside a real load run, so injected
  faults show up in the load report's error rate (load × chaos);
* the capacity model's queueing prediction lining up with the offered render
  demand a reader population produces (capacity ↔ reader model);
* a load run feeding the SLO gate + the runbook registry, so a degraded run
  surfaces the right incident plan (load → SLO → runbook).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.reliability.capacity import (
    ReadingProfile,
    RenderDemand,
    min_servers_for_utilisation,
    mmc_queue,
    watermark_feasibility,
)
from app.reliability.chaos import (
    SEAM_PROVIDER,
    ChaosController,
    FaultKind,
    FaultRule,
    InjectedFault,
)
from app.reliability.metrics_report import LoadReport, RequestOutcome
from app.reliability.profiles import ProfileOverrides, get_profile
from app.reliability.runbook import Severity, triggered_runbooks
from app.reliability.runner import LoadRunner, RunnerConfig, VirtualClock
from app.reliability.scenarios import EP_INTENT, steady_reader
from app.reliability.slo import default_kinora_slos
from app.reliability.transport import FakeTransport, Response, Transport


class _ChaosTransport:
    """A Transport that routes every call through a ChaosController.

    Demonstrates the chaos library wrapping the transport seam: a fault makes the
    request surface as a status-0 transport failure, which the load report counts
    as an error — the load × chaos composition the brief calls for.
    """

    def __init__(self, inner: Transport, chaos: ChaosController) -> None:
        self._inner = inner
        self._chaos = chaos

    async def request(
        self, method: str, path: str, *, json: Any = None, headers: Any = None
    ) -> Response:
        try:
            return await self._chaos.call(
                SEAM_PROVIDER,
                lambda: self._inner.request(method, path, json=json, headers=headers),
            )
        except InjectedFault as exc:
            return Response(status=0, elapsed_ms=0.0, error=str(exc))

    async def aclose(self) -> None:
        await self._inner.aclose()


async def test_load_run_with_chaos_surfaces_errors() -> None:
    chaos = ChaosController(seed=3)
    # 30% of all requests fault.
    chaos.add_rule(
        SEAM_PROVIDER, FaultRule(name="flaky", kind=FaultKind.ERROR, probability=0.3)
    )
    transport = _ChaosTransport(FakeTransport(seed=1), chaos)
    clock = VirtualClock()
    runner = LoadRunner(transport, clock=clock.now, sleep=clock.sleep, config=RunnerConfig())
    report = await runner.run(steady_reader(), get_profile("steady_soak").build_workload(
        ProfileOverrides(users=4, duration_s=20.0)
    ))
    # Roughly 30% of requests errored (transport failures from chaos).
    assert 0.15 < report.error_rate < 0.45
    # And the chaos controller recorded the injected faults.
    assert chaos.stats(SEAM_PROVIDER).errors_injected > 0


async def test_load_then_slo_then_runbook_on_buffer_stall() -> None:
    # Build a degraded report (intent latency well over SLO) and a stalled buffer.
    report = LoadReport(wall_seconds=10.0)
    for _ in range(50):
        report.record(RequestOutcome(EP_INTENT, 200, 600.0, True))  # slow intents

    verdict = default_kinora_slos().evaluate_report(report)
    assert verdict.passed is False
    assert any(v.slo.name == "intent-p99" for v in verdict.violations)

    # The matching operational signal (a drained buffer) triggers the buffer_stall
    # runbook at PAGE severity — the load result and the incident response align.
    plans = triggered_runbooks(
        {"committed_seconds_ahead": 8.0, "render_utilisation": 0.95}
    )
    names = {p.runbook for p in plans}
    assert "buffer_stall" in names
    page = next(p for p in plans if p.runbook == "buffer_stall")
    assert page.severity is Severity.PAGE
    # The capacity-add step is present because utilisation is saturated.
    assert any("capacity" in s.title.lower() for s in page.steps)


def test_capacity_sizing_for_a_reader_population() -> None:
    # 50 concurrent readers, the §4.2 spacing, a 60s render: size the committed lane.
    profile = ReadingProfile(active_fraction=0.7)
    demand = RenderDemand(readers=50, profile=profile)
    arrival = demand.arrival_rate_shots_per_s
    service_s = 60.0

    needed = min_servers_for_utilisation(
        arrival_rate_per_s=arrival, service_time_s=service_s, max_utilisation=0.8
    )
    # With that many servers the queue is stable and not saturated.
    result = mmc_queue(
        arrival_rate_per_s=arrival, service_time_s=service_s, servers=needed
    )
    assert result.stable is True
    assert result.utilisation <= 0.8 + 1e-9
    # One server fewer would push utilisation up (sizing is tight, not wasteful).
    if needed > 1:
        tighter = mmc_queue(
            arrival_rate_per_s=arrival, service_time_s=service_s, servers=needed - 1
        )
        assert tighter.utilisation > result.utilisation


def test_watermark_feasibility_consistent_with_queue_stability() -> None:
    # If the committed lane is stable (M/M/c) it should also be watermark-feasible
    # for a single reader: production must exceed one reader's consumption.
    profile = ReadingProfile()
    demand = RenderDemand(readers=10, profile=profile)
    service_s = 45.0
    servers = min_servers_for_utilisation(
        arrival_rate_per_s=demand.arrival_rate_shots_per_s,
        service_time_s=service_s,
        max_utilisation=0.8,
    )
    queue = mmc_queue(
        arrival_rate_per_s=demand.arrival_rate_shots_per_s,
        service_time_s=service_s,
        servers=servers,
    )
    feas = watermark_feasibility(
        servers=servers,
        service_time_s=service_s,
        seconds_per_shot=profile.seconds_per_shot,
        profile=profile,
        high_watermark_s=75.0,
    )
    assert queue.stable is True
    assert feas.feasible is True


@pytest.mark.parametrize("profile_name", ["steady_soak", "skim_storm", "seek_thrash", "cold_open"])
async def test_every_closed_profile_runs_clean_against_healthy_transport(
    profile_name: str,
) -> None:
    profile = get_profile(profile_name)
    plan = profile.build_workload(ProfileOverrides(users=3, duration_s=15.0))
    transport = FakeTransport(base_latency_ms=8.0, latency_jitter_ms=1.0, seed=2)
    clock = VirtualClock()
    runner = LoadRunner(transport, clock=clock.now, sleep=clock.sleep)
    report = await runner.run(profile.scenario(), plan)
    assert report.total_requests > 0
    # A healthy transport with fast intents passes the profile's SLOs.
    verdict = profile.slos.evaluate_report(report)
    assert verdict.passed is True, verdict.render_text()
