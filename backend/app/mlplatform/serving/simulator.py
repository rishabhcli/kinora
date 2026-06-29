"""The discrete-event serving simulator — the serving brain under load.

This is the model that predicts throughput / latency / cost without a GPU. It runs
a **step-driven** discrete-event loop over the continuous-batching scheduler:

    while work remains:
        1. advance the clock by one decode-step duration
        2. admit any requests that have arrived (queue → batch), subject to limits
        3. PREFILL: newly-admitted sequences pay their prompt pass (sets TTFT)
        4. DECODE: every running sequence emits tokens this step
           (1 token in plain mode, E[accepted] in speculative mode)
        5. grow each sequence's KV-cache; if the cache is exhausted, preempt a victim
        6. retire finished sequences, free their cache
        7. record per-step occupancy + KV utilization for the metrics report

A "step" is one decode iteration of the batch. Its wall-clock duration is the
decode cost of the *largest* sequence in the batch (decode is bandwidth-bound and
the batch shares the weight read) plus a tiny per-extra-sequence overhead — the same
shape real continuous-batching servers exhibit. Prefill is charged as an
amortized-into-the-step cost on the admission step so TTFT reflects queue wait +
prefill honestly.

Determinism: every stochastic quantity (arrivals, lengths, speculative acceptance)
is seeded, so a given :class:`SimConfig` + workload always produces the identical
report. That is what makes the invariants property-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.mlplatform.serving.batching import BatchScheduler, ContinuousBatchConfig
from app.mlplatform.serving.errors import InvariantViolationError, ServingConfigError
from app.mlplatform.serving.kvcache import PagedKVCache, PagedKVConfig
from app.mlplatform.serving.metrics import RunAccumulator, ServingReport, summarize_run
from app.mlplatform.serving.model import ModelProfile
from app.mlplatform.serving.requests import InferenceRequest, RequestState
from app.mlplatform.serving.speculative import SpeculativeConfig, SpeculativeDecoder


@dataclass(frozen=True, slots=True)
class SimConfig:
    """Everything needed to run one serving simulation.

    Couples the model's serving profile with the cache, batch, and speculative
    configs. ``shared_prefix_key`` — when set — declares that *every* request shares
    one prompt prefix (the Kinora read-ahead case where shots reuse the same canon
    slice), letting the KV-cache model prefix reuse end-to-end.
    """

    profile: ModelProfile
    cache: PagedKVConfig
    batch: ContinuousBatchConfig = field(default_factory=ContinuousBatchConfig)
    speculative: SpeculativeConfig = field(default_factory=SpeculativeConfig)
    #: Hard cap on simulated steps; guards against a runaway loop (a bug).
    max_steps: int = 1_000_000
    #: If set, all requests share this prefix key for KV-cache reuse.
    shared_prefix_key: str | None = None
    #: Decode tokens committed per step in plain mode (always 1 for true AR decode).
    tokens_per_step_plain: int = 1

    def __post_init__(self) -> None:
        if self.max_steps <= 0:
            raise ServingConfigError("max_steps must be positive")
        if self.tokens_per_step_plain < 1:
            raise ServingConfigError("tokens_per_step_plain must be >= 1")
        # The batch token budget cannot exceed what the KV-cache can physically hold,
        # or the scheduler would admit work the cache must immediately preempt.
        cache_token_capacity = self.cache.total_blocks * self.cache.block_tokens
        if self.batch.max_batch_tokens > cache_token_capacity:
            raise ServingConfigError(
                f"max_batch_tokens {self.batch.max_batch_tokens} exceeds KV-cache "
                f"token capacity {cache_token_capacity}"
            )


@dataclass(slots=True)
class _StepCost:
    """The wall-clock cost (ms) and committed-token plan for one decode step."""

    duration_ms: float
    tokens_per_seq: dict[str, int]


class ServingSimulator:
    """Runs a :class:`SimConfig` over a request stream and returns a report.

    Stateless across runs: construct once, call :meth:`run` with a workload. The
    instance rebuilds its cache/scheduler each run so reports are independent.
    """

    def __init__(self, config: SimConfig) -> None:
        self.config = config
        self._decoder = SpeculativeDecoder(config.speculative)

    def run(self, requests: list[InferenceRequest]) -> ServingReport:
        """Simulate serving ``requests`` and return the capacity report.

        The caller's request objects are never mutated: the simulator works on fresh
        copies, so the same request list can be replayed across configurations.
        """
        cfg = self.config
        cache = PagedKVCache(cfg.cache)
        scheduler = BatchScheduler(cfg.batch, cache)
        accumulator = RunAccumulator()

        # Work on copies so re-running the same workload under different configs is
        # safe (the runtime fields — state/generated/timestamps — are mutated here).
        work = [replace(r) for r in requests]
        # Sort arrivals deterministically (arrival, then id).
        pending = sorted(work, key=lambda r: (r.arrival_ms, r.request_id))
        arrival_cursor = 0
        completed: list[InferenceRequest] = []
        failed: list[InferenceRequest] = []
        clock = 0.0
        steps = 0

        prefix_of = self._prefix_map(work)

        while True:
            steps += 1
            if steps > cfg.max_steps:
                raise InvariantViolationError(
                    f"simulation exceeded max_steps={cfg.max_steps} — likely a loop bug"
                )

            # 1) If nothing is running, jump the clock to the next arrival to avoid
            #    burning empty steps (event-driven idle skip).
            if scheduler.n_running == 0 and arrival_cursor < len(pending):
                next_arrival = pending[arrival_cursor].arrival_ms
                if clock < next_arrival:
                    clock = next_arrival

            # 2) Admit any requests that have arrived by `clock`.
            while arrival_cursor < len(pending) and pending[arrival_cursor].arrival_ms <= clock:
                scheduler.enqueue(pending[arrival_cursor])
                arrival_cursor += 1
            decision = scheduler.schedule(clock, prefix_of=prefix_of)
            scheduler.apply(decision, clock, prefix_of=prefix_of)

            # 3) PREFILL: charge the prompt pass for newly admitted sequences and
            #    mark their first token time once prefill completes (this step).
            prefill_ms = 0.0
            for req in decision.admit:
                prefill_ms = max(prefill_ms, req.prompt_tokens * cfg.profile.prefill_ms_per_token)

            # 4) Compute this decode step's cost + per-sequence token plan.
            step = self._step_cost(scheduler.running)
            duration = prefill_ms + step.duration_ms
            clock += duration

            # 5) Apply decode: grow KV-cache, advance generated counts, retire/preempt.
            self._advance_decode(scheduler, step, clock, completed)

            # 6) Record per-step telemetry.
            accumulator.observe_step(scheduler.n_running, cache.utilization)

            # 7) Termination: every request arrived AND nothing left to do.
            if arrival_cursor >= len(pending) and scheduler.is_idle:
                break
            # Safety: if nothing is running and nothing waiting but arrivals remain,
            # the idle-skip at the top of the next loop will advance the clock.

        report = summarize_run(
            completed,
            failed,
            cost_per_1k_tokens=cfg.profile.cost_per_1k_tokens,
            wall_clock_ms=clock,
            accumulator=accumulator,
            kv_reuse_ratio=self._reuse_ratio(cache),
            speculative_speedup=(
                self._decoder.outcome().speedup if self._decoder.is_active() else 1.0
            ),
        )
        self._final_invariants(scheduler, completed, work)
        return report

    # -- step mechanics ---------------------------------------------------- #

    def _step_cost(self, running: tuple[InferenceRequest, ...]) -> _StepCost:
        """Cost + token plan for decoding the current batch one iteration.

        Per-sequence committed tokens: plain mode commits ``tokens_per_step_plain``;
        speculative mode commits a seeded per-request accepted count. The step's
        duration is the max single-sequence decode cost (shared weight read) plus a
        small per-extra-sequence overhead.
        """
        cfg = self.config
        tokens_per_seq: dict[str, int] = {}
        if not running:
            return _StepCost(duration_ms=0.0, tokens_per_seq={})
        max_seq_ms = 0.0
        spec_active = self._decoder.is_active()
        for req in running:
            if spec_active:
                committed = self._decoder.simulate_block(req.request_id, req.generated)
            else:
                committed = cfg.tokens_per_step_plain
            committed = min(committed, req.remaining_tokens) if req.remaining_tokens else 0
            tokens_per_seq[req.request_id] = max(0, committed)
            # In speculative mode the *target* still pays one verify pass; cost is the
            # spec cost-per-token ratio times committed tokens, floored at one step.
            if spec_active:
                ratio = self._decoder.outcome().cost_per_token_ratio
                seq_ms = max(1, committed) * ratio * cfg.profile.decode_ms_per_token
            else:
                seq_ms = cfg.profile.decode_ms_per_token
            max_seq_ms = max(max_seq_ms, seq_ms)
        overhead = cfg.profile.batch_overhead_ms_per_seq * max(0, len(running) - 1)
        return _StepCost(duration_ms=max_seq_ms + overhead, tokens_per_seq=tokens_per_seq)

    def _advance_decode(
        self,
        scheduler: BatchScheduler,
        step: _StepCost,
        clock: float,
        completed: list[InferenceRequest],
    ) -> None:
        """Apply the token plan: grow cache, set timestamps, retire/preempt."""
        finished_ids: list[str] = []
        for req in list(scheduler.running):
            committed = step.tokens_per_seq.get(req.request_id, 0)
            if req.state == RequestState.PREFILL:
                # Prefill just completed this step → first token emitted now.
                req.state = RequestState.DECODING
                if req.first_token_ms is None:
                    req.first_token_ms = clock
            if committed <= 0:
                continue
            req.generated += committed
            if req.generated > req.target_tokens:
                req.generated = req.target_tokens
            # Grow KV-cache for the new context length. If exhausted, preempt a victim
            # and skip growth this step (the victim's blocks free up room).
            try:
                scheduler.cache.grow(req.request_id, req.total_context_tokens)
            except Exception:  # noqa: BLE001 — CapacityError handled by preemption
                victim = scheduler.victim_for_preemption()
                if victim is not None and victim.request_id != req.request_id:
                    scheduler.preempt(victim.request_id)
                # Re-try growth once room is freed; if still no room, preempt self.
                try:
                    scheduler.cache.grow(req.request_id, req.total_context_tokens)
                except Exception:  # noqa: BLE001
                    scheduler.preempt(req.request_id)
                    continue
            if req.remaining_tokens == 0:
                req.state = RequestState.DONE
                req.finish_ms = clock
                finished_ids.append(req.request_id)
        for rid in finished_ids:
            completed.append(scheduler.complete(rid))
        scheduler.assert_invariants()

    # -- helpers ----------------------------------------------------------- #

    def _prefix_map(self, requests: list[InferenceRequest]) -> dict[str, str]:
        if self.config.shared_prefix_key is None:
            return {}
        return {r.request_id: self.config.shared_prefix_key for r in requests}

    @staticmethod
    def _reuse_ratio(cache: PagedKVCache) -> float:
        total = cache.reused_blocks_total + cache.allocated_blocks_total
        if total == 0:
            return 0.0
        return cache.reused_blocks_total / total

    def _final_invariants(
        self,
        scheduler: BatchScheduler,
        completed: list[InferenceRequest],
        original: list[InferenceRequest],
    ) -> None:
        """End-of-run sanity: no request is lost, the cache drained, nothing starved."""
        scheduler.assert_invariants()
        if not scheduler.is_idle:
            raise InvariantViolationError("simulator ended with work still in flight")
        # Every original request must have completed (none starved). Failed requests
        # would also appear; this simulator never fails a request, so completed == all.
        if len(completed) != len(original):
            done_ids = {r.request_id for r in completed}
            missing = sorted(r.request_id for r in original if r.request_id not in done_ids)
            raise InvariantViolationError(f"requests starved / never completed: {missing}")
        # KV-cache must fully drain when idle.
        if scheduler.cache.used_blocks != 0:
            raise InvariantViolationError(
                f"KV-cache leaked {scheduler.cache.used_blocks} blocks at end of run"
            )


__all__ = ["ServingSimulator", "SimConfig"]
