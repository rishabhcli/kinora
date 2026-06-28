"""The pure flag evaluator — a total function from (snapshot, key, context) → result.

``FlagEvaluator`` holds an immutable :class:`~app.flags.models.FlagSnapshot` and
evaluates a flag for an :class:`~app.flags.context.EvalContext` by walking a
fixed waterfall (see ``DESIGN.md``). It never performs I/O and never raises into
the caller: any internal inconsistency degrades to the flag's default with
``Reason.ERROR``, so a buggy rule can never take a request path down.

Prerequisite resolution is depth-bounded and cycle-safe (a cycle is treated as a
failed prerequisite), so a misconfigured dependency graph can't recurse forever.
"""

from __future__ import annotations

from app.flags.context import EvalContext
from app.flags.hashing import bucket_bp
from app.flags.models import (
    Evaluation,
    Flag,
    FlagSnapshot,
    Reason,
    Rollout,
    Rule,
)
from app.flags.rollout import resolve_rollout
from app.flags.targeting import rule_matches

#: Hard cap on prerequisite chain depth (defence against deep/cyclic graphs).
_MAX_PREREQ_DEPTH = 25


class FlagEvaluator:
    """Evaluate flags against a fixed snapshot. Cheap to construct, fully pure."""

    def __init__(self, snapshot: FlagSnapshot, *, default_salt: str = "") -> None:
        self._snapshot = snapshot
        self._default_salt = default_salt

    @property
    def snapshot(self) -> FlagSnapshot:
        """The snapshot this evaluator reads."""
        return self._snapshot

    def evaluate(
        self,
        flag_key: str,
        context: EvalContext,
        *,
        default: object = None,
    ) -> Evaluation:
        """Evaluate ``flag_key`` for ``context``; never raises.

        ``default`` is the value returned only when the flag is *absent* from the
        snapshot (``FLAG_NOT_FOUND``) — for a present flag the value always comes
        from one of its own variations.
        """
        flag = self._snapshot.get(flag_key)
        if flag is None:
            return Evaluation(
                flag_key=flag_key,
                value=default,
                variation_key=None,
                variation_index=None,
                reason=Reason.FLAG_NOT_FOUND,
            )
        try:
            return self._evaluate_flag(flag, context, set())
        except Exception:  # noqa: BLE001 - total function: degrade, never raise
            return self._serve(flag, flag.default_variation, Reason.ERROR)

    # --- internals ------------------------------------------------------ #

    def _evaluate_flag(self, flag: Flag, context: EvalContext, seen: set[str]) -> Evaluation:
        if flag.archived:
            return self._serve(flag, flag.default_variation, Reason.FLAG_ARCHIVED)
        if not flag.enabled:
            return self._serve(flag, flag.default_variation, Reason.FLAG_OFF)

        if not self._prerequisites_pass(flag, context, seen):
            return self._serve(flag, flag.default_variation, Reason.PREREQUISITE_FAILED)

        # Individual targeting beats rules (an explicit pin is the strongest signal).
        for target in flag.targets:
            if context.key in target.keys:
                return self._serve(flag, target.variation, Reason.TARGET_MATCH)

        for rule in flag.rules:
            if rule_matches(rule, context):
                return self._serve_rule(flag, rule, context)

        return self._serve_rollout(flag, flag.fallthrough, context, Reason.FALLTHROUGH)

    def _prerequisites_pass(self, flag: Flag, context: EvalContext, seen: set[str]) -> bool:
        if not flag.prerequisites:
            return True
        if flag.key in seen or len(seen) >= _MAX_PREREQ_DEPTH:
            # Cycle or runaway depth → treat as unsatisfiable (fail safe).
            return False
        seen = seen | {flag.key}
        for prereq in flag.prerequisites:
            dep = self._snapshot.get(prereq.flag_key)
            if dep is None:
                return False
            result = self._evaluate_flag(dep, context, seen)
            if result.variation_key != prereq.variation:
                return False
        return True

    def _serve_rule(self, flag: Flag, rule: Rule, context: EvalContext) -> Evaluation:
        if rule.variation is not None:
            return self._serve(flag, rule.variation, Reason.RULE_MATCH, rule_id=rule.id)
        assert rule.rollout is not None
        return self._serve_rollout(
            flag, rule.rollout, context, Reason.RULE_MATCH, rule_id=rule.id
        )

    def _serve_rollout(
        self,
        flag: Flag,
        rollout: Rollout,
        context: EvalContext,
        reason: Reason,
        *,
        rule_id: str | None = None,
    ) -> Evaluation:
        salt = self._rollout_salt(flag)
        chosen = resolve_rollout(rollout, context, default_salt=salt)
        return self._serve(flag, chosen, reason, rule_id=rule_id)

    def _rollout_salt(self, flag: Flag) -> str:
        # Flag key (optionally namespaced by the platform default salt) keeps each
        # flag's bucketing independent of every other flag's.
        return f"{self._default_salt}:{flag.key}" if self._default_salt else flag.key

    def _serve(
        self, flag: Flag, variation_key: str, reason: Reason, *, rule_id: str | None = None
    ) -> Evaluation:
        variation = flag.variation_by_key(variation_key)
        return Evaluation(
            flag_key=flag.key,
            value=variation.value,
            variation_key=variation.key,
            variation_index=flag.variation_index(variation.key),
            reason=reason,
            flag_version=flag.version,
            rule_id=rule_id,
        )

    # --- diagnostics ---------------------------------------------------- #

    def bucket(self, flag_key: str, context: EvalContext) -> int | None:
        """The raw fallthrough bucket (0..9999) a context would land in.

        Useful for the admin "why" panel and bucketing sanity checks. ``None``
        for an unknown flag.
        """
        flag = self._snapshot.get(flag_key)
        if flag is None:
            return None
        salt = self._rollout_salt(flag)
        ft = flag.fallthrough
        unit = context.unit_for(ft.bucket_by)
        effective = ft.salt or salt
        if ft.seed:
            effective = f"{effective}#{ft.seed}"
        return bucket_bp(unit, effective)


__all__ = ["FlagEvaluator"]
