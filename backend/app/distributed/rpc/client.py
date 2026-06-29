"""The resilient RPC client — where every policy composes into one call.

This is the heart of the data-plane: a single :meth:`RpcClient.call` that takes a
logical ``(service, method, request)`` and applies, in the right order, every
distributed-systems guard the mesh promises. The composition order matters and is
deliberate:

    deadline gate
      └─ retry loop (budget + backoff)
           └─ hedging race (idempotent only)
                └─ per-attempt:  discovery → load-balance → circuit gate
                                  → transport.send → classify outcome

Read it inside-out: each *attempt* resolves the service to instances, picks one
under the load-balancer policy, checks that instance's circuit breaker, sends over
the transport, and classifies the result (feeding the breaker + outlier detector).
A hedge races several such attempts against the tail; the retry loop re-issues a
*failed* call under backoff and the shared retry budget; the whole thing is capped
by the inherited deadline so a chain can never exceed the originator's time
budget. Per-call policy can be overridden via :class:`CallOptions`; otherwise the
client's defaults (and the method's ``idempotent`` flag from the contract) decide.

Everything is driven by the injected :class:`Clock` + ``sleep`` seam, so the full
policy stack is exercised in tests with a :class:`ManualClock` and a fake
transport — no sockets, no real waiting, fully deterministic.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.distributed.rpc import metrics
from app.distributed.rpc.circuit import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
)
from app.distributed.rpc.context import RequestContext
from app.distributed.rpc.contracts import MethodSpec
from app.distributed.rpc.deadline import Clock, Deadline, SystemClock, deadline_for
from app.distributed.rpc.errors import (
    FailureKind,
    RpcError,
    RpcStatus,
    deadline_exceeded,
    unavailable,
)
from app.distributed.rpc.health import OutlierDetector
from app.distributed.rpc.hedging import HedgePolicy, run_with_hedging
from app.distributed.rpc.loadbalancer import LoadBalancePolicy, LoadBalancer
from app.distributed.rpc.messages import RpcRequest, RpcResponse
from app.distributed.rpc.registry import Discovery, ServiceInstance
from app.distributed.rpc.retry import RetryPolicy, SleepFn, run_with_retry
from app.distributed.rpc.transport import Transport

log = get_logger(__name__)


async def _default_sleep(seconds: float) -> None:
    """The production sleep seam (anyio); imported lazily so tests can replace it."""
    import anyio

    if seconds > 0:
        await anyio.sleep(seconds)


#: How the client obtains the right transport for a chosen instance. In-process
#: there is one shared transport; a split-out service maps its instance's
#: ``transport_ref`` to a remote transport. Defaults to a constant transport.
TransportResolver = Callable[[ServiceInstance], Transport]


@dataclass(frozen=True, slots=True)
class CallOptions:
    """Per-call overrides for the resilience policies.

    Any field left ``None`` falls back to the client's defaults / the method's
    contract flags. ``hash_key`` drives consistent-hash load balancing (e.g. a
    ``session_id`` so a reader's shots stick to one worker). ``idempotent``
    overrides the contract's flag for this one call (rarely needed).
    """

    timeout_s: float | None = None
    retry: RetryPolicy | None = None
    hedge: HedgePolicy | None = None
    hash_key: str | None = None
    idempotent: bool | None = None
    lb_policy: LoadBalancePolicy | None = None


@dataclass
class RpcClient:
    """A resilient client for one logical service set (shared across endpoints).

    Construct one per *process* (it holds the breaker registry, outlier detector,
    and per-service load balancers); call any service through it. The transport is
    resolved per-instance so the same client serves in-process and remote
    instances side by side during a gradual split-out.
    """

    discovery: Discovery
    transport_resolver: TransportResolver
    clock: Clock = field(default_factory=SystemClock)
    sleep: SleepFn = _default_sleep
    default_timeout_s: float | None = 5.0
    default_retry: RetryPolicy = field(default_factory=RetryPolicy)
    default_hedge: HedgePolicy = field(default_factory=HedgePolicy)
    default_lb_policy: LoadBalancePolicy = LoadBalancePolicy.P2C
    breakers: CircuitBreakerRegistry = field(default_factory=CircuitBreakerRegistry)
    outliers: OutlierDetector = field(default_factory=OutlierDetector)
    max_depth: int = 32
    rng: random.Random = field(default_factory=random.Random)
    _balancers: dict[str, LoadBalancer] = field(default_factory=dict, init=False)

    def _balancer(self, service: str, policy: LoadBalancePolicy) -> LoadBalancer:
        lb = self._balancers.get(service)
        if lb is None or lb.policy is not policy:
            lb = LoadBalancer(policy=policy)
            self._balancers[service] = lb
        return lb

    async def call(
        self,
        service: str,
        method: str,
        request: dict[str, object] | None = None,
        *,
        context: RequestContext,
        method_spec: MethodSpec | None = None,
        options: CallOptions | None = None,
    ) -> RpcResponse:
        """Issue one resilient RPC and return the (in-band) response.

        ``request`` is the already-encoded payload dict (a typed stub encodes it
        first; see :mod:`app.distributed.rpc.stub`). ``context`` carries the
        deadline/trace/auth/tenant to propagate. The returned :class:`RpcResponse`
        is result-or-error; transport-fatal conditions (no endpoint, breaker open
        after exhausting fallbacks, deadline) come back as an error response too,
        so a caller can branch on :attr:`RpcResponse.ok` without a try/except.
        """
        opts = options or CallOptions()
        if context.depth > self.max_depth:
            return RpcResponse.from_error(
                RpcError(
                    RpcStatus.RESOURCE_EXHAUSTED,
                    f"max call depth {self.max_depth} exceeded (cycle?)",
                    kind=FailureKind.TRANSPORT,
                    service=service,
                    method=method,
                )
            )

        idempotent = (
            opts.idempotent
            if opts.idempotent is not None
            else (method_spec.idempotent if method_spec is not None else False)
        )
        timeout_s = opts.timeout_s if opts.timeout_s is not None else self.default_timeout_s
        deadline = deadline_for(timeout_s, clock=self.clock, inherited=context.deadline)
        retry = opts.retry or self.default_retry
        hedge = opts.hedge or self.default_hedge
        lb_policy = opts.lb_policy or self.default_lb_policy

        start = self.clock.now()
        retry_count = 0
        hedge_count = 0

        async def _one_attempt(_attempt_index: int) -> RpcResponse:
            """Resolve → balance → breaker → send → classify, once.

            Raises on a non-ok response so a hedge / retry leg is treated as a
            *failed* attempt — a hedging race must only declare a winner on a real
            success, never on an in-band error response (otherwise a fast primary
            failure would cancel the slower-but-successful hedge).
            """
            nonlocal hedge_count
            if _attempt_index > 0:
                hedge_count += 1
            resp = await self._dispatch_once(
                service,
                method,
                request or {},
                context=context,
                deadline=deadline,
                lb_policy=lb_policy,
                hash_key=opts.hash_key,
                attempt=_attempt_index,
            )
            if not resp.ok:
                raise resp.to_error(service=service, method=method)
            return resp

        async def _retryable_attempt(attempt: int) -> RpcResponse:
            """One retry-loop attempt: a (possibly hedged) dispatch.

            ``run_with_hedging`` returns the first successful leg or raises the
            last leg's :class:`RpcError`; that raise propagates to the retry loop,
            which decides whether to re-issue.
            """
            nonlocal retry_count
            if attempt > 0:
                retry_count += 1
            resp = await run_with_hedging(
                _one_attempt,
                policy=hedge,
                idempotent=idempotent,
                deadline=deadline,
                clock=self.clock,
                sleep=self.sleep,
            )
            assert isinstance(resp, RpcResponse)  # noqa: S101
            return resp

        try:
            result = await run_with_retry(
                _retryable_attempt,
                policy=retry,
                idempotent=idempotent,
                deadline=deadline,
                clock=self.clock,
                sleep=self.sleep,
                rng=self.rng,
            )
            assert isinstance(result, RpcResponse)  # noqa: S101
            response = result
        except RpcError as err:
            response = RpcResponse.from_error(err.with_endpoint(service, method))

        # Terminal metrics for the whole call (after all retries/hedges).
        latency = max(0.0, self.clock.now() - start)
        metrics.observe_rpc(service, method, code=response.status.name, latency_s=latency)
        metrics.inc_rpc_retry(service, method, retry_count)
        metrics.inc_rpc_hedge(service, method, hedge_count)
        if response.status is RpcStatus.DEADLINE_EXCEEDED:
            metrics.inc_deadline_exceeded(service, method)
        return response

    async def _dispatch_once(
        self,
        service: str,
        method: str,
        payload: dict[str, object],
        *,
        context: RequestContext,
        deadline: Deadline,
        lb_policy: LoadBalancePolicy,
        hash_key: str | None,
        attempt: int,
    ) -> RpcResponse:
        """A single physical attempt against one instance (no retry/hedge here)."""
        if deadline.expired(clock=self.clock):
            raise deadline_exceeded(
                "deadline expired before dispatch", service=service, method=method
            )

        instances = [
            i
            for i in self.discovery.resolve(service)
            if not self.outliers.is_ejected(i.instance_id)
        ]
        if not instances:
            raise unavailable(
                f"no healthy instance for {service}", service=service, method=method
            )

        balancer = self._balancer(service, lb_policy)
        instance = balancer.pick(instances, hash_key=hash_key)
        if instance is None:
            raise unavailable(
                f"load balancer found no instance for {service}",
                service=service,
                method=method,
            )

        endpoint = f"{service}.{method}"
        breaker = self.breakers.get(endpoint)
        prev_state = breaker.state
        if not breaker.allow(clock=self.clock):
            metrics.inc_circuit_rejection(endpoint)
            raise breaker.reject_error().with_endpoint(service, method)

        # Build the wire request with propagated headers (child context per hop).
        hop_ctx = context.child()
        headers = hop_ctx.to_headers(clock=self.clock)
        request = RpcRequest(
            service=service,
            method=method,
            payload=payload,
            headers=headers,
            attempt=attempt,
        )
        transport = self.transport_resolver(instance)

        balancer.tracker.inc(instance.instance_id)
        try:
            with metrics.track_inflight(service, method):
                response = await transport.send(request)
        except RpcError as raised:
            self._record_outcome(
                breaker,
                instance,
                error=raised.with_endpoint(service, method),
                prev_state=prev_state,
            )
            raise
        finally:
            balancer.tracker.dec(instance.instance_id)

        # Classify the in-band response for the breaker / outlier detector.
        if response.ok:
            self._record_outcome(breaker, instance, error=None, prev_state=prev_state)
        else:
            in_band = response.to_error(service=service, method=method)
            counts = breaker.counts_against_breaker(in_band)
            self._record_outcome(
                breaker,
                instance,
                error=in_band if counts else None,
                prev_state=prev_state,
            )
        return response

    def _record_outcome(
        self,
        breaker: CircuitBreaker,
        instance: ServiceInstance,
        *,
        error: RpcError | None,
        prev_state: BreakerState,
    ) -> None:
        """Feed the breaker + outlier detector, and emit transition metrics."""
        success = error is None
        breaker.record(success=success, clock=self.clock)
        self.outliers.record(instance.instance_id, error)
        if breaker.state is not prev_state:
            metrics.inc_circuit_transition(breaker.name, breaker.state.value)


def constant_transport_resolver(transport: Transport) -> TransportResolver:
    """A resolver that returns the same transport for every instance.

    The in-process default: one shared :class:`InProcessTransport` serves every
    logical service, so a resolved instance just routes by ``service.method``.
    """

    def _resolve(_instance: ServiceInstance) -> Transport:
        return transport

    return _resolve


__all__ = [
    "CallOptions",
    "RpcClient",
    "TransportResolver",
    "constant_transport_resolver",
]
