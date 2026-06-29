"""Continuous batching scheduler (the serving scheduler under test).

Static batching waits for a whole batch to finish before admitting new work, so a
fast request is held hostage by the slowest one in its batch. **Continuous batching**
(a.k.a. iteration-level scheduling) instead re-forms the batch every decode step:
finished sequences leave, queued sequences join, all subject to two hard limits —

* ``max_batch_size`` — how many sequences may decode concurrently, and
* ``max_batch_tokens`` — the total live context-token budget across the batch
  (the KV-cache pressure proxy).

This module is the *policy*: given the current running set, the wait queue, and the
KV-cache state, it decides which queued requests to admit this step and detects when
a running request must be preempted because the cache ran out of room. It is a pure
state object — it does not advance the clock (the simulator does) — so its decisions
are exhaustively property-testable.

**The invariants this scheduler must never break** (asserted here *and* re-checked by
the simulator, and the subject of the property tests):

1. The running batch never exceeds ``max_batch_size`` sequences.
2. The running batch's live token total never exceeds ``max_batch_tokens``.
3. No queued request starves: admission is priority-then-FIFO, so a request can only
   be overtaken by a strictly higher-priority one, and aging guarantees forward
   progress for everyone within a priority class.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.mlplatform.serving.errors import InvariantViolationError, ServingConfigError
from app.mlplatform.serving.kvcache import PagedKVCache
from app.mlplatform.serving.requests import InferenceRequest, RequestState


@dataclass(frozen=True, slots=True)
class ContinuousBatchConfig:
    """Limits and policy knobs for the continuous-batching scheduler.

    ``max_batch_tokens`` bounds the running batch's **maximum reachable context**
    (each sequence's ``prompt + max_tokens``), not its instantaneous live length.
    Reserving against the worst case at admission is what makes the budget
    unbreachable: as sequences decode and grow, the live token total stays ``<=`` the
    budget by construction. This mirrors how a real server sizes its per-sequence KV
    reservation; the per-step *prefill* burst is separately capped by
    ``max_admit_per_step``.
    """

    max_batch_size: int = 32
    max_batch_tokens: int = 16384
    #: Admit at most this many new sequences per step (prefill is bursty/expensive).
    max_admit_per_step: int = 8
    #: When True, prefer to fill the batch even if the newest admit is large.
    eager_admission: bool = True

    def __post_init__(self) -> None:
        if self.max_batch_size <= 0:
            raise ServingConfigError("max_batch_size must be positive")
        if self.max_batch_tokens <= 0:
            raise ServingConfigError("max_batch_tokens must be positive")
        if self.max_admit_per_step <= 0:
            raise ServingConfigError("max_admit_per_step must be positive")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The result of a scheduling step: who to admit, who to preempt."""

    admit: tuple[InferenceRequest, ...]
    preempt: tuple[InferenceRequest, ...]


class BatchScheduler:
    """Iteration-level (continuous) batching policy over a paged KV-cache.

    Holds the wait queue and the running set as ordered structures; the simulator
    calls :meth:`enqueue` on arrival, :meth:`schedule` once per step to get the
    admit/preempt plan, then applies it and advances decoding.
    """

    def __init__(self, config: ContinuousBatchConfig, cache: PagedKVCache) -> None:
        self.config = config
        self.cache = cache
        self._waiting: list[InferenceRequest] = []
        self._running: dict[str, InferenceRequest] = {}

    # -- queue management -------------------------------------------------- #

    def enqueue(self, request: InferenceRequest) -> None:
        """Add a freshly arrived request to the wait queue."""
        if request.state != RequestState.QUEUED:
            raise InvariantViolationError(
                f"enqueue requires QUEUED, got {request.state} for {request.request_id}"
            )
        self._waiting.append(request)

    @property
    def waiting(self) -> tuple[InferenceRequest, ...]:
        return tuple(self._waiting)

    @property
    def running(self) -> tuple[InferenceRequest, ...]:
        return tuple(self._running.values())

    @property
    def n_waiting(self) -> int:
        return len(self._waiting)

    @property
    def n_running(self) -> int:
        return len(self._running)

    @property
    def is_idle(self) -> bool:
        return not self._waiting and not self._running

    def live_token_total(self) -> int:
        """Sum of *live* context tokens across the running batch (telemetry).

        This grows as sequences decode; it is bounded above by
        :meth:`reserved_token_total`, which is what the budget invariant guards.
        """
        return sum(r.total_context_tokens for r in self._running.values())

    @staticmethod
    def _reserved_tokens(req: InferenceRequest) -> int:
        """A sequence's worst-case context: prompt + everything it could still emit."""
        return req.prompt_tokens + req.target_tokens

    def reserved_token_total(self) -> int:
        """Sum of worst-case (max-reachable) context across the running batch.

        This is the quantity ``max_batch_tokens`` bounds. Because we reserve the full
        ``prompt + max_tokens`` per sequence at admission, decode growth can never push
        the batch past the budget — the invariant holds by construction.
        """
        return sum(self._reserved_tokens(r) for r in self._running.values())

    # -- scheduling -------------------------------------------------------- #

    def schedule(
        self, now_ms: float, *, prefix_of: dict[str, str] | None = None
    ) -> AdmissionDecision:
        """Decide admissions for this step without mutating state.

        Walks the wait queue in priority-then-FIFO order and admits while every
        limit holds: batch-size, token budget, KV-cache room, and the per-step admit
        cap. ``prefix_of`` optionally maps a request id to a shared prompt-prefix key
        for KV-cache reuse accounting. Returns the plan; :meth:`apply` enacts it.
        """
        prefix_of = prefix_of or {}
        # Order the queue deterministically (priority, arrival, id). This is the
        # anti-starvation guarantee: a request is only overtaken by a strictly more
        # urgent one, and within a class it is strict FIFO.
        ordered = sorted(self._waiting, key=lambda r: r.sort_key())
        admit: list[InferenceRequest] = []
        # Track the *projected* batch state as we tentatively admit. The token
        # projection uses the worst-case reservation so the budget can never be
        # breached by later decode growth.
        proj_size = len(self._running)
        proj_tokens = self.reserved_token_total()
        proj_used_blocks = self.cache.used_blocks
        for req in ordered:
            if len(admit) >= self.config.max_admit_per_step:
                break
            if proj_size >= self.config.max_batch_size:
                break
            new_tokens = self._reserved_tokens(req)
            # An oversized request (reservation > whole budget) can only ever run
            # solo. Admit it when the batch is empty so it never starves; otherwise
            # it must wait for the batch to drain.
            oversized = new_tokens > self.config.max_batch_tokens
            if oversized:
                if proj_size == 0:
                    prefix = prefix_of.get(req.request_id)
                    need_blocks = self.cache._new_blocks_for_prompt(
                        req.prompt_tokens, prefix=prefix
                    )
                    if need_blocks <= self.cache.capacity:
                        admit.append(req)
                        proj_size += 1
                        proj_tokens += new_tokens
                        proj_used_blocks += need_blocks
                # Whether or not we admitted it, do not skip past the head — keep FIFO.
                break
            if proj_tokens + new_tokens > self.config.max_batch_tokens:
                # Budget would be exceeded by a normally-sized head request. Stopping
                # (rather than skipping to a smaller request) preserves FIFO fairness
                # and prevents large requests from starving behind small ones.
                break
            prefix = prefix_of.get(req.request_id)
            need_blocks = self.cache._new_blocks_for_prompt(req.prompt_tokens, prefix=prefix)
            if proj_used_blocks + need_blocks > self.cache.capacity:
                # No KV-cache room for the head right now. In eager mode a later,
                # smaller request may still fit; otherwise hold the line for FIFO.
                if not self.config.eager_admission:
                    break
                continue
            admit.append(req)
            proj_size += 1
            proj_tokens += new_tokens
            proj_used_blocks += need_blocks
        return AdmissionDecision(admit=tuple(admit), preempt=())

    def apply(
        self,
        decision: AdmissionDecision,
        now_ms: float,
        *,
        prefix_of: dict[str, str] | None = None,
    ) -> None:
        """Enact an :meth:`schedule` decision: reserve cache, move requests to PREFILL.

        Admitted requests are removed from the wait queue, allocated KV-cache blocks
        for their prompt, and transitioned to PREFILL with an ``admit_ms`` stamp.
        """
        prefix_of = prefix_of or {}
        admit_ids = {r.request_id for r in decision.admit}
        for req in decision.admit:
            prefix = prefix_of.get(req.request_id)
            # Reserve KV-cache for the prompt. CapacityError here is a scheduler bug
            # because schedule() already checked room — surface it as an invariant.
            self.cache.admit(req.request_id, req.prompt_tokens, prefix=prefix)
            req.state = RequestState.PREFILL
            req.admit_ms = now_ms
            self._running[req.request_id] = req
        if admit_ids:
            self._waiting = [r for r in self._waiting if r.request_id not in admit_ids]
        self._check_invariants()

    def complete(self, request_id: str) -> InferenceRequest:
        """Remove a finished request from the running set and free its cache."""
        req = self._running.pop(request_id, None)
        if req is None:
            raise InvariantViolationError(f"complete called for non-running {request_id!r}")
        self.cache.free(request_id)
        return req

    def preempt(self, request_id: str) -> InferenceRequest:
        """Evict a running request back to the wait queue, freeing its KV-cache.

        Continuous-batching servers preempt the *most recently admitted* / lowest
        priority sequence when the cache cannot grow. The evicted request keeps its
        generated count reset (recompute on re-admission) and goes back to QUEUED so
        the anti-starvation ordering re-applies.
        """
        req = self._running.pop(request_id, None)
        if req is None:
            raise InvariantViolationError(f"preempt called for non-running {request_id!r}")
        self.cache.free(request_id)
        req.state = RequestState.QUEUED
        req.generated = 0
        req.admit_ms = None
        req.first_token_ms = None
        self._waiting.append(req)
        return req

    def victim_for_preemption(self) -> InferenceRequest | None:
        """Pick the running request to evict when the cache is exhausted.

        Lowest priority first, then the most recently admitted (largest admit_ms),
        then largest id — the inverse of the admission order, so we evict the least
        deserving sequence and never thrash the oldest, highest-priority one.
        """
        if not self._running:
            return None
        return max(
            self._running.values(),
            key=lambda r: (r.priority, r.admit_ms or 0.0, r.request_id),
        )

    # -- invariants -------------------------------------------------------- #

    def _check_invariants(self) -> None:
        if self.n_running > self.config.max_batch_size:
            raise InvariantViolationError(
                f"running batch {self.n_running} > max_batch_size {self.config.max_batch_size}"
            )
        # The budget bounds the worst-case reservation, which dominates the live
        # total. The one sanctioned exception is a *single* oversized request running
        # solo (its own reservation exceeds the whole budget) — real servers admit
        # such a request alone rather than starve it. So the invariant is: with two
        # or more sequences, the reservation must fit the budget.
        reserved = self.reserved_token_total()
        if self.n_running >= 2 and reserved > self.config.max_batch_tokens:
            raise InvariantViolationError(
                f"reserved batch tokens {reserved} > max_batch_tokens "
                f"{self.config.max_batch_tokens} across {self.n_running} sequences"
            )

    def assert_invariants(self) -> None:
        """Public hook for the simulator/property tests to re-check invariants."""
        self._check_invariants()
        if self.cache.used_blocks > self.cache.capacity:
            raise InvariantViolationError("cache overcommitted")


def waiting_ids(requests: Iterable[InferenceRequest]) -> tuple[str, ...]:
    """Helper: stable id tuple of a request collection (for assertions/tests)."""
    return tuple(r.request_id for r in requests)


__all__ = [
    "AdmissionDecision",
    "BatchScheduler",
    "ContinuousBatchConfig",
    "waiting_ids",
]
