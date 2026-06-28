"""``LLMOpsService`` — the façade the API + composition root wire to.

It binds the platform's pieces into one object with a small, intentional surface:

* a **prompt registry** seeded from the crew's live prompts (registry, diff,
  rollback, changelog);
* a **guardrail policy** (injection input + output policy) the API can run text
  through;
* a **run-trace store** + a convenience ``trace_call`` that records a guarded,
  timed, costed call;
* a **response cache**;
* a **model registry** (capability/cost routing);
* an **eval surface**: run a prompt version over a golden dataset (harness), A/B
  two versions, detect a regression on a candidate.

Construction is **pure and offline** — it seeds from ``app.agents.prompts`` and
the bundled fixtures, builds an in-memory trace store + cache + default catalog,
and uses the deterministic :class:`~app.llmops.judge.HeuristicJudge`. No model
call, no DB connection at construction time (mirroring the lazy composition root).
A caller that wants durability passes the DB-backed stores explicitly.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.llmops.ab import ABResult, ABRunner
from app.llmops.cache import ResponseCache
from app.llmops.datasets import DATASETS, GoldenDataset, get_dataset
from app.llmops.diff import PromptDiff
from app.llmops.guardrails import Decision, GuardrailPolicy, default_json_policy
from app.llmops.harness import EvalHarness, EvalReport, Responder
from app.llmops.judge import HeuristicJudge, Judge
from app.llmops.models_registry import ModelRegistry, default_catalog
from app.llmops.registry import PromptRecord, PromptRegistry
from app.llmops.regression import (
    RegressionPolicy,
    RegressionVerdict,
    detect_from_harness,
)
from app.llmops.rubric import RUBRICS, Rubric
from app.llmops.tracing import (
    InMemoryTraceStore,
    RunTrace,
    TraceQuery,
    TraceStore,
    aggregate,
    cost_of,
    group_by,
    new_trace_id,
)


@dataclass
class LLMOpsService:
    """The wired LLM-ops platform façade."""

    registry: PromptRegistry
    guardrails: GuardrailPolicy
    trace_store: TraceStore
    cache: ResponseCache
    models: ModelRegistry
    judge: Judge
    datasets: dict[str, GoldenDataset] = field(default_factory=lambda: dict(DATASETS))

    # -- construction -------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        *,
        trace_store: TraceStore | None = None,
        cache: ResponseCache | None = None,
        models: ModelRegistry | None = None,
        judge: Judge | None = None,
        guardrails: GuardrailPolicy | None = None,
        seed_from_agents: bool = True,
    ) -> LLMOpsService:
        """Build a fully wired service (pure, offline)."""
        registry = PromptRegistry.seeded_from_agents() if seed_from_agents else PromptRegistry()
        return cls(
            registry=registry,
            guardrails=guardrails or default_json_policy(),
            trace_store=trace_store or InMemoryTraceStore(),
            cache=cache or ResponseCache(),
            models=models or default_catalog(),
            judge=judge or HeuristicJudge(),
        )

    # -- prompt registry ----------------------------------------------------- #

    def register_prompt(
        self,
        key: str,
        system: str,
        *,
        bump: str | None = None,
        author: str = "operator",
        summary: str | None = None,
    ) -> PromptRecord:
        return self.registry.register(key, system, bump=bump, author=author, summary=summary)

    def rollback_prompt(self, key: str, *, to: str | None = None) -> PromptRecord:
        return self.registry.rollback(key, to=to)

    async def promote_with_gate(
        self,
        key: str,
        candidate_version: str,
        *,
        dataset_name: str,
        runs: int = 3,
        policy: RegressionPolicy | None = None,
        author: str = "operator",
    ) -> tuple[bool, RegressionVerdict]:
        """Promote a candidate to active ONLY if it does not regress vs the active.

        Ties the eval harness to the registry: runs the candidate and the current
        active version over ``dataset_name``, and promotes the candidate only when
        :func:`regression.detect` finds no regression. Returns
        ``(promoted, verdict)`` so the caller can surface *why* a promotion was
        blocked. This is the "don't ship a prompt that silently degrades the crew"
        gate referenced in ``DESIGN.md``.
        """
        verdict, _baseline, _candidate = await self.check_regression(
            prompt_key=key,
            candidate_version=candidate_version,
            dataset_name=dataset_name,
            runs=runs,
            policy=policy,
        )
        if verdict.regressed:
            return False, verdict
        self.registry.promote(key, candidate_version, author=author)
        return True, verdict

    def diff_prompt(self, key: str, *, old: str, new: str) -> PromptDiff:
        return self.registry.diff(key, old=old, new=new)

    def active_prompt(self, key: str) -> PromptRecord:
        return self.registry.get_active(key)

    # -- guardrails ---------------------------------------------------------- #

    def guard_input(self, text: str) -> dict[str, Any]:
        verdict = self.guardrails.check_input(text)
        return {
            "decision": verdict.decision.value,
            "score": verdict.scan.score,
            "categories": [c.value for c in verdict.scan.categories],
            "reasons": list(verdict.reasons),
            "safe_text": verdict.safe_text,
        }

    def guard_output(
        self, text: str, *, protected_texts: list[str] | None = None
    ) -> dict[str, Any]:
        verdict = self.guardrails.check_output(text, protected_texts=protected_texts)
        return {
            "decision": verdict.decision.value,
            "max_severity": verdict.report.max_severity.name,
            "violations": [
                {"kind": v.kind.value, "severity": v.severity.name, "detail": v.detail}
                for v in verdict.report.violations
            ],
            "reasons": list(verdict.reasons),
        }

    # -- traced call --------------------------------------------------------- #

    async def trace_call(
        self,
        *,
        prompt_key: str,
        prompt_version: str,
        model: str,
        inputs: dict[str, Any],
        producer: Callable[[], Awaitable[str]],
        temperature: float | None = None,
        cacheable: bool = False,
        book_id: str | None = None,
        session_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        guard: bool = True,
    ) -> RunTrace:
        """Run a model call with guardrails + cache + tracing, returning the trace.

        The ``producer`` is the actual (async) call; this wraps it: it optionally
        serves from cache, times the call, prices it from the model registry, and
        records a :class:`RunTrace`. The platform never *constructs* a live
        producer itself — the caller supplies it.
        """
        guard_decision: str | None = None
        if guard:
            # Guard the serialized inputs as one untrusted blob.
            blob = " ".join(str(v) for v in inputs.values())
            input_verdict = self.guardrails.check_input(blob)
            guard_decision = input_verdict.decision.value
            if input_verdict.decision is Decision.BLOCK:
                trace = self._make_trace(
                    prompt_key=prompt_key,
                    prompt_version=prompt_version,
                    model=model,
                    inputs=inputs,
                    output="",
                    latency_ms=0.0,
                    input_tokens=input_tokens,
                    output_tokens=0,
                    cache_hit=False,
                    guardrail_decision=guard_decision,
                    error="input blocked by guardrail",
                    book_id=book_id,
                    session_id=session_id,
                )
                self.trace_store.record(trace)
                return trace

        started = time.monotonic()
        if cacheable:
            output, cache_hit = await self.cache.get_or_set(
                producer,
                prompt_key=prompt_key,
                prompt_version=prompt_version,
                model=model,
                inputs=inputs,
                temperature=temperature,
            )
        else:
            output, cache_hit = await producer(), False
        latency_ms = (time.monotonic() - started) * 1000.0

        trace = self._make_trace(
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            model=model,
            inputs=inputs,
            output=output,
            latency_ms=round(latency_ms, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit=cache_hit,
            guardrail_decision=guard_decision,
            error=None,
            book_id=book_id,
            session_id=session_id,
        )
        self.trace_store.record(trace)
        return trace

    def _make_trace(self, **kw: Any) -> RunTrace:
        cost = (
            Decimal("0")
            if kw["cache_hit"]
            else cost_of(kw["model"], kw["input_tokens"], kw["output_tokens"], registry=self.models)
        )
        from datetime import UTC, datetime

        return RunTrace(
            id=new_trace_id(),
            cost_usd=cost,
            created_at=datetime.now(UTC),
            **kw,
        )

    def query_traces(self, q: TraceQuery) -> list[RunTrace]:
        return self.trace_store.query(q)

    def trace_rollup(self, q: TraceQuery, *, group: str | None = None) -> dict[str, Any]:
        traces = self.trace_store.query(q.all_matching())
        if group is None:
            return aggregate(traces).to_dict()
        return {k: v.to_dict() for k, v in group_by(traces, group).items()}

    # -- eval surface -------------------------------------------------------- #

    async def evaluate(
        self,
        *,
        prompt_key: str,
        dataset_name: str,
        version: str | None = None,
        responder: Responder | None = None,
        runs: int = 3,
    ) -> EvalReport:
        """Run a prompt version over a golden dataset and return the §13 report."""
        record = (
            self.active_prompt(prompt_key)
            if version is None
            else self.registry.get(prompt_key, version)
        )
        dataset = self.datasets.get(dataset_name) or get_dataset(dataset_name)
        harness = EvalHarness(judge=self.judge)
        return await harness.run(
            prompt_key=prompt_key,
            prompt_version=record.version,
            system=record.system,
            dataset=dataset,
            responder=responder,
            runs=runs,
        )

    async def ab_test(
        self,
        *,
        prompt_key: str,
        version_a: str,
        version_b: str,
        dataset_name: str,
        responder_a: Responder | None = None,
        responder_b: Responder | None = None,
        runs: int = 3,
    ) -> ABResult:
        rec_a = self.registry.get(prompt_key, version_a)
        rec_b = self.registry.get(prompt_key, version_b)
        dataset = self.datasets.get(dataset_name) or get_dataset(dataset_name)
        return await ABRunner(judge=self.judge).compare(
            prompt_key=prompt_key,
            version_a=rec_a.version,
            system_a=rec_a.system,
            version_b=rec_b.version,
            system_b=rec_b.system,
            dataset=dataset,
            responder_a=responder_a,
            responder_b=responder_b,
            runs=runs,
        )

    async def check_regression(
        self,
        *,
        prompt_key: str,
        candidate_version: str,
        dataset_name: str,
        baseline_version: str | None = None,
        responder: Responder | None = None,
        runs: int = 3,
        policy: RegressionPolicy | None = None,
    ) -> tuple[RegressionVerdict, EvalReport, EvalReport]:
        """Run candidate vs baseline (default = active) and detect a regression."""
        baseline = (
            self.active_prompt(prompt_key)
            if baseline_version is None
            else self.registry.get(prompt_key, baseline_version)
        )
        candidate = self.registry.get(prompt_key, candidate_version)
        dataset = self.datasets.get(dataset_name) or get_dataset(dataset_name)
        return await detect_from_harness(
            prompt_key=prompt_key,
            baseline_version=baseline.version,
            baseline_system=baseline.system,
            candidate_version=candidate.version,
            candidate_system=candidate.system,
            dataset=dataset,
            judge=self.judge,
            responder=responder,
            runs=runs,
            policy=policy,
        )

    # -- introspection ------------------------------------------------------- #

    def rubrics(self) -> dict[str, Rubric]:
        return dict(RUBRICS)

    def dataset_names(self) -> list[str]:
        return sorted(self.datasets)


__all__ = ["LLMOpsService"]
