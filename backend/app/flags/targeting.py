"""Predicate evaluation — does a context match a clause / a whole rule?

The targeting engine is pure and total: a malformed comparison (a string ``gt``
a number, an unparseable semver) evaluates to *no match* rather than raising, so
a single bad attribute can never crash an evaluation. Regexes are compiled and
cached per pattern; an invalid pattern simply never matches.

Supported operators (see :class:`~app.flags.models.Operator`):

* equality / set: ``eq neq in not_in``
* string: ``contains not_contains starts_with ends_with matches``
* numeric: ``gt gte lt lte``
* semver: ``semver_gt/gte/lt/lte/eq``
* presence: ``exists not_exists``
* deterministic: ``percentage`` (bucket the unit into a [lo, hi) bp window)
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.flags.context import AttrValue, EvalContext
from app.flags.hashing import bucket_bp
from app.flags.models import Clause, Operator, Rule

# --------------------------------------------------------------------------- #
# semver parsing (major.minor.patch with optional -prerelease, leading 'v' ok)
# --------------------------------------------------------------------------- #

_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?(?:[-+].*)?$"
)


def _parse_semver(value: AttrValue) -> tuple[int, int, int] | None:
    """Parse ``value`` into a ``(major, minor, patch)`` tuple, or ``None``."""
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.match(value.strip())
    if match is None:
        return None
    patch = match.group("patch")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(patch) if patch is not None else 0,
    )


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str] | None:
    """Compile + cache a regex; ``None`` if the pattern is invalid."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _as_number(value: AttrValue) -> float | None:
    """Coerce ``value`` to a float for ordered comparison (bools excluded)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _scalars(values: tuple[AttrValue, ...]) -> list[AttrValue]:
    """Flatten any list-valued operands into a flat scalar list."""
    out: list[AttrValue] = []
    for v in values:
        if isinstance(v, list):
            out.extend(v)
        else:
            out.append(v)
    return out


def _eval_op(op: Operator, actual: AttrValue, values: tuple[AttrValue, ...], unit: str) -> bool:
    """Evaluate one operator (pre-negation). Total — returns ``False`` on type mismatch."""
    if op is Operator.EXISTS:
        return actual is not None
    if op is Operator.NOT_EXISTS:
        return actual is None

    targets = _scalars(values)

    if op in (Operator.EQ, Operator.IN):
        return _membership(actual, targets)
    if op in (Operator.NEQ, Operator.NOT_IN):
        return not _membership(actual, targets)

    if op in (Operator.CONTAINS, Operator.NOT_CONTAINS):
        hit = _contains(actual, targets)
        return hit if op is Operator.CONTAINS else not hit

    if op in (Operator.STARTS_WITH, Operator.ENDS_WITH):
        return _affix(op, actual, targets)

    if op is Operator.MATCHES:
        if not isinstance(actual, str):
            return False
        return any(
            isinstance(t, str) and (c := _compile(t)) is not None and c.search(actual) is not None
            for t in targets
        )

    if op in (Operator.GT, Operator.GTE, Operator.LT, Operator.LTE):
        return _numeric(op, actual, targets)

    if op in (
        Operator.SEMVER_GT,
        Operator.SEMVER_GTE,
        Operator.SEMVER_LT,
        Operator.SEMVER_LTE,
        Operator.SEMVER_EQ,
    ):
        return _semver(op, actual, targets)

    if op is Operator.PERCENTAGE:
        return _percentage(targets, unit)

    return False  # pragma: no cover - exhaustive above


def _membership(actual: AttrValue, targets: list[AttrValue]) -> bool:
    if isinstance(actual, list):
        return any(a in targets for a in actual)
    return actual in targets


def _contains(actual: AttrValue, targets: list[AttrValue]) -> bool:
    # list attribute: contains any target element; string attribute: substring.
    if isinstance(actual, list):
        return any(t in actual for t in targets)
    if isinstance(actual, str):
        return any(isinstance(t, str) and t in actual for t in targets)
    return False


def _affix(op: Operator, actual: AttrValue, targets: list[AttrValue]) -> bool:
    if not isinstance(actual, str):
        return False
    strs = [t for t in targets if isinstance(t, str)]
    if op is Operator.STARTS_WITH:
        return any(actual.startswith(t) for t in strs)
    return any(actual.endswith(t) for t in strs)


def _numeric(op: Operator, actual: AttrValue, targets: list[AttrValue]) -> bool:
    a = _as_number(actual)
    if a is None:
        return False
    for raw in targets:
        b = _as_number(raw)
        if b is None:
            continue
        if op is Operator.GT and a > b:
            return True
        if op is Operator.GTE and a >= b:
            return True
        if op is Operator.LT and a < b:
            return True
        if op is Operator.LTE and a <= b:
            return True
    return False


def _semver(op: Operator, actual: AttrValue, targets: list[AttrValue]) -> bool:
    a = _parse_semver(actual)
    if a is None:
        return False
    for raw in targets:
        b = _parse_semver(raw)
        if b is None:
            continue
        if op is Operator.SEMVER_EQ and a == b:
            return True
        if op is Operator.SEMVER_GT and a > b:
            return True
        if op is Operator.SEMVER_GTE and a >= b:
            return True
        if op is Operator.SEMVER_LT and a < b:
            return True
        if op is Operator.SEMVER_LTE and a <= b:
            return True
    return False


def _percentage(targets: list[AttrValue], unit: str) -> bool:
    """``[lo_bp, hi_bp]`` (optional 3rd salt) → unit bucket in ``[lo, hi)``."""
    if len(targets) < 2:
        return False
    lo, hi = _as_number(targets[0]), _as_number(targets[1])
    if lo is None or hi is None:
        return False
    salt = targets[2] if len(targets) > 2 and isinstance(targets[2], str) else "percentage"
    b = bucket_bp(unit, salt)
    return int(lo) <= b < int(hi)


def clause_matches(clause: Clause, context: EvalContext) -> bool:
    """Evaluate one :class:`Clause` against ``context`` (negation applied)."""
    actual = context.get(clause.attribute)
    result = _eval_op(clause.op, actual, clause.values, context.key)
    return (not result) if clause.negate else result


def rule_matches(rule: Rule, context: EvalContext) -> bool:
    """A rule matches when **every** clause matches (logical AND, empty → True)."""
    return all(clause_matches(c, context) for c in rule.clauses)


__all__ = ["clause_matches", "rule_matches"]
