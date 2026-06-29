"""The :class:`InferenceRouter` — the high-throughput scheduling brain (facet A).

This is the composition root for the router facet. It wires together every
primitive in this package into one async submit→dispatch→complete loop:

```
submit(request)
  └─ coalescing       (§12.3 dedup: followers await the leader, never scheduled)
       └─ admission   (§12.2 backpressure: shed / cap / reject, else enqueue)
            └─ fair-share queue   (§12.2 strict priority over per-flow WFQ)

tick()  (driven by the caller / event loop / simulator)
  └─ drop SLA-expired heads
  └─ for each worker with headroom, in fair-share order:
        bin-pack a token-budget micro-batch onto the KV-warmest fitting worker
        └─ execute_batch() on the InferenceBackend
             └─ settle the leader + fan out to coalesced followers
```

It owns *scheduling*, not *transport*: the actual model call is an injected
:class:`~app.inference.router.protocols.InferenceBackend` per model (which, in
production, funnels through the existing providers/resilience gateway — the
router *composes* with it, never edits it). With no backend wired and
``KINORA_LIVE_VIDEO`` off, nothing here spends a credit; the bundled simulator
backend is a deterministic fake.

Concurrency model: a single-writer scheduling loop. ``submit`` only touches the
queue + coalescing table (cheap, synchronous critical section under one lock);
``tick`` does the dispatch + awaits backends. The router is safe to drive from
one event loop; cross-loop use is out of scope (one router per worker process).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger

from .admission import (
    AdmissionConfig,
    AdmissionController,
    AdmissionDecision,
    LoadSnapshot,
    QueueTimeSLA,
    RejectReason,
)
from .affinity import AffinityConfig, AffinityRouter, ResidencyOracle
from .cancellation import CancellationToken, CancelledError
from .coalescing import CoalescingTable
from .errors import (
    AdmissionRejected,
    BackendError,
    QueueTimeSLAExpired,
)
from .fairshare import FairShareConfig, FairShareScheduler
from .metrics import RouterStats
from .protocols import InferenceBackend, InferenceResult, PrefixCacheOracle
from .request import TERMINAL_STATES, InferenceRequest, RequestPriority, RequestState
from .worker import Worker, WorkerPool

logger = get_logger("app.inference.router")

#: Optional hook fired on every state transition (request_id, new_state).
TransitionHook = Callable[[str, RequestState], None]


@dataclass(frozen=True, slots=True)
class RouterConfig:
    """Top-level router tunables; sub-configs default to sensible values."""

    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    fairshare: FairShareConfig = field(default_factory=FairShareConfig)
    affinity: AffinityConfig = field(default_factory=AffinityConfig)
    #: Per-step chunked-prefill cap (new prompt tokens admitted per worker tick).
    prefill_chunk_budget: int | None = None
    #: Backstop queue-time SLA for requests that did not set their own.
    default_queue_sla_s: float | None = None
    #: Coalesce identical in-flight requests (§12.3 dedup).
    coalescing_enabled: bool = True
    #: §12.2 preemption: when a committed/interactive request is rejected for
    #: capacity (queue full / soft-zone shed), evict queued strictly-lower-priority
    #: work to make room. The evicted victim's future is rejected so the client
    #: can fall back (the keyframe ladder covers a dropped speculative shot).
    preemption_enabled: bool = True
    #: Highest priority a request must *exceed* to trigger preemption — i.e. only
    #: requests strictly above ``preempt_floor`` preempt, and only victims at or
    #: below it are evictable. Defaults to ``SPECULATIVE`` so committed+ preempt
    #: speculative/bulk, but speculative never preempts.
    preempt_floor: RequestPriority = RequestPriority.SPECULATIVE


@dataclass(slots=True)
class _Tracked:
    """Router-side bookkeeping for one in-flight request."""

    request: InferenceRequest
    state: RequestState
    coalesce_key: str
    is_leader: bool
    worker_id: str | None = None
    cancel_token: CancellationToken | None = None


class InferenceRouter:
    """Composes admission, fair-share, bin-packing, affinity, and coalescing.

    One router instance serves one model (its ``pool`` is per-model). A
    multi-model deployment runs one router per model behind a thin dispatcher;
    keeping a router single-model is what makes affinity + bin-packing tractable.
    """

    def __init__(
        self,
        model: str,
        pool: WorkerPool,
        backend: InferenceBackend,
        *,
        config: RouterConfig | None = None,
        oracle: PrefixCacheOracle | None = None,
        clock: Callable[[], float] | None = None,
        on_transition: TransitionHook | None = None,
    ) -> None:
        if pool.model != model or backend.model != model:
            raise BackendError(
                f"model mismatch: router={model!r} pool={pool.model!r} backend={backend.model!r}"
            )
        self.model = model
        self._pool = pool
        self._backend = backend
        self._config = config or RouterConfig()
        self._clock = clock or time.monotonic
        self._on_transition = on_transition

        self._admission = AdmissionController(self._config.admission)
        self._sla = QueueTimeSLA(default_sla_s=self._config.default_queue_sla_s, clock=self._clock)
        self._queue = FairShareScheduler(self._config.fairshare)
        self._oracle = oracle or ResidencyOracle(self._pool.get)
        self._affinity = AffinityRouter(self._oracle, self._config.affinity)
        self._coalescing = CoalescingTable(enabled=self._config.coalescing_enabled)

        self.stats = RouterStats()
        self._tracked: dict[str, _Tracked] = {}
        self._tenant_inflight: dict[str, int] = {}
        self._futures: dict[str, asyncio.Future[InferenceResult]] = {}
        self._lock = asyncio.Lock()

    # -- introspection ---------------------------------------------------- #

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def inflight(self) -> int:
        """Requests dispatched/running on a worker (not yet terminal)."""
        return sum(
            1
            for t in self._tracked.values()
            if t.state in (RequestState.DISPATCHED, RequestState.RUNNING)
        )

    def state_of(self, request_id: str) -> RequestState | None:
        t = self._tracked.get(request_id)
        return t.state if t else None

    # -- cancellation ----------------------------------------------------- #

    async def cancel(self, request_id: str, *, reason: str | None = None) -> bool:
        """Cancel a request by id (§4.8 / §12.1).

        A *queued* request is dropped from the queue immediately and its future
        rejected with :class:`~app.inference.router.cancellation.CancelledError`.
        A *running* request has its cancellation token tripped (if it carries
        one) so the backend can abort cooperatively at its next safe point; the
        router leaves the in-flight execution to settle naturally. Returns
        ``True`` if anything was cancelled.
        """
        async with self._lock:
            return self._cancel_locked(request_id, reason)

    async def cancel_scope(self, scope: str, *, reason: str | None = None) -> int:
        """Cancel every tracked request whose ``cancel_token.scope`` matches.

        Trips running tokens and drops queued requests in the scope. Returns the
        count cancelled. The scope is read from each request's attached token, so
        only requests submitted with a scoped token participate.
        """
        async with self._lock:
            ids = [
                rid
                for rid, t in self._tracked.items()
                if t.cancel_token is not None and t.cancel_token.scope == scope
            ]
            return sum(1 for rid in ids if self._cancel_locked(rid, reason))

    def _cancel_locked(self, request_id: str, reason: str | None) -> bool:
        t = self._tracked.get(request_id)
        if t is None or t.state in TERMINAL_STATES:
            return False
        if t.cancel_token is not None:
            t.cancel_token.cancel(reason)
        if t.state == RequestState.QUEUED:
            self._queue.remove(request_id)
            self._settle_failure(
                request_id,
                CancelledError(request_id=request_id, reason=reason),
                RequestState.CANCELLED,
            )
            return True
        # Dispatched/running: token (if any) is tripped; settlement happens when
        # the backend returns. Mark intent so a second cancel is a no-op.
        return t.cancel_token is not None

    # -- submit ----------------------------------------------------------- #

    async def submit(self, request: InferenceRequest) -> asyncio.Future[InferenceResult]:
        """Admit + enqueue (or coalesce) ``request``; return a result future.

        Raises:
            AdmissionRejected: if backpressure/caps refuse the request. The
                future is *not* created in that case — rejection is synchronous.
        """
        if request.model != self.model:
            raise BackendError(f"router serves {self.model!r}, got {request.model!r}")
        async with self._lock:
            return self._submit_locked(request)

    def _submit_locked(self, request: InferenceRequest) -> asyncio.Future[InferenceResult]:
        # 1. Coalesce: a follower never enters admission/queue.
        outcome = self._coalescing.admit(request)
        if not outcome.is_leader:
            assert outcome.follower_future is not None
            self.stats.on_coalesce()
            self._mark(request, RequestState.COALESCED, track=False)
            return outcome.follower_future

        # 2. Admission control.
        load = self._load_snapshot(request)
        decision = self._admission.evaluate(request, load)
        if not decision.admit and self._should_preempt(request, decision.reason):
            # §12.2: a committed/interactive request preempts queued lower-priority
            # (speculative/bulk) work when capacity is scarce. Evict one victim and
            # re-evaluate; repeat until the request is admitted or no victim remains.
            decision = self._preempt_and_reevaluate(request)
        if not decision.admit:
            assert decision.reason is not None
            self.stats.on_reject(decision.reason)
            self._coalescing.fail(
                outcome.coalesce_key,
                AdmissionRejected(
                    f"admission rejected: {decision.reason.value}",
                    request_id=request.request_id,
                    reason=decision.reason.value,
                    retry_after_s=decision.retry_after_s,
                ),
            )
            self._mark(request, RequestState.REJECTED, track=False)
            raise AdmissionRejected(
                f"admission rejected: {decision.reason.value}",
                request_id=request.request_id,
                reason=decision.reason.value,
                retry_after_s=decision.retry_after_s,
            )

        # 3. Enqueue (leader).
        stamped = request.with_enqueued_at(self._clock())
        future: asyncio.Future[InferenceResult] = asyncio.get_running_loop().create_future()
        self._futures[request.request_id] = future
        token = request.metadata.get("cancel_token")
        self._tracked[request.request_id] = _Tracked(
            request=stamped,
            state=RequestState.QUEUED,
            coalesce_key=outcome.coalesce_key,
            is_leader=True,
            cancel_token=token if isinstance(token, CancellationToken) else None,
        )
        self._tenant_inflight[request.tenant] = self._tenant_inflight.get(request.tenant, 0) + 1
        self._queue.enqueue(stamped)
        self.stats.on_admit()
        self._fire(request.request_id, RequestState.QUEUED)
        return future

    def _should_preempt(self, request: InferenceRequest, reason: RejectReason | None) -> bool:
        """Whether ``request`` may preempt queued lower-priority work (§12.2)."""
        return (
            self._config.preemption_enabled
            and request.priority > self._config.preempt_floor
            and reason in (RejectReason.QUEUE_FULL, RejectReason.SHED_LOW_PRIORITY)
        )

    def _preempt_and_reevaluate(self, request: InferenceRequest) -> AdmissionDecision:
        """Evict victims until ``request`` is admissible or none remain.

        Each evicted victim's future is rejected with an
        :class:`AdmissionRejected` (reason ``preempted``) so its waiter can fall
        back to the degradation ladder; the victim releases its tenant-inflight
        reservation. Returns the final admission decision.
        """
        decision = self._admission.evaluate(request, self._load_snapshot(request))
        while not decision.admit:
            victim = self._queue.evict_victim(self._config.preempt_floor)
            if victim is None:
                break
            self.stats.on_preempt()
            self._settle_failure(
                victim.request_id,
                AdmissionRejected(
                    "preempted by higher-priority request",
                    request_id=victim.request_id,
                    reason="preempted",
                ),
                RequestState.REJECTED,
            )
            decision = self._admission.evaluate(request, self._load_snapshot(request))
        return decision

    # -- scheduling tick -------------------------------------------------- #

    async def tick(self) -> int:
        """Run one scheduling step: drop SLA-expired heads, then dispatch.

        Returns the number of requests dispatched this tick. Idempotent when the
        queue is empty. Awaits the backend(s) for the batches it forms, so one
        ``tick`` advances the whole pipeline by one round.
        """
        async with self._lock:
            self._expire_locked()
            batches = self._form_batches_locked()
        if not batches:
            return 0
        await self._execute_batches(batches)
        dispatched = sum(len(reqs) for _, reqs in batches)
        return dispatched

    async def run_until_idle(self, *, max_ticks: int = 100_000) -> int:
        """Drive ``tick`` until the queue drains (or ``max_ticks``). Test/sim aid.

        Returns total dispatched. Stops early once a tick makes no progress and
        the queue is empty, so it never spins.
        """
        total = 0
        for _ in range(max_ticks):
            n = await self.tick()
            total += n
            if n == 0 and self.queue_depth == 0:
                break
        return total

    # -- internals -------------------------------------------------------- #

    def _load_snapshot(self, request: InferenceRequest) -> LoadSnapshot:
        tenant = request.tenant
        queued_for_tenant = sum(1 for r in self._queue.queued_requests() if r.tenant == tenant)
        return LoadSnapshot(
            queue_depth=len(self._queue),
            tenant_inflight=self._tenant_inflight.get(tenant, 0),
            tenant_queue_depth=queued_for_tenant,
            max_worker_token_capacity=self._max_worker_capacity(),
        )

    def _max_worker_capacity(self) -> int:
        workers = self._pool.schedulable_workers()
        return max((w.config.token_capacity for w in workers), default=0)

    def _expire_locked(self) -> None:
        now = self._clock()
        for req in self._sla.expired_among(self._queue.queued_requests(), now):
            self._queue.remove(req.request_id)
            self.stats.on_expire()
            waited = self._sla.waited(req, now)
            sla = self._sla.sla_for(req) or 0.0
            self._settle_failure(
                req.request_id,
                QueueTimeSLAExpired(
                    f"queue-time SLA expired after {waited:.3f}s",
                    request_id=req.request_id,
                    waited_s=waited,
                    sla_s=sla,
                ),
                RequestState.EXPIRED,
            )

    def _form_batches_locked(self) -> list[tuple[Worker, list[InferenceRequest]]]:
        """Select fair-share-ordered requests and pack them onto KV-warm workers.

        Capacity-aware WFQ: we repeatedly ask the scheduler for the next request
        in strict-priority-then-weighted-fair-share order via ``select_next`` and
        try to place it on its best fitting worker (affinity + load). Only a
        request that is actually placed is ``commit``\\ ed (which advances its
        flow's virtual time + served cost) — so fairness accounting tracks
        genuine dispatch, never a peek that gets skipped. A request whose head
        cannot be placed on any worker this tick has its flow added to ``skip``,
        so it neither stalls a placeable flow nor is mis-charged; it is simply
        reconsidered next tick. The per-worker chunked-prefill cap is enforced as
        part of placement.
        """
        workers = self._pool.schedulable_workers()
        if not workers:
            return []

        now = self._clock()
        per_worker: dict[str, list[InferenceRequest]] = {}
        # Provisional headroom so multiple requests can co-schedule on a worker.
        prov_tokens: dict[str, int] = {w.worker_id: w.token_headroom for w in workers}
        prov_slots: dict[str, int] = {w.worker_id: w.slot_headroom for w in workers}
        prov_prefill: dict[str, int] = {
            w.worker_id: (self._config.prefill_chunk_budget or 1 << 62) for w in workers
        }
        skip: set[tuple[str, str]] = set()

        while True:
            req = self._queue.select_next(skip)
            if req is None:
                break
            target = self._place(req, workers, prov_tokens, prov_slots, prov_prefill)
            if target is None:
                # No worker can take this flow's head this tick; pass the flow.
                skip.add(req.fairness_key())
                continue
            self._queue.commit(req)
            per_worker.setdefault(target.worker_id, []).append(req)
            prov_tokens[target.worker_id] -= req.total_tokens
            prov_slots[target.worker_id] -= 1
            prov_prefill[target.worker_id] -= req.prompt_tokens

        # Commit the placements to real worker capacity + record the batches.
        batches: list[tuple[Worker, list[InferenceRequest]]] = []
        for worker in workers:
            cands = per_worker.get(worker.worker_id)
            if not cands:
                continue
            for req in cands:
                worker.admit(req)
                self._dispatch_mark(req, worker, now)
            self.stats.on_batch(len(cands))
            batches.append((worker, cands))
        return batches

    def _place(
        self,
        req: InferenceRequest,
        workers: list[Worker],
        prov_tokens: dict[str, int],
        prov_slots: dict[str, int],
        prov_prefill: dict[str, int],
    ) -> Worker | None:
        """Affinity-select among workers with provisional headroom for ``req``.

        Honours the per-worker token, slot, and chunked-prefill provisional
        budgets so the placement decision matches what the worker can actually
        admit this tick (including the §12.2 prefill-chunk cap).
        """
        eligible = [
            w
            for w in workers
            if prov_slots[w.worker_id] >= 1
            and req.total_tokens <= prov_tokens[w.worker_id]
            and req.prompt_tokens <= prov_prefill[w.worker_id]
        ]
        if not eligible:
            return None
        return self._affinity.select(req, eligible)

    def _dispatch_mark(self, req: InferenceRequest, worker: Worker, now: float) -> None:
        t = self._tracked.get(req.request_id)
        if t is not None:
            t.state = RequestState.DISPATCHED
            t.worker_id = worker.worker_id
        wait = max(0.0, now - req.enqueued_at)
        self.stats.on_dispatch(req.priority, wait)
        self._fire(req.request_id, RequestState.DISPATCHED)

    async def _execute_batches(self, batches: list[tuple[Worker, list[InferenceRequest]]]) -> None:
        await asyncio.gather(*(self._execute_one(w, reqs) for w, reqs in batches))

    async def _execute_one(self, worker: Worker, reqs: list[InferenceRequest]) -> None:
        for r in reqs:
            t = self._tracked.get(r.request_id)
            if t is not None:
                t.state = RequestState.RUNNING
                self._fire(r.request_id, RequestState.RUNNING)
        try:
            results = await self._backend.execute_batch(reqs)
        except Exception as exc:  # noqa: BLE001 - normalize to BackendError per request
            err = BackendError(f"backend batch failed: {exc}", cause=exc)
            async with self._lock:
                for r in reqs:
                    worker.complete(r)
                    self._settle_failure(r.request_id, err, RequestState.FAILED)
            return
        by_id = {res.request_id: res for res in results}
        async with self._lock:
            for r in reqs:
                res = by_id.get(r.request_id)
                actual = res.total_tokens if res else None
                worker.complete(r, actual_total_tokens=actual)
                tracked = self._tracked.get(r.request_id)
                token = tracked.cancel_token if tracked else None
                if token is not None and token.cancelled:
                    # Cancelled mid-flight (§4.8): discard the result, settle the
                    # waiter as cancelled — its capacity was already released above.
                    self._settle_failure(
                        r.request_id,
                        CancelledError(request_id=r.request_id, reason=token.reason),
                        RequestState.CANCELLED,
                    )
                elif res is None:
                    self._settle_failure(
                        r.request_id,
                        BackendError(f"backend returned no result for {r.request_id}"),
                        RequestState.FAILED,
                    )
                elif res.ok:
                    self._settle_success(r, res)
                else:
                    self._settle_failure(
                        r.request_id,
                        BackendError(res.error or "backend error"),
                        RequestState.FAILED,
                    )

    # -- settlement ------------------------------------------------------- #

    def _settle_success(self, req: InferenceRequest, result: InferenceResult) -> None:
        self.stats.on_complete(
            ok=True,
            tokens_in=result.prompt_tokens,
            tokens_out=result.output_tokens,
            cache_hit=result.cache_hit,
        )
        n = self._coalescing.settle(self._coalesce_key_of(req.request_id), result)
        if n:
            self.stats.on_coalesce(0)  # already counted at admit; keep gauge accurate
        self._resolve_future(req.request_id, result)
        self._finish_tracking(req.request_id, RequestState.SUCCEEDED, tenant=req.tenant)

    def _settle_failure(self, request_id: str, error: BaseException, state: RequestState) -> None:
        t = self._tracked.get(request_id)
        tenant = t.request.tenant if t else None
        if t is not None and t.is_leader:
            self._coalescing.fail(t.coalesce_key, error)
        if state == RequestState.FAILED:
            self.stats.on_complete(ok=False, tokens_in=0, tokens_out=0, cache_hit=False)
        elif state == RequestState.CANCELLED:
            self.stats.on_cancel()
        self._reject_future(request_id, error)
        self._finish_tracking(request_id, state, tenant=tenant)

    def _coalesce_key_of(self, request_id: str) -> str:
        t = self._tracked.get(request_id)
        return t.coalesce_key if t else request_id

    def _resolve_future(self, request_id: str, result: InferenceResult) -> None:
        fut = self._futures.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _reject_future(self, request_id: str, error: BaseException) -> None:
        fut = self._futures.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_exception(error)

    def _finish_tracking(self, request_id: str, state: RequestState, *, tenant: str | None) -> None:
        t = self._tracked.get(request_id)
        if t is not None:
            t.state = state
        if tenant is not None and state in TERMINAL_STATES:
            remaining = self._tenant_inflight.get(tenant, 0) - 1
            if remaining > 0:
                self._tenant_inflight[tenant] = remaining
            else:
                self._tenant_inflight.pop(tenant, None)
        self._fire(request_id, state)
        # Drop terminal tracking to bound memory (state already fired).
        if state in TERMINAL_STATES:
            self._tracked.pop(request_id, None)

    def _mark(self, request: InferenceRequest, state: RequestState, *, track: bool) -> None:
        if track:
            self._tracked[request.request_id] = _Tracked(
                request=request,
                state=state,
                coalesce_key=request.effective_coalesce_key,
                is_leader=False,
            )
        self._fire(request.request_id, state)

    def _fire(self, request_id: str, state: RequestState) -> None:
        if self._on_transition is not None:
            self._on_transition(request_id, state)


__all__ = ["InferenceRouter", "RouterConfig", "TransitionHook"]
