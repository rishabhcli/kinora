"""A Rego-style policy DSL — a small declarative language + evaluator.

This is the OPA/Rego-shaped engine of the plane: a deployment writes *policy* in
a tiny declarative language instead of hand-coding ``if`` ladders, and the
evaluator runs it against the request document. The language is intentionally
minimal and total (no loops, no recursion, no user code) so evaluation always
terminates and every branch is unit-testable.

Grammar (one statement per line; ``#`` starts a comment)::

    package book.access            # optional namespace
    default allow = false          # the rule's value when no body fires

    allow if {                     # a rule: head `if` a conjunctive body
        input.action == "book:read"
        input.subject.id == input.resource.owner
    }

    deny if {                      # explicit deny rules short-circuit
        input.context.suspended == true
    }

A **rule** has a head (``allow`` / ``deny``), an optional set of brace-delimited
**bodies** (each a conjunction of **expressions**), and a default value. A rule
*fires* when any one of its bodies is fully satisfied (bodies are OR-ed, the
expressions within a body are AND-ed — exactly Rego's incremental-definition
semantics). ``allow`` firing ⇒ the policy permits; a ``deny`` firing ⇒ the policy
forbids and overrides ``allow``.

Expressions reference the request via the ``input`` root (``input.subject.id``,
``input.resource.tenant``, ``input.action``, ``input.context.mfa``) and compare
with ``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``in``. The right side is a
literal (string, number, bool, null) or another ``input.*`` reference.

**Partial evaluation** (:meth:`Policy.partial`) is the headline feature: given a
*partial* document (some attributes known, others unknown), it folds away every
expression it can decide and returns a residual :class:`PartialResult` — the
verdict if already determined, or the still-unknown expressions that gate it.
This is what powers "what-if" simulation and reverse-index pruning: ask "would
this be allowed for *any* resource owned by me?" and get back the residual
constraints on the unknown resource.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from app.platform.authz.engine import SyncEngine
from app.platform.authz.model import AuthorizationRequest, EngineResult

# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


class Ref:
    """A reference to a path in the ``input`` document (e.g. ``input.subject.id``)."""

    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Ref({self.path!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ref) and other.path == self.path

    def __hash__(self) -> int:
        return hash(("Ref", self.path))


@dataclass(frozen=True)
class Expr:
    """A single comparison expression: ``left <op> right``.

    ``left`` is always a :class:`Ref` into ``input``. ``right`` is either a
    literal Python value or another :class:`Ref`.
    """

    left: Ref
    op: str
    right: Any

    def render(self) -> str:
        right = self.right.path if isinstance(self.right, Ref) else repr(self.right)
        return f"{self.left.path} {self.op} {right}"


@dataclass(frozen=True)
class Rule:
    """One rule: a head (``allow``/``deny``) defined by OR-ed conjunctive bodies."""

    head: str  # "allow" | "deny"
    bodies: tuple[tuple[Expr, ...], ...]  # OR of AND-of-Expr


@dataclass(frozen=True)
class Policy:
    """A parsed policy module — a package name, defaults, and rules.

    ``default_allow`` is the verdict when no ``allow`` body fires (Rego's
    ``default allow``). A policy permits iff some ``allow`` body fires (or the
    default is allow) AND no ``deny`` body fires — deny always wins, matching the
    plane's deny-overrides posture.
    """

    package: str
    rules: tuple[Rule, ...]
    default_allow: bool = False

    def allow_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.head == "allow")

    def deny_rules(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.head == "deny")


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class PolicyParseError(ValueError):
    """Raised when policy source is malformed (with line context)."""


_OPS = ("==", "!=", "<=", ">=", "<", ">", " in ")


def parse_policy(source: str) -> Policy:
    """Parse DSL ``source`` into a :class:`Policy` (raises on malformed input)."""
    package = "default"
    default_allow = False
    rules: list[Rule] = []

    lines = source.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = _strip_comment(raw).strip()
        i += 1
        if not line:
            continue
        if line.startswith("package "):
            package = line[len("package ") :].strip()
            continue
        if line.startswith("default "):
            default_allow = _parse_default(line)
            continue
        head, opened = _rule_head(line)
        if head is None:
            raise PolicyParseError(f"line {i}: expected a rule, got {line!r}")
        # Collect the body (possibly across lines) until the closing brace.
        if opened:
            body_lines, i = _collect_body(lines, i)
        else:
            body_lines = []
        body = tuple(_parse_expr(b, i) for b in body_lines if b.strip())
        # An empty body means an unconditional rule (fires always).
        rules.append(Rule(head=head, bodies=(body,)))

    return Policy(
        package=package,
        rules=_merge_rules(rules),
        default_allow=default_allow,
    )


def _strip_comment(line: str) -> str:
    # A '#' inside a string literal is not supported (the DSL has no such need).
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def _parse_default(line: str) -> bool:
    # default allow = false / true
    rest = line[len("default ") :].strip()
    if not rest.startswith("allow"):
        raise PolicyParseError(f"only 'default allow = ...' is supported: {line!r}")
    _, _, value = rest.partition("=")
    return _literal(value.strip()) is True


def _rule_head(line: str) -> tuple[str | None, bool]:
    """Return ``(head, opened_brace)`` for a rule line, or ``(None, False)``."""
    for head in ("allow", "deny"):
        if line == head or line == f"{head} {{" or line.startswith(f"{head} if"):
            return head, line.endswith("{")
        if line == f"{head} if {{":
            return head, True
    return None, False


def _collect_body(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect body expression lines until the matching ``}`` (returns next idx)."""
    body: list[str] = []
    i = start
    n = len(lines)
    while i < n:
        line = _strip_comment(lines[i]).strip()
        i += 1
        if line == "}":
            return body, i
        if line:
            body.append(line)
    raise PolicyParseError("unterminated rule body (missing '}')")


def _parse_expr(text: str, line_no: int) -> Expr:
    for op in _OPS:
        idx = text.find(op)
        if idx >= 0:
            left = text[:idx].strip()
            right = text[idx + len(op) :].strip()
            if not left.startswith("input"):
                raise PolicyParseError(
                    f"line {line_no}: left side must be an input reference: {text!r}"
                )
            return Expr(left=Ref(left), op=op.strip(), right=_operand(right))
    raise PolicyParseError(f"line {line_no}: no comparison operator in {text!r}")


def _operand(token: str) -> Any:
    if token.startswith("input"):
        return Ref(token)
    return _literal(token)


def _literal(token: str) -> Any:
    token = token.strip()
    if token in ("true", "false"):
        return token == "true"
    if token in ("null", "none"):
        return None
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return token[1:-1]
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        return token  # bare identifier treated as a string literal


def _merge_rules(rules: list[Rule]) -> tuple[Rule, ...]:
    """Merge same-head rules so multiple ``allow if`` blocks OR together."""
    by_head: dict[str, list[tuple[Expr, ...]]] = {}
    order: list[str] = []
    for r in rules:
        if r.head not in by_head:
            by_head[r.head] = []
            order.append(r.head)
        by_head[r.head].extend(r.bodies)
    return tuple(Rule(head=h, bodies=tuple(by_head[h])) for h in order)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


class Truth(enum.Enum):
    """Three-valued truth for partial evaluation: TRUE / FALSE / UNKNOWN."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def _resolve(path: str, doc: dict[str, Any]) -> tuple[bool, Any]:
    """Resolve ``input.a.b`` against ``doc``; return ``(known, value)``.

    ``known`` is False when any segment of the path is absent from the (possibly
    partial) document — that is what makes an expression UNKNOWN.
    """
    parts = path.split(".")
    if parts and parts[0] == "input":
        parts = parts[1:]
    cur: Any = doc
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _eval_expr(expr: Expr, doc: dict[str, Any]) -> Truth:
    """Evaluate one expression to three-valued truth over a partial ``doc``."""
    lknown, lval = _resolve(expr.left.path, doc)
    if isinstance(expr.right, Ref):
        rknown, rval = _resolve(expr.right.path, doc)
    else:
        rknown, rval = True, expr.right
    if not lknown or not rknown:
        return Truth.UNKNOWN
    return Truth.TRUE if _apply(lval, expr.op, rval) else Truth.FALSE


def _apply(left: Any, op: str, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "in":
        try:
            return left in right
        except TypeError:
            return False
    if left is None or right is None:
        return False
    try:
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    except TypeError:
        return False
    raise ValueError(f"unknown operator {op!r}")


def _eval_body(body: tuple[Expr, ...], doc: dict[str, Any]) -> tuple[Truth, list[Expr]]:
    """Evaluate a conjunctive body; return ``(truth, residual_unknowns)``.

    A body is AND-of-expressions: FALSE if any expr is FALSE; TRUE if all are
    TRUE; otherwise UNKNOWN with the still-unknown expressions returned as the
    residual (the constraints that remain to be satisfied).
    """
    residual: list[Expr] = []
    for expr in body:
        t = _eval_expr(expr, doc)
        if t is Truth.FALSE:
            return Truth.FALSE, []
        if t is Truth.UNKNOWN:
            residual.append(expr)
    if residual:
        return Truth.UNKNOWN, residual
    return Truth.TRUE, []


@dataclass(frozen=True)
class PartialResult:
    """The residual of a partial evaluation.

    ``decided`` is True when the verdict is already determined regardless of the
    unknown attributes. When undecided, ``residual_allow`` holds the bodies (each
    a conjunction of still-unknown expressions) that *could* still grant, and
    ``residual_deny`` the bodies that could still deny — the constraints a caller
    can push down (e.g. into a SQL filter) to enumerate matching resources.
    """

    decided: bool
    allow: bool
    residual_allow: tuple[tuple[Expr, ...], ...] = ()
    residual_deny: tuple[tuple[Expr, ...], ...] = ()


def _request_to_doc(request: AuthorizationRequest) -> dict[str, Any]:
    """Project the request into the ``input`` document the DSL reads."""
    return {
        "action": request.action,
        "subject": {
            "id": request.subject.id,
            "type": request.subject.type,
            **request.subject.attributes,
        },
        "resource": {
            "id": request.resource.id,
            "type": request.resource.type,
            **request.resource.attributes,
        },
        "context": dict(request.context.attributes),
    }


def evaluate_policy(policy: Policy, request: AuthorizationRequest) -> tuple[bool, list[str]]:
    """Fully evaluate ``policy`` for ``request``; return ``(allow, reasons)``.

    Deny-overrides: if any ``deny`` body fires, the result is deny. Otherwise the
    result is allow iff an ``allow`` body fires or ``default_allow`` is set.
    """
    doc = _request_to_doc(request)
    reasons: list[str] = []

    for rule in policy.deny_rules():
        for body in rule.bodies:
            t, _ = _eval_body(body, doc)
            if t is Truth.TRUE:
                reasons.append(f"deny body fired: {_render_body(body)}")
                return False, reasons

    allow_fired = False
    for rule in policy.allow_rules():
        for body in rule.bodies:
            t, _ = _eval_body(body, doc)
            if t is Truth.TRUE:
                allow_fired = True
                reasons.append(f"allow body fired: {_render_body(body)}")
                break
        if allow_fired:
            break

    if allow_fired:
        return True, reasons
    if policy.default_allow:
        reasons.append("default allow")
        return True, reasons
    reasons.append("no allow body fired; default deny")
    return False, reasons


def partial_evaluate(policy: Policy, known: dict[str, Any]) -> PartialResult:
    """Partially evaluate ``policy`` against a *partial* ``input`` document.

    ``known`` is an ``input``-shaped dict with only the attributes that are known
    (e.g. ``{"subject": {"id": "u1"}, "action": "book:read"}``). Returns a
    :class:`PartialResult`: decided when the verdict is fixed regardless of the
    unknowns, else the residual allow/deny bodies (still-unknown conjunctions).
    """
    # Deny short-circuits: a fully-true deny body decides (deny); any
    # possibly-true deny body keeps the result undecided.
    residual_deny: list[tuple[Expr, ...]] = []
    for rule in policy.deny_rules():
        for body in rule.bodies:
            t, residual = _eval_body(body, known)
            if t is Truth.TRUE:
                return PartialResult(decided=True, allow=False)
            if t is Truth.UNKNOWN:
                residual_deny.append(tuple(residual))

    residual_allow: list[tuple[Expr, ...]] = []
    allow_fully = False
    for rule in policy.allow_rules():
        for body in rule.bodies:
            t, residual = _eval_body(body, known)
            if t is Truth.TRUE:
                allow_fully = True
            elif t is Truth.UNKNOWN:
                residual_allow.append(tuple(residual))

    if not residual_deny:
        # No deny can still fire — the allow side alone decides.
        if allow_fully:
            return PartialResult(decided=True, allow=True)
        if not residual_allow:
            # Nothing can grant; fall to default.
            return PartialResult(decided=True, allow=policy.default_allow)
    # Undecided: return the residual constraints for downstream pruning.
    return PartialResult(
        decided=False,
        allow=False,
        residual_allow=tuple(residual_allow),
        residual_deny=tuple(residual_deny),
    )


def _render_body(body: tuple[Expr, ...]) -> str:
    if not body:
        return "true"
    return " AND ".join(e.render() for e in body)


# --------------------------------------------------------------------------- #
# The DSL engine
# --------------------------------------------------------------------------- #


@dataclass
class PolicyEngine(SyncEngine):
    """Run a set of named policies as a plane engine.

    Every policy is evaluated; the engine emits DENY if *any* policy denies via a
    fired deny rule, ALLOW if any policy allows, else abstains (so the policy
    layer composes under the plane's deny-overrides like every other engine). A
    policy whose default is allow but which never positively fires for the action
    is treated as *abstaining* rather than allowing, so a permissive default in
    one policy module doesn't blanket-grant unrelated actions.
    """

    name: str = "policy"
    policies: tuple[Policy, ...] = field(default_factory=tuple)

    @classmethod
    def from_sources(cls, *sources: str, name: str = "policy") -> PolicyEngine:
        return cls(name=name, policies=tuple(parse_policy(s) for s in sources))

    def evaluate(self, request: AuthorizationRequest) -> EngineResult:
        doc = _request_to_doc(request)
        for policy in self.policies:
            for rule in policy.deny_rules():
                for body in rule.bodies:
                    t, _ = _eval_body(body, doc)
                    if t is Truth.TRUE:
                        return EngineResult.deny(
                            self.name,
                            f"{policy.package}: deny {_render_body(body)}",
                            rule=f"{policy.package}/deny",
                        )
        for policy in self.policies:
            for rule in policy.allow_rules():
                for body in rule.bodies:
                    t, _ = _eval_body(body, doc)
                    if t is Truth.TRUE:
                        return EngineResult.allow(
                            self.name,
                            f"{policy.package}: allow {_render_body(body)}",
                            rule=f"{policy.package}/allow",
                        )
        return EngineResult.abstain(self.name, "no policy rule fired")


__all__ = [
    "Expr",
    "PartialResult",
    "Policy",
    "PolicyEngine",
    "PolicyParseError",
    "Ref",
    "Rule",
    "Truth",
    "evaluate_policy",
    "parse_policy",
    "partial_evaluate",
]
