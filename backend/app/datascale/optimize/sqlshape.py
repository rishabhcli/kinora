"""A lightweight, dependency-free SQL *shape* parser for SELECT statements.

This is **not** a general SQL parser. It extracts just the structure the
optimization platform reasons about — base tables, output columns, equality/
range predicates, join conditions, GROUP BY keys, ORDER BY keys, and aggregate
functions — from the common shapes the app actually issues (single- and
multi-table SELECTs with ``WHERE``/``JOIN``/``GROUP BY``/``ORDER BY``). It
recognises the idioms a SQLAlchemy Core / ORM query compiles to.

It is deterministic and total over its supported subset: anything it cannot shape
confidently raises :class:`ParseError`, and every caller treats that as "decline
to optimise, run the original query." A *wrong* shape is never returned — the
matview rewriter and index advisor that consume this are correctness-critical.

Design: tokenise on a small lexer, then split the token stream on the top-level
clause keywords (respecting parenthesis depth so a subquery's ``where`` is not
mistaken for the outer one). Subqueries and set operations are detected and
rejected (``ParseError``) rather than mis-shaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.datascale.optimize.errors import ParseError

# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


class PredicateOp(StrEnum):
    """The predicate operators the shape parser distinguishes."""

    EQ = "="
    NEQ = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    IN = "in"
    LIKE = "like"
    IS_NULL = "is null"
    IS_NOT_NULL = "is not null"
    BETWEEN = "between"

    @property
    def is_equality(self) -> bool:
        """True for ``=`` / ``IN`` — the ops a btree index serves as a seek."""
        return self in (PredicateOp.EQ, PredicateOp.IN)

    @property
    def is_range(self) -> bool:
        """True for the ordered comparisons a btree serves as a range scan."""
        return self in (
            PredicateOp.LT,
            PredicateOp.LTE,
            PredicateOp.GT,
            PredicateOp.GTE,
            PredicateOp.BETWEEN,
        )


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A (table-or-alias, column) reference; ``table`` is ``None`` when unqualified."""

    table: str | None
    column: str

    def qualified(self) -> str:
        """``table.column`` when qualified, else just ``column``."""
        return f"{self.table}.{self.column}" if self.table else self.column

    def __str__(self) -> str:
        return self.qualified()


@dataclass(frozen=True, slots=True)
class Predicate:
    """One atomic ``WHERE`` comparison: ``column op <value>``."""

    column: ColumnRef
    op: PredicateOp
    #: True when the right-hand side is a bound parameter / literal (a seekable
    #: constant) rather than another column (a join-shaped predicate).
    literal_rhs: bool = True


@dataclass(frozen=True, slots=True)
class JoinCondition:
    """An equi-join ``left_col = right_col`` between two relations."""

    left: ColumnRef
    right: ColumnRef


@dataclass(frozen=True, slots=True)
class TableRef:
    """A base table with an optional alias."""

    name: str
    alias: str | None = None

    @property
    def key(self) -> str:
        """The name a column reference would use (alias if present, else name)."""
        return self.alias or self.name


@dataclass(slots=True)
class SelectShape:
    """The extracted structure of a SELECT statement."""

    tables: list[TableRef]
    columns: list[ColumnRef]
    star: bool
    predicates: list[Predicate]
    joins: list[JoinCondition]
    group_by: list[ColumnRef]
    order_by: list[ColumnRef]
    aggregates: list[str]
    distinct: bool
    limit: int | None

    # ---- convenience views used by the advisor / rewriter ---- #

    @property
    def table_names(self) -> frozenset[str]:
        """The set of base table names (not aliases)."""
        return frozenset(t.name for t in self.tables)

    def equality_columns(self) -> list[ColumnRef]:
        """Columns constrained by an equality/IN predicate (seek keys)."""
        return [p.column for p in self.predicates if p.op.is_equality and p.literal_rhs]

    def range_columns(self) -> list[ColumnRef]:
        """Columns constrained by a range predicate."""
        return [p.column for p in self.predicates if p.op.is_range and p.literal_rhs]

    @property
    def is_aggregate(self) -> bool:
        """True when the SELECT computes aggregates (with or without GROUP BY)."""
        return bool(self.aggregates) or bool(self.group_by)


# --------------------------------------------------------------------------- #
# Lexer
# --------------------------------------------------------------------------- #

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<str>'(?:[^']|'')*')
    | (?P<num>\b\d+(?:\.\d+)?\b)
    | (?P<param>\$\d+|:\w+|%\(\w+\)s|%s|\?)
    | (?P<op><=|>=|!=|<>|=|<|>)
    | (?P<punct>[(),.*])
    | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    """,
    re.VERBOSE,
)


def _tokenize(sql: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    n = len(sql)
    while pos < n:
        m = _TOKEN_RE.match(sql, pos)
        if m is None:
            # An unrecognised character (e.g. ``::`` cast, ``||``, ``[``) — we do
            # not shape such queries.
            raise ParseError(f"unexpected character {sql[pos]!r} at offset {pos}")
        pos = m.end()
        if m.lastgroup == "ws":
            continue
        text = m.group()
        tokens.append(text)
    return tokens


_KEYWORDS = {
    "select",
    "from",
    "where",
    "group",
    "order",
    "by",
    "having",
    "limit",
    "offset",
    "join",
    "inner",
    "left",
    "right",
    "full",
    "outer",
    "cross",
    "on",
    "as",
    "and",
    "or",
    "not",
    "in",
    "is",
    "null",
    "like",
    "ilike",
    "between",
    "distinct",
    "asc",
    "desc",
    "union",
    "intersect",
    "except",
    "with",
}

_AGG_FUNCS = {"count", "sum", "avg", "min", "max", "array_agg", "string_agg", "bool_and", "bool_or"}


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def _lower(tok: str) -> str:
    return tok.lower()


def _split_top_level(tokens: list[str], keyword: str) -> tuple[list[str], list[str]]:
    """Split ``tokens`` at the first top-level (depth-0) ``keyword``.

    Returns ``(before, after)`` where ``after`` excludes the keyword itself. If
    the keyword is absent, ``after`` is empty.
    """
    depth = 0
    for i, tok in enumerate(tokens):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and _lower(tok) == keyword:
            return tokens[:i], tokens[i + 1 :]
    return tokens, []


def _split_group_by(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split at a top-level ``group by`` (two-token keyword)."""
    depth = 0
    for i in range(len(tokens) - 1):
        tok = tokens[i]
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and _lower(tok) == "group" and _lower(tokens[i + 1]) == "by":
            return tokens[:i], tokens[i + 2 :]
    return tokens, []


def _split_order_by(tokens: list[str]) -> tuple[list[str], list[str]]:
    depth = 0
    for i in range(len(tokens) - 1):
        tok = tokens[i]
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and _lower(tok) == "order" and _lower(tokens[i + 1]) == "by":
            return tokens[:i], tokens[i + 2 :]
    return tokens, []


def _has_top_level_keyword(tokens: list[str], keyword: str) -> bool:
    depth = 0
    for tok in tokens:
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and _lower(tok) == keyword:
            return True
    return False


def _split_commas_top_level(tokens: list[str]) -> list[list[str]]:
    """Split a token list on top-level commas into item token-lists."""
    items: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for tok in tokens:
        if tok == "(":
            depth += 1
            current.append(tok)
        elif tok == ")":
            depth -= 1
            current.append(tok)
        elif tok == "," and depth == 0:
            items.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        items.append(current)
    return items


def _parse_column_ref(tokens: list[str]) -> ColumnRef | None:
    """Parse ``a.b`` / ``b`` into a :class:`ColumnRef`; ``None`` if not a plain ref."""
    if len(tokens) == 3 and tokens[1] == "." and _is_ident(tokens[0]) and _is_ident(tokens[2]):
        return ColumnRef(table=tokens[0].lower(), column=tokens[2].lower())
    if len(tokens) == 1 and _is_ident(tokens[0]):
        return ColumnRef(table=None, column=tokens[0].lower())
    return None


def _is_ident(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", tok)) and _lower(tok) not in _KEYWORDS


def _parse_select_list(tokens: list[str]) -> tuple[list[ColumnRef], bool, list[str], bool]:
    """Return (columns, star, aggregate_funcs, distinct)."""
    distinct = False
    if tokens and _lower(tokens[0]) == "distinct":
        distinct = True
        tokens = tokens[1:]
    columns: list[ColumnRef] = []
    aggregates: list[str] = []
    star = False
    for item in _split_commas_top_level(tokens):
        if not item:
            continue
        if len(item) == 1 and item[0] == "*":
            star = True
            continue
        head = _lower(item[0])
        if head in _AGG_FUNCS and len(item) >= 2 and item[1] == "(":
            aggregates.append(head)
            # Pull a column out of the aggregate args when it is a simple ref.
            inner = item[2:-1] if item[-1] == ")" else item[2:]
            ref = _parse_column_ref([t for t in inner if t.lower() != "distinct"])
            if ref is not None:
                columns.append(ref)
            continue
        # Drop a trailing ``as alias``.
        item = _strip_alias(item)
        ref = _parse_column_ref(item)
        if ref is not None:
            columns.append(ref)
    return columns, star, aggregates, distinct


def _strip_alias(item: list[str]) -> list[str]:
    """Remove a trailing ``as alias`` or bare ``alias`` from a select item."""
    if len(item) >= 2 and _lower(item[-2]) == "as":
        return item[:-2]
    # A bare alias: ``col alias`` (two idents, no dot between). Only strip when the
    # leading part is itself a valid ref (``a.b alias`` -> 4 tokens).
    if len(item) == 2 and _is_ident(item[0]) and _is_ident(item[1]):
        return item[:1]
    if len(item) == 4 and item[1] == "." and _is_ident(item[3]):
        return item[:3]
    return item


def _parse_from(tokens: list[str]) -> tuple[list[TableRef], list[JoinCondition]]:
    """Parse the FROM clause: comma-tables and JOIN ... ON ... conditions."""
    tables: list[TableRef] = []
    joins: list[JoinCondition] = []
    # Normalise: walk left to right, segmenting on join keywords.
    i = 0
    n = len(tokens)
    join_kw = {"join", "inner", "left", "right", "full", "outer", "cross"}
    segment: list[str] = []
    on_tokens: list[str] = []
    in_on = False

    join_words = {"inner", "left", "right", "full", "outer", "cross", "join"}

    def flush_table(seg: list[str]) -> None:
        seg = [t for t in seg if _lower(t) not in join_words]
        if not seg:
            return
        tref = _parse_table_ref(seg)
        if tref is not None:
            tables.append(tref)

    while i < n:
        tok = tokens[i]
        low = _lower(tok)
        if low == "on":
            in_on = True
            flush_table(segment)
            segment = []
            on_tokens = []
            i += 1
            continue
        if low in join_kw:
            if in_on:
                jc = _parse_join_condition(on_tokens)
                if jc is not None:
                    joins.append(jc)
                in_on = False
                on_tokens = []
            else:
                flush_table(segment)
            segment = []
            i += 1
            continue
        if tok == "," and not in_on:
            flush_table(segment)
            segment = []
            i += 1
            continue
        if in_on:
            on_tokens.append(tok)
        else:
            segment.append(tok)
        i += 1

    if in_on:
        jc = _parse_join_condition(on_tokens)
        if jc is not None:
            joins.append(jc)
    else:
        flush_table(segment)
    return tables, joins


def _parse_table_ref(tokens: list[str]) -> TableRef | None:
    """Parse ``name`` / ``name alias`` / ``name as alias`` / ``schema.name [alias]``."""
    if not tokens:
        return None
    # schema.table
    name: str
    rest: list[str]
    if len(tokens) >= 3 and tokens[1] == "." and _is_ident(tokens[0]) and _is_ident(tokens[2]):
        name = tokens[2].lower()
        rest = tokens[3:]
    elif _is_ident(tokens[0]):
        name = tokens[0].lower()
        rest = tokens[1:]
    else:
        return None
    alias: str | None = None
    if rest:
        if _lower(rest[0]) == "as" and len(rest) >= 2 and _is_ident(rest[1]):
            alias = rest[1].lower()
        elif _is_ident(rest[0]):
            alias = rest[0].lower()
    return TableRef(name=name, alias=alias)


def _parse_join_condition(tokens: list[str]) -> JoinCondition | None:
    """Parse ``a.x = b.y`` (only equi-joins between two column refs)."""
    # Find a top-level '='.
    if "=" not in tokens:
        return None
    idx = tokens.index("=")
    left = _parse_column_ref(tokens[:idx])
    right = _parse_column_ref(tokens[idx + 1 :])
    if left is None or right is None:
        return None
    if left.table is None or right.table is None:
        return None
    return JoinCondition(left=left, right=right)


_OP_TOKENS = {
    "=": PredicateOp.EQ,
    "!=": PredicateOp.NEQ,
    "<>": PredicateOp.NEQ,
    "<": PredicateOp.LT,
    "<=": PredicateOp.LTE,
    ">": PredicateOp.GT,
    ">=": PredicateOp.GTE,
}


def _parse_where(tokens: list[str]) -> tuple[list[Predicate], list[JoinCondition]]:
    """Parse a WHERE clause into predicates + any column=column join conditions.

    Only top-level ``AND``-separated atoms are extracted. The presence of a
    top-level ``OR`` makes per-column index reasoning unsound, so we shape the
    AND atoms we can and leave OR-joined ones out (they simply do not contribute
    seek columns — conservative, never wrong).
    """
    predicates: list[Predicate] = []
    joins: list[JoinCondition] = []
    for atom in _split_on_keyword(tokens, "and"):
        # A top-level OR inside an AND-atom makes any single-column seek reasoning
        # unsound (the row may match via the other disjunct), so we contribute no
        # predicate/join from it — conservative, never wrong.
        if _has_top_level_keyword(atom, "or"):
            continue
        pred = _parse_atom(atom)
        if isinstance(pred, Predicate):
            predicates.append(pred)
        elif isinstance(pred, JoinCondition):
            joins.append(pred)
    return predicates, joins


def _split_on_keyword(tokens: list[str], keyword: str) -> list[list[str]]:
    atoms: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for tok in tokens:
        if tok == "(":
            depth += 1
            current.append(tok)
        elif tok == ")":
            depth -= 1
            current.append(tok)
        elif depth == 0 and _lower(tok) == keyword:
            atoms.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        atoms.append(current)
    return atoms


def _parse_atom(tokens: list[str]) -> Predicate | JoinCondition | None:
    """Parse one WHERE atom into a Predicate or a column=column JoinCondition."""
    tokens = [t for t in tokens if t.strip()]
    if not tokens:
        return None
    # IS NULL / IS NOT NULL
    low = [t.lower() for t in tokens]
    if "is" in low:
        idx = low.index("is")
        col = _parse_column_ref(tokens[:idx])
        if col is None:
            return None
        if low[idx:] == ["is", "not", "null"]:
            return Predicate(col, PredicateOp.IS_NOT_NULL, literal_rhs=True)
        if low[idx:] == ["is", "null"]:
            return Predicate(col, PredicateOp.IS_NULL, literal_rhs=True)
        return None
    # BETWEEN
    if "between" in low:
        idx = low.index("between")
        col = _parse_column_ref(tokens[:idx])
        if col is None:
            return None
        return Predicate(col, PredicateOp.BETWEEN, literal_rhs=True)
    # IN ( ... )
    if "in" in low:
        idx = low.index("in")
        col = _parse_column_ref(tokens[:idx])
        if col is not None and idx + 1 < len(tokens) and tokens[idx + 1] == "(":
            return Predicate(col, PredicateOp.IN, literal_rhs=True)
    # LIKE / ILIKE
    for kw in ("like", "ilike"):
        if kw in low:
            idx = low.index(kw)
            col = _parse_column_ref(tokens[:idx])
            if col is not None:
                return Predicate(col, PredicateOp.LIKE, literal_rhs=True)
    # Comparison operators.
    for i, tok in enumerate(tokens):
        if tok in _OP_TOKENS:
            left = _parse_column_ref(tokens[:i])
            rhs = tokens[i + 1 :]
            if left is None:
                return None
            right_col = _parse_column_ref(rhs)
            op = _OP_TOKENS[tok]
            if right_col is not None and right_col.table is not None and left.table is not None:
                # column = column ⇒ a join condition (only for '=').
                if op == PredicateOp.EQ:
                    return JoinCondition(left=left, right=right_col)
                return Predicate(left, op, literal_rhs=False)
            return Predicate(left, op, literal_rhs=True)
    return None


def _parse_simple_refs(tokens: list[str]) -> list[ColumnRef]:
    """Parse a comma list of column refs (GROUP BY / ORDER BY), dropping ASC/DESC."""
    refs: list[ColumnRef] = []
    for item in _split_commas_top_level(tokens):
        item = [t for t in item if _lower(t) not in {"asc", "desc", "nulls", "first", "last"}]
        ref = _parse_column_ref(item)
        if ref is not None:
            refs.append(ref)
    return refs


def parse_select(sql: str) -> SelectShape:
    """Shape a single SELECT statement.

    Raises :class:`ParseError` for anything outside the supported subset: non
    SELECT statements, CTEs (``WITH``), set operations (``UNION``/…), subqueries in
    FROM, or syntax the lexer cannot tokenise.
    """
    if sql is None or not sql.strip():
        raise ParseError("empty statement")
    tokens = _tokenize(sql.strip().rstrip(";"))
    if not tokens or _lower(tokens[0]) != "select":
        raise ParseError("not a SELECT statement")
    if _lower(tokens[0]) == "with":
        raise ParseError("CTEs are not shaped")
    for setop in ("union", "intersect", "except"):
        if _has_top_level_keyword(tokens, setop):
            raise ParseError(f"set operation {setop!r} is not shaped")

    body = tokens[1:]  # drop SELECT
    select_tokens, after_select = _split_top_level(body, "from")
    if not after_select:
        raise ParseError("SELECT without FROM is not shaped")

    # Strip LIMIT/OFFSET from the tail first (they follow ORDER BY).
    rest = after_select
    limit_val: int | None = None
    rest, after_limit = _split_top_level(rest, "limit")
    if after_limit:
        for t in after_limit:
            if re.fullmatch(r"\d+", t):
                limit_val = int(t)
                break
    rest, _after_offset = _split_top_level(rest, "offset")

    rest, after_having = _split_top_level(rest, "having")
    rest, order_tokens = _split_order_by(rest)
    rest, group_tokens = _split_group_by(rest)
    from_tokens, where_tokens = _split_top_level(rest, "where")

    if any(t == "(" for t in from_tokens) and _looks_like_subquery(from_tokens):
        raise ParseError("subquery in FROM is not shaped")

    columns, star, aggregates, distinct = _parse_select_list(select_tokens)
    tables, from_joins = _parse_from(from_tokens)
    if not tables:
        raise ParseError("could not resolve any base table in FROM")

    predicates: list[Predicate] = []
    where_joins: list[JoinCondition] = []
    if where_tokens:
        predicates, where_joins = _parse_where(where_tokens)
    # ``having`` aggregate functions count toward is_aggregate.
    if after_having:
        for tok in after_having:
            if _lower(tok) in _AGG_FUNCS:
                aggregates.append(_lower(tok))

    group_by = _parse_simple_refs(group_tokens) if group_tokens else []
    order_by = _parse_simple_refs(order_tokens) if order_tokens else []
    joins = from_joins + where_joins

    return SelectShape(
        tables=tables,
        columns=columns,
        star=star,
        predicates=predicates,
        joins=joins,
        group_by=group_by,
        order_by=order_by,
        aggregates=aggregates,
        distinct=distinct,
        limit=limit_val,
    )


def _looks_like_subquery(from_tokens: list[str]) -> bool:
    """Heuristic: a '(' immediately followed by 'select' is a derived table."""
    for i, tok in enumerate(from_tokens):
        if tok == "(" and i + 1 < len(from_tokens) and _lower(from_tokens[i + 1]) == "select":
            return True
    return False


def try_parse_select(sql: str) -> SelectShape | None:
    """Like :func:`parse_select` but returns ``None`` instead of raising."""
    try:
        return parse_select(sql)
    except ParseError:
        return None


__all__ = [
    "ColumnRef",
    "JoinCondition",
    "Predicate",
    "PredicateOp",
    "SelectShape",
    "TableRef",
    "parse_select",
    "try_parse_select",
]
