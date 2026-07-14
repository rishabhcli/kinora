"""Synthetic monitoring — scripted critical-journey probes (kinora.md §13/§5.6).

A canary is a tiny, *scripted* run of the one journey that must always work: the
reader logs in, sees their library, opens a prepared book, reads (an intent
tick), and seeks. Unlike load testing (which floods), a canary issues one request
per step and asserts an SLA on each — latency under a budget, the right status,
an expected field in the body — so it doubles as both a smoke test and a
continuous availability probe.

It runs against the same :class:`~app.reliability.transport.Transport` seam, so a
unit test drives the journey with a :class:`FakeTransport` (asserting the step
sequence + the SLA verdicts) and the CLI runs it against a real ``--target``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.reliability.transport import Response, Transport

#: A body assertion: returns ``None`` if ok, else a failure reason.
BodyCheck = Callable[[object], str | None]
#: A monotonic clock seam (seconds). Production: ``time.monotonic``.
ClockFn = Callable[[], float]


@dataclass(frozen=True, slots=True)
class JourneyStep:
    """One scripted step of a critical journey + its SLA.

    ``build`` takes the mutable journey context (a dict of state captured from
    prior steps, e.g. a token or a session id) and returns the request tuple
    ``(method, path, json)``. ``capture`` (optional) extracts state from the
    response body into the context for later steps. ``sla_ms`` bounds the step's
    latency; ``expect_status`` and ``body_check`` validate the response.
    """

    name: str
    build: Callable[[dict[str, object]], tuple[str, str, dict[str, object] | None]]
    sla_ms: float = 1000.0
    expect_status: tuple[int, ...] = (200,)
    body_check: BodyCheck | None = None
    capture: Callable[[dict[str, object], object], None] | None = None


@dataclass(frozen=True, slots=True)
class StepResult:
    """The outcome of one journey step against its SLA."""

    name: str
    status: int
    latency_ms: float
    passed: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """JSON projection of the step result."""
        return {
            "name": self.name,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 3),
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CanaryResult:
    """The verdict of a whole journey probe."""

    journey: str
    passed: bool
    steps: tuple[StepResult, ...]
    total_latency_ms: float

    @property
    def failures(self) -> list[StepResult]:
        """The steps that failed (the ones to alert on)."""
        return [s for s in self.steps if not s.passed]

    def to_dict(self) -> dict[str, object]:
        """JSON projection of the canary result."""
        return {
            "journey": self.journey,
            "passed": self.passed,
            "total_latency_ms": round(self.total_latency_ms, 3),
            "steps": [s.to_dict() for s in self.steps],
        }

    def render_text(self) -> str:
        """A compact pass/fail journey report."""
        lines = [
            f"Canary '{self.journey}': {'PASS' if self.passed else 'FAIL'} "
            f"({self.total_latency_ms:.0f}ms total)"
        ]
        for s in self.steps:
            mark = "ok " if s.passed else "FAIL"
            extra = f"  ({'; '.join(s.reasons)})" if s.reasons else ""
            lines.append(
                f"  [{mark}] {s.name:<20} {s.status:>4}  {s.latency_ms:>7.1f}ms{extra}"
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Journey:
    """A named, ordered sequence of critical-journey steps."""

    name: str
    steps: Sequence[JourneyStep]


class CanaryRunner:
    """Executes a :class:`Journey` against a transport, asserting each SLA.

    The journey runs steps in order, threading a shared context dict so a later
    step can use state captured from an earlier one (the token, the session id).
    A step that fails its status/SLA check stops the journey (a broken login
    means the rest is meaningless) and the canary reports failed.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        clock: ClockFn,
        stop_on_failure: bool = True,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._stop_on_failure = stop_on_failure

    async def run(
        self, journey: Journey, *, context: dict[str, object] | None = None
    ) -> CanaryResult:
        """Run the journey and return its pass/fail verdict with per-step SLAs."""
        ctx: dict[str, object] = dict(context or {})
        results: list[StepResult] = []
        total = 0.0
        passed_all = True
        for step in journey.steps:
            method, path, body = step.build(ctx)
            resp = await self._timed_request(method, path, body)
            total += resp.elapsed_ms
            result = self._evaluate(step, resp)
            results.append(result)
            if result.passed and step.capture is not None and resp.body is not None:
                step.capture(ctx, resp.body)
            if not result.passed:
                passed_all = False
                if self._stop_on_failure:
                    break
        return CanaryResult(
            journey=journey.name,
            passed=passed_all,
            steps=tuple(results),
            total_latency_ms=total,
        )

    async def _timed_request(
        self, method: str, path: str, body: dict[str, object] | None
    ) -> Response:
        return await self._transport.request(method, path, json=body)

    @staticmethod
    def _evaluate(step: JourneyStep, resp: Response) -> StepResult:
        reasons: list[str] = []
        if resp.status not in step.expect_status:
            reasons.append(
                f"status {resp.status} not in {step.expect_status}"
                + (f" ({resp.error})" if resp.error else "")
            )
        if resp.elapsed_ms > step.sla_ms:
            reasons.append(f"latency {resp.elapsed_ms:.0f}ms > SLA {step.sla_ms:.0f}ms")
        if step.body_check is not None and resp.body is not None:
            problem = step.body_check(resp.body)
            if problem:
                reasons.append(problem)
        return StepResult(
            name=step.name,
            status=resp.status,
            latency_ms=resp.elapsed_ms,
            passed=not reasons,
            reasons=tuple(reasons),
        )


# --------------------------------------------------------------------------- #
# The standard Kinora critical journey (the §5.6 read-along path)
# --------------------------------------------------------------------------- #


def _login_step(email: str, password: str, *, sla_ms: float) -> JourneyStep:
    def _capture(ctx: dict[str, object], body: object) -> None:
        if isinstance(body, dict) and "access_token" in body:
            ctx["token"] = body["access_token"]

    def _check(body: object) -> str | None:
        if not (isinstance(body, dict) and body.get("access_token")):
            return "login response missing access_token"
        return None

    return JourneyStep(
        name="login",
        build=lambda ctx: ("POST", "/auth/login", {"email": email, "password": password}),
        sla_ms=sla_ms,
        body_check=_check,
        capture=_capture,
    )


def _create_session_step(book_id: str, *, sla_ms: float) -> JourneyStep:
    def _capture(ctx: dict[str, object], body: object) -> None:
        if isinstance(body, dict) and "session_id" in body:
            ctx["session_id"] = body["session_id"]

    return JourneyStep(
        name="open_session",
        build=lambda ctx: ("POST", "/sessions", {"book_id": book_id, "focus_word": 0}),
        sla_ms=sla_ms,
        expect_status=(200, 201),
        capture=_capture,
    )


def kinora_read_journey(
    *,
    email: str = "demo@kinora.local",
    password: str = "demo-password-123",  # noqa: S107 - demo creds, documented in AGENTS.md
    book_id: str = "book_demo",
    library_sla_ms: float = 800.0,
    intent_sla_ms: float = 250.0,
    seek_sla_ms: float = 150.0,
) -> Journey:
    """The §5.6 critical journey: login → library → open → read → seek.

    SLA defaults mirror the §4 control-plane budgets: intent must stay snappy so
    the buffer keeps up (§4.9), seek must bridge ≈instantly (§4.8). The session
    id captured at ``open_session`` is threaded into the read/seek steps.
    """

    def _session_path(ctx: dict[str, object], suffix: str) -> str:
        sid = ctx.get("session_id", "sess_canary")
        return f"/sessions/{sid}{suffix}"

    return Journey(
        name="kinora_read",
        steps=(
            _login_step(email, password, sla_ms=800.0),
            JourneyStep(
                name="library",
                build=lambda ctx: ("GET", "/books", None),
                sla_ms=library_sla_ms,
            ),
            _create_session_step(book_id, sla_ms=500.0),
            JourneyStep(
                name="read_intent",
                build=lambda ctx: (
                    "POST",
                    _session_path(ctx, "/intent"),
                    {"focus_word": 120, "velocity": 4.0, "mode": "viewer"},
                ),
                sla_ms=intent_sla_ms,
            ),
            JourneyStep(
                name="seek",
                build=lambda ctx: ("POST", _session_path(ctx, "/seek"), {"word": 9000}),
                sla_ms=seek_sla_ms,
            ),
        ),
    )


__all__ = [
    "BodyCheck",
    "CanaryResult",
    "CanaryRunner",
    "Journey",
    "JourneyStep",
    "StepResult",
    "kinora_read_journey",
]
