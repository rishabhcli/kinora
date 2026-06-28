"""The pure flag data model — frozen, validated, infra-free dataclasses.

A :class:`Flag` is a self-describing evaluable unit: a list of typed
:class:`Variation`\\ s, optional :class:`Prerequisite`\\ s and individual
:class:`Target`\\ s, an ordered list of targeting :class:`Rule`\\ s, and a
``fallthrough`` :class:`Rollout` for everyone who matched no rule. The
constructors validate structural invariants up front (every variation/rule
reference resolves, rollout weights sum to 100%, indices are in range) so the
evaluator can stay a total function that never has to defend against a malformed
flag at runtime.

All types are ``frozen`` so a :class:`FlagSnapshot` is safely shareable across
threads/tasks and cacheable by value.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from app.flags.context import AttrValue, EvalContext
from app.flags.errors import FlagValidationError
from app.flags.hashing import TOTAL_BASIS_POINTS


class FlagKind(StrEnum):
    """The value type a flag's variations carry."""

    BOOLEAN = "boolean"
    STRING = "string"
    NUMBER = "number"
    JSON = "json"


class Operator(StrEnum):
    """Predicate operators a :class:`Clause` can apply to an attribute."""

    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    MATCHES = "matches"  # regex
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    SEMVER_GT = "semver_gt"
    SEMVER_GTE = "semver_gte"
    SEMVER_LT = "semver_lt"
    SEMVER_LTE = "semver_lte"
    SEMVER_EQ = "semver_eq"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"
    PERCENTAGE = "percentage"  # value = [bucket_lo_bp, bucket_hi_bp]


class Reason(StrEnum):
    """Why the evaluator returned the value it did (attached to every result)."""

    FLAG_NOT_FOUND = "flag_not_found"
    FLAG_ARCHIVED = "flag_archived"
    FLAG_OFF = "flag_off"
    PREREQUISITE_FAILED = "prerequisite_failed"
    TARGET_MATCH = "target_match"
    RULE_MATCH = "rule_match"
    FALLTHROUGH = "fallthrough"
    DEFAULT = "default"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Variation:
    """One possible value a flag can serve, addressed by a stable ``key``."""

    key: str
    value: Any
    name: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise FlagValidationError("variation.key must be non-empty")


@dataclass(frozen=True, slots=True)
class Clause:
    """A single attribute predicate: ``attribute <op> values`` (negatable)."""

    attribute: str
    op: Operator
    values: tuple[AttrValue, ...] = ()
    negate: bool = False

    def __post_init__(self) -> None:
        if not self.attribute:
            raise FlagValidationError("clause.attribute must be non-empty")
        unary = {Operator.EXISTS, Operator.NOT_EXISTS}
        if self.op not in unary and not self.values:
            raise FlagValidationError(
                f"clause op {self.op.value!r} on {self.attribute!r} requires values"
            )


@dataclass(frozen=True, slots=True)
class WeightedVariation:
    """A variation key with an integer basis-point weight (for multivariate)."""

    variation: str
    weight: int  # basis points; the set must sum to TOTAL_BASIS_POINTS

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise FlagValidationError("weighted variation weight must be >= 0")


@dataclass(frozen=True, slots=True)
class Rollout:
    """A weighted split of traffic across variations, bucketed deterministically.

    ``weights`` partition 100% of the (bucketing-unit) traffic across variation
    keys. ``bucket_by`` names the attribute to bucket on (``None`` → the context
    key). ``salt`` overrides the bucketing salt (defaults to the flag key at
    evaluation time, so two flags with identical rollouts do not correlate).
    ``seed`` further perturbs the salt to let an operator *reshuffle* a rollout
    without changing its shape.
    """

    weights: tuple[WeightedVariation, ...]
    bucket_by: str | None = None
    salt: str | None = None
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.weights:
            raise FlagValidationError("rollout must have at least one weighted variation")
        total = sum(w.weight for w in self.weights)
        if total != TOTAL_BASIS_POINTS:
            raise FlagValidationError(
                f"rollout weights must sum to {TOTAL_BASIS_POINTS} bp (got {total})"
            )

    @classmethod
    def single(cls, variation: str) -> Rollout:
        """A degenerate rollout that always serves ``variation`` (100%)."""
        return cls(weights=(WeightedVariation(variation, TOTAL_BASIS_POINTS),))

    @classmethod
    def even(cls, variations: tuple[str, ...], *, bucket_by: str | None = None) -> Rollout:
        """An even split across ``variations`` (remainder added to the first)."""
        if not variations:
            raise FlagValidationError("even rollout needs at least one variation")
        each = TOTAL_BASIS_POINTS // len(variations)
        remainder = TOTAL_BASIS_POINTS - each * len(variations)
        weights = tuple(
            WeightedVariation(v, each + (remainder if i == 0 else 0))
            for i, v in enumerate(variations)
        )
        return cls(weights=weights, bucket_by=bucket_by)

    def variation_keys(self) -> tuple[str, ...]:
        """The set of variation keys this rollout can serve."""
        return tuple(w.variation for w in self.weights)


@dataclass(frozen=True, slots=True)
class Rule:
    """An ordered targeting rule: when every clause matches, serve the rule.

    A rule serves either a single ``variation`` or a ``rollout`` (a within-rule
    split — e.g. "EU users: 50/50 A/B"). Exactly one must be set.
    """

    id: str
    clauses: tuple[Clause, ...]
    variation: str | None = None
    rollout: Rollout | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise FlagValidationError("rule.id must be non-empty")
        if (self.variation is None) == (self.rollout is None):
            raise FlagValidationError(
                f"rule {self.id!r} must set exactly one of variation / rollout"
            )


@dataclass(frozen=True, slots=True)
class Prerequisite:
    """A dependency on another flag serving an expected variation."""

    flag_key: str
    variation: str

    def __post_init__(self) -> None:
        if not self.flag_key or not self.variation:
            raise FlagValidationError("prerequisite needs flag_key and variation")


@dataclass(frozen=True, slots=True)
class Target:
    """Pin specific context keys to a variation (individual targeting)."""

    variation: str
    keys: frozenset[str]

    def __post_init__(self) -> None:
        if not self.keys:
            raise FlagValidationError("target must list at least one key")


@dataclass(frozen=True, slots=True)
class Flag:
    """A complete, self-validating evaluable feature flag."""

    key: str
    kind: FlagKind
    variations: tuple[Variation, ...]
    default_variation: str  # served when the flag is OFF
    fallthrough: Rollout  # served to everyone who matched no rule when ON
    enabled: bool = True
    archived: bool = False
    prerequisites: tuple[Prerequisite, ...] = ()
    targets: tuple[Target, ...] = ()
    rules: tuple[Rule, ...] = ()
    version: int = 1
    name: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key:
            raise FlagValidationError("flag.key must be non-empty")
        if not self.variations:
            raise FlagValidationError(f"flag {self.key!r} must have at least one variation")
        keys = [v.key for v in self.variations]
        if len(set(keys)) != len(keys):
            raise FlagValidationError(f"flag {self.key!r} has duplicate variation keys")
        known = set(keys)
        if self.default_variation not in known:
            raise FlagValidationError(
                f"flag {self.key!r} default_variation {self.default_variation!r} is unknown"
            )
        self._validate_references(known)
        if self.kind is FlagKind.BOOLEAN:
            self._validate_boolean()

    def _validate_references(self, known: set[str]) -> None:
        for target in self.targets:
            if target.variation not in known:
                raise FlagValidationError(
                    f"flag {self.key!r} target -> unknown variation {target.variation!r}"
                )
        for wv in self.fallthrough.weights:
            if wv.variation not in known:
                raise FlagValidationError(
                    f"flag {self.key!r} fallthrough -> unknown variation {wv.variation!r}"
                )
        rule_ids: set[str] = set()
        for rule in self.rules:
            if rule.id in rule_ids:
                raise FlagValidationError(f"flag {self.key!r} has duplicate rule id {rule.id!r}")
            rule_ids.add(rule.id)
            if rule.variation is not None and rule.variation not in known:
                raise FlagValidationError(
                    f"flag {self.key!r} rule {rule.id!r} -> unknown variation"
                )
            if rule.rollout is not None:
                for wv in rule.rollout.weights:
                    if wv.variation not in known:
                        raise FlagValidationError(
                            f"flag {self.key!r} rule {rule.id!r} rollout -> unknown variation"
                        )

    def _validate_boolean(self) -> None:
        for v in self.variations:
            if not isinstance(v.value, bool):
                raise FlagValidationError(
                    f"boolean flag {self.key!r} variation {v.key!r} value must be bool"
                )

    # --- lookups -------------------------------------------------------- #

    def variation_by_key(self, key: str) -> Variation:
        """Return the variation with ``key`` (KeyError-safe: validated at build)."""
        for v in self.variations:
            if v.key == key:
                return v
        raise FlagValidationError(f"flag {self.key!r} has no variation {key!r}")

    def variation_index(self, key: str) -> int:
        """The positional index of variation ``key`` (for compact exposure logs)."""
        for i, v in enumerate(self.variations):
            if v.key == key:
                return i
        raise FlagValidationError(f"flag {self.key!r} has no variation {key!r}")

    def with_version(self, version: int) -> Flag:
        """Return a copy stamped with ``version`` (used by the store on write)."""
        return replace(self, version=version)

    @classmethod
    def boolean(
        cls,
        key: str,
        *,
        enabled: bool = True,
        default: bool = False,
        rollout_percent: float | None = None,
        **kwargs: Any,
    ) -> Flag:
        """Build a canonical 2-variation boolean flag (``on``/``off``).

        With ``rollout_percent`` set, the fallthrough serves ``on`` to that
        percentage and ``off`` to the rest (a gradual boolean rollout). Without
        it, the fallthrough serves ``on`` to everyone (the flag is a simple
        on/off switch gated by ``enabled``).
        """
        on = Variation("on", True)
        off = Variation("off", False)
        default_key = "on" if default else "off"
        if rollout_percent is None:
            fallthrough = Rollout.single("on")
        else:
            pct_bp = round(rollout_percent * 100)
            pct_bp = max(0, min(TOTAL_BASIS_POINTS, pct_bp))
            fallthrough = Rollout(
                weights=(
                    WeightedVariation("on", pct_bp),
                    WeightedVariation("off", TOTAL_BASIS_POINTS - pct_bp),
                )
            )
        return cls(
            key=key,
            kind=FlagKind.BOOLEAN,
            variations=(on, off),
            default_variation=default_key,
            fallthrough=fallthrough,
            enabled=enabled,
            **kwargs,
        )

    @classmethod
    def multivariate(
        cls,
        key: str,
        variations: tuple[Variation, ...],
        *,
        default: str,
        fallthrough: Rollout | None = None,
        kind: FlagKind = FlagKind.STRING,
        **kwargs: Any,
    ) -> Flag:
        """Build a multivariate flag; fallthrough defaults to an even split."""
        ft = fallthrough or Rollout.even(tuple(v.key for v in variations))
        return cls(
            key=key,
            kind=kind,
            variations=variations,
            default_variation=default,
            fallthrough=ft,
            **kwargs,
        )


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The total, never-raising result of evaluating one flag for one context."""

    flag_key: str
    value: Any
    variation_key: str | None
    variation_index: int | None
    reason: Reason
    flag_version: int = 0
    rule_id: str | None = None
    in_experiment: bool = False

    @property
    def is_default(self) -> bool:
        """True when no flag served the value (missing / error fallback)."""
        return self.reason in (Reason.FLAG_NOT_FOUND, Reason.ERROR)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for the API / SDK / exposure log."""
        return {
            "flag_key": self.flag_key,
            "value": self.value,
            "variation_key": self.variation_key,
            "variation_index": self.variation_index,
            "reason": self.reason.value,
            "flag_version": self.flag_version,
            "rule_id": self.rule_id,
            "in_experiment": self.in_experiment,
        }


@dataclass(frozen=True, slots=True)
class FlagSnapshot:
    """An immutable, versioned set of flags — the unit the evaluator reads.

    ``version`` is a monotone counter bumped on every change; the cache uses it
    to detect staleness and the SDK uses it to know whether to refetch. The
    snapshot is keyed by flag key for O(1) prerequisite resolution.
    """

    flags: dict[str, Flag]
    version: int = 0

    @classmethod
    def from_flags(cls, flags: tuple[Flag, ...], *, version: int = 0) -> FlagSnapshot:
        """Build a snapshot from a flag tuple (rejects duplicate keys)."""
        by_key: dict[str, Flag] = {}
        for flag in flags:
            if flag.key in by_key:
                raise FlagValidationError(f"duplicate flag key {flag.key!r} in snapshot")
            by_key[flag.key] = flag
        return cls(flags=by_key, version=version)

    def get(self, key: str) -> Flag | None:
        """Return the flag with ``key`` or ``None``."""
        return self.flags.get(key)

    def with_flag(self, flag: Flag) -> FlagSnapshot:
        """Return a new snapshot with ``flag`` upserted and version bumped."""
        new_flags = dict(self.flags)
        new_flags[flag.key] = flag
        return FlagSnapshot(flags=new_flags, version=self.version + 1)

    def without_flag(self, key: str) -> FlagSnapshot:
        """Return a new snapshot with ``key`` removed and version bumped."""
        new_flags = {k: v for k, v in self.flags.items() if k != key}
        return FlagSnapshot(flags=new_flags, version=self.version + 1)

    def keys(self) -> tuple[str, ...]:
        """All flag keys (sorted, for stable listing)."""
        return tuple(sorted(self.flags))


EMPTY_SNAPSHOT = FlagSnapshot(flags={}, version=0)


__all__ = [
    "EMPTY_SNAPSHOT",
    "Clause",
    "Evaluation",
    "Flag",
    "FlagKind",
    "FlagSnapshot",
    "Operator",
    "Prerequisite",
    "Reason",
    "Rollout",
    "Rule",
    "Target",
    "Variation",
    "WeightedVariation",
    "EvalContext",
]
