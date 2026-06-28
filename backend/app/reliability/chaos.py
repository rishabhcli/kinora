"""Chaos injection at the provider / redis / db seams (kinora.md §12.1/§4.11).

kinora.md §4.11 lists the failures the system must absorb — a render fails
repeatedly (→ DLQ → Ken-Burns), the provider rate-limits, a seek strands
in-flight work — and the queue (§12.1) is built to survive them. To *test* that
resilience you need to make those failures happen on demand, deterministically.

This module is a small chaos engine: :class:`ChaosController` holds a seeded RNG
and a list of :class:`FaultRule`, and :meth:`ChaosController.wrap` returns an
async wrapper around *any* awaitable that, per call, may add latency, raise an
injected error, or simulate a partition (a hang that surfaces as a timeout-style
error). Because the seam types in Kinora are all simple async callables
(provider ``call``, redis ``get_json``/``publish``, blob ``get_bytes``), one
generic wrapper covers them all, and named helpers document the intent.

Determinism is the whole point: given a seed, the exact sequence of injected
faults is reproducible, so a resilience test asserts a *specific* failure
pattern (e.g. "the 3rd and 4th provider calls 429, the rest succeed") rather
than flaking. No real sleeping happens unless a real ``sleep`` is injected; the
default advances an injected clock only.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")

#: An async sleep seam (so tests inject an instant/virtual sleep). Default: none.
SleepFn = Callable[[float], Awaitable[None]]


class FaultKind(StrEnum):
    """The kind of disruption a rule injects."""

    LATENCY = "latency"  # add delay, then proceed normally
    ERROR = "error"  # raise an injected exception instead of calling through
    PARTITION = "partition"  # simulate a network partition: a timeout-style error


class InjectedFault(RuntimeError):  # noqa: N818 - it *is* a fault, name is intentional
    """The exception a chaos rule raises (carries the seam + rule for assertions)."""

    def __init__(self, seam: str, rule_name: str, message: str) -> None:
        super().__init__(message)
        self.seam = seam
        self.rule_name = rule_name


@dataclass(frozen=True, slots=True)
class FaultRule:
    """A probabilistic disruption applied to a seam's calls.

    ``probability`` is the per-call chance the rule activates. ``latency_ms`` adds
    delay (LATENCY, or before a PARTITION timeout). ``exc_factory`` builds the
    error raised for ERROR/PARTITION (defaults to :class:`InjectedFault`).
    ``only_call_indices`` restricts the rule to specific call ordinals (0-based)
    so a test can target "fail the 3rd call exactly".
    """

    name: str
    kind: FaultKind
    probability: float = 1.0
    latency_ms: float = 0.0
    exc_factory: Callable[[str, str], Exception] | None = None
    only_call_indices: frozenset[int] | None = None

    def applies_to_index(self, index: int) -> bool:
        """Whether this rule is eligible for the ``index``-th call."""
        return self.only_call_indices is None or index in self.only_call_indices

    def build_error(self, seam: str) -> Exception:
        """Construct the exception this rule raises."""
        if self.exc_factory is not None:
            return self.exc_factory(seam, self.name)
        verb = "partitioned" if self.kind is FaultKind.PARTITION else "fault"
        return InjectedFault(seam, self.name, f"chaos {verb} on {seam} ({self.name})")


@dataclass
class SeamCounters:
    """Per-seam observability the chaos engine records (for test assertions)."""

    calls: int = 0
    latency_injections: int = 0
    errors_injected: int = 0
    partitions_injected: int = 0
    total_injected_latency_ms: float = 0.0


@dataclass
class ChaosController:
    """A deterministic fault injector around async seam calls.

    Register :class:`FaultRule` s, then wrap a seam call site with
    :meth:`wrap` / :meth:`call`. The controller decides, per call and per rule,
    whether to inject — using its own seeded RNG so the whole run is reproducible.
    """

    seed: int = 1337
    sleep: SleepFn | None = None
    rules: dict[str, list[FaultRule]] = field(default_factory=dict)
    counters: dict[str, SeamCounters] = field(default_factory=dict)
    _rng: random.Random = field(init=False, repr=False)
    _index: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # -- rule registration --------------------------------------------------- #

    def add_rule(self, seam: str, rule: FaultRule) -> ChaosController:
        """Register ``rule`` for the named ``seam`` (chainable)."""
        self.rules.setdefault(seam, []).append(rule)
        return self

    def _counters(self, seam: str) -> SeamCounters:
        c = self.counters.get(seam)
        if c is None:
            c = SeamCounters()
            self.counters[seam] = c
        return c

    # -- the core: wrap one seam call --------------------------------------- #

    async def call(
        self, seam: str, fn: Callable[[], Awaitable[T]]
    ) -> T:
        """Invoke ``fn`` for the named ``seam``, injecting faults per the rules.

        Decision order, per matching rule (first match wins): an ERROR/PARTITION
        rule raises (and never calls through); a LATENCY rule delays then proceeds
        to call ``fn``. A call with no firing rule simply calls ``fn``.
        """
        counters = self._counters(seam)
        index = self._index.get(seam, 0)
        self._index[seam] = index + 1
        counters.calls += 1

        latency_ms = 0.0
        for rule in self.rules.get(seam, ()):
            if not rule.applies_to_index(index):
                continue
            if self._rng.random() >= rule.probability:
                continue
            if rule.kind is FaultKind.LATENCY:
                latency_ms += rule.latency_ms
                counters.latency_injections += 1
                continue
            # ERROR or PARTITION: delay (if any) then raise.
            if rule.latency_ms > 0.0:
                latency_ms += rule.latency_ms
            await self._maybe_sleep(latency_ms)
            counters.total_injected_latency_ms += latency_ms
            if rule.kind is FaultKind.PARTITION:
                counters.partitions_injected += 1
            else:
                counters.errors_injected += 1
            raise rule.build_error(seam)

        if latency_ms > 0.0:
            await self._maybe_sleep(latency_ms)
            counters.total_injected_latency_ms += latency_ms
        return await fn()

    def wrap(self, seam: str, fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        """Return a drop-in async wrapper around ``fn`` for the named ``seam``.

        The wrapper forwards ``*args, **kwargs`` to ``fn`` and applies chaos, so
        a resilience test can swap a real seam (``provider.call``) for
        ``chaos.wrap("provider", provider.call)`` with no other change.
        """

        async def _wrapped(*args: Any, **kwargs: Any) -> T:
            return await self.call(seam, lambda: fn(*args, **kwargs))

        return _wrapped

    async def _maybe_sleep(self, ms: float) -> None:
        if ms > 0.0 and self.sleep is not None:
            await self.sleep(ms / 1000.0)

    # -- introspection ------------------------------------------------------- #

    def stats(self, seam: str) -> SeamCounters:
        """The recorded counters for a seam (a fresh zero record if unseen)."""
        return self.counters.get(seam, SeamCounters())

    def reset(self) -> None:
        """Reset counters + call ordinals (re-seeds the RNG to the original seed)."""
        self.counters.clear()
        self._index.clear()
        self._rng = random.Random(self.seed)


# --------------------------------------------------------------------------- #
# Named seam labels + convenience rule builders (documenting intent)
# --------------------------------------------------------------------------- #

SEAM_PROVIDER = "provider"  # DashScope chat/image/tts/video call
SEAM_REDIS = "redis"  # RedisClient get_json/set_json/publish
SEAM_DB = "db"  # SQLAlchemy session / repo
SEAM_BLOB = "blob"  # OSS / S3 blob store


def provider_rate_limit(
    *, probability: float = 1.0, only_indices: frozenset[int] | None = None
) -> FaultRule:
    """A DashScope ``429 Throttling.RateQuota`` rule (the §gotcha image-model 429)."""

    def _exc(seam: str, name: str) -> Exception:
        return InjectedFault(seam, name, "429 Throttling.RateQuota (injected)")

    return FaultRule(
        name="provider_429",
        kind=FaultKind.ERROR,
        probability=probability,
        exc_factory=_exc,
        only_call_indices=only_indices,
    )


def provider_slow(latency_ms: float, *, probability: float = 1.0) -> FaultRule:
    """A slow-provider rule (Wan poll dragging toward the timeout, §12.1)."""
    return FaultRule(
        name="provider_slow",
        kind=FaultKind.LATENCY,
        probability=probability,
        latency_ms=latency_ms,
    )


def redis_partition(*, probability: float = 1.0, latency_ms: float = 0.0) -> FaultRule:
    """A Redis partition rule — the queue/scheduler control plane goes unreachable."""
    return FaultRule(
        name="redis_partition",
        kind=FaultKind.PARTITION,
        probability=probability,
        latency_ms=latency_ms,
    )


def db_fault(*, probability: float = 1.0) -> FaultRule:
    """A transient DB error rule (mirror writes must never break the hot path)."""
    return FaultRule(name="db_fault", kind=FaultKind.ERROR, probability=probability)


def transient_then_recover(seam: str, *, fail_first: int) -> ChaosController:
    """A controller that fails a seam's first ``fail_first`` calls, then recovers.

    The canonical retry/DLQ test fixture (§12.1): the first N attempts raise an
    injected error, attempt N+1 onward call through cleanly — so a test can prove
    the retry policy recovers before the cap or dead-letters after it.
    """
    controller = ChaosController(seed=0)
    controller.add_rule(
        seam,
        FaultRule(
            name="transient",
            kind=FaultKind.ERROR,
            probability=1.0,
            only_call_indices=frozenset(range(fail_first)),
        ),
    )
    return controller


__all__ = [
    "SEAM_BLOB",
    "SEAM_DB",
    "SEAM_PROVIDER",
    "SEAM_REDIS",
    "ChaosController",
    "FaultKind",
    "FaultRule",
    "InjectedFault",
    "SeamCounters",
    "db_fault",
    "provider_rate_limit",
    "provider_slow",
    "redis_partition",
    "transient_then_recover",
]
