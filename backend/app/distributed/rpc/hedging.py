"""Request hedging — beat tail latency by racing a backup request.

The p99 of a distributed call is dominated by the unlucky few that hit a slow
instance, a GC pause, a cold cache. Hedging (Google's "tail at scale" technique)
fixes this without over-provisioning: issue the primary request, and if it hasn't
answered within a *hedge delay* (typically the p95 latency), fire a second
request to a *different* instance and take whichever returns first. The slow
instance no longer sets your latency; the fast one does.

The guardrails that keep hedging from doubling load:

* **idempotent-only** — a hedge sends the request twice, so the method must be
  safe to run twice (a read, or a write keyed by ``shot_hash`` §12.1). The client
  refuses to hedge a non-idempotent method.
* **a hedge budget** — like the retry budget, hedges are capped as a fraction of
  primary traffic, so a system-wide slowdown can't make every call hedge and
  double the offered load exactly when it's already slow.
* **cancel-the-losers** — once one attempt wins, the others are cancelled
  (cooperatively, the same way a seek cancels a render §4.8), releasing their
  budget instead of finishing wasted work.

The race itself is structured-concurrency (``anyio`` task group) so a crash in one
leg never orphans the others; the *timing* (hedge delay) is driven by the injected
clock + sleep so tests are deterministic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.distributed.rpc.deadline import Clock, Deadline
from app.distributed.rpc.errors import RpcError, deadline_exceeded
from app.distributed.rpc.retry import SleepFn

#: One hedge attempt: ``async (attempt_index) -> result``. Each call should target
#: a (possibly) different instance; raising :class:`RpcError` is a failed leg.
HedgeAttempt = Callable[[int], Awaitable["object"]]


@dataclass
class HedgeBudget:
    """A token-bucket cap on hedges as a fraction of primary traffic.

    Mirrors :class:`~app.distributed.rpc.retry.RetryBudget`: each primary deposits
    ``ratio`` tokens; each hedge withdraws one; an empty bucket suppresses hedging.
    """

    ratio: float = 0.1
    max_tokens: float = 50.0
    _tokens: float = field(default=0.0, init=False)

    def record_primary(self) -> None:
        """Account a primary request; deposits ``ratio`` tokens."""
        self._tokens = min(self.max_tokens, self._tokens + self.ratio)

    def try_withdraw(self) -> bool:
        """Spend one hedge token if available."""
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def tokens(self) -> float:
        """Current token balance."""
        return self._tokens


@dataclass(frozen=True, slots=True)
class HedgePolicy:
    """Declarative hedging rules.

    ``delay_s`` is how long to wait for the primary before launching the first
    hedge (set it to your p95); ``max_hedges`` caps the extra in-flight copies.
    Disabled by default (``max_hedges == 0``) — a method opts in.
    """

    delay_s: float = 0.0
    max_hedges: int = 0
    budget: HedgeBudget = field(default_factory=HedgeBudget)

    @property
    def enabled(self) -> bool:
        """Whether this policy will ever hedge."""
        return self.max_hedges > 0 and self.delay_s >= 0.0


async def run_with_hedging(
    attempt_fn: HedgeAttempt,
    *,
    policy: HedgePolicy,
    idempotent: bool,
    deadline: Deadline,
    clock: Clock,
    sleep: SleepFn,
) -> object:
    """Race a primary request against up to ``max_hedges`` backups.

    Returns the first *successful* result and cancels the rest. If every launched
    leg fails, the last error is raised. Hedging is skipped entirely (a single
    plain attempt) when the policy is disabled, the method is non-idempotent, or
    the hedge budget is exhausted — so the function is always a safe drop-in for a
    single call.
    """
    if not policy.enabled or not idempotent:
        return await attempt_fn(0)

    import anyio

    policy.budget.record_primary()

    winner: list[object] = []
    errors: list[RpcError] = []

    async def _leg(index: int, cancel_scope_holder: list[anyio.CancelScope]) -> None:
        try:
            result = await attempt_fn(index)
        except RpcError as err:
            errors.append(err)
            return
        if not winner:
            winner.append(result)
            # Cancel the sibling legs — we have an answer.
            for scope in cancel_scope_holder:
                scope.cancel()

    launched = 0
    try:
        async with anyio.create_task_group() as tg:
            scopes: list[anyio.CancelScope] = []

            async def _supervised(index: int) -> None:
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await _leg(index, scopes)

            # Launch the primary immediately.
            tg.start_soon(_supervised, 0)
            launched = 1

            # Then launch each hedge after the delay, unless the primary already
            # won or the budget/deadline is spent.
            for hedge_index in range(1, policy.max_hedges + 1):
                remaining = deadline.remaining(clock=clock)
                if remaining <= 0.0:
                    break
                await sleep(min(policy.delay_s, remaining))
                if winner:
                    break
                if not policy.budget.try_withdraw():
                    break
                tg.start_soon(_supervised, hedge_index)
                launched += 1
    except RpcError:
        raise

    if winner:
        return winner[0]
    if deadline.expired(clock=clock):
        raise deadline_exceeded("all hedged attempts exhausted the deadline")
    if errors:
        raise errors[-1]
    # No leg ran (shouldn't happen since the primary always launches).
    raise deadline_exceeded("no hedge attempt completed")  # pragma: no cover


__all__ = [
    "HedgeAttempt",
    "HedgeBudget",
    "HedgePolicy",
    "run_with_hedging",
]
