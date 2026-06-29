"""SQL normalisation + fingerprinting — the shared identity primitive.

Two queries that differ only in *literals*, whitespace, casing, or parameter
placeholder style are the *same query shape*. Collapsing them to a stable
fingerprint is what lets the platform aggregate stats per shape (profiler),
detect a burst of the same parameterised query (N+1), and key a result cache by
shape-plus-arguments rather than by raw text.

``normalize_sql`` rewrites a statement into a canonical skeleton:

* lowercases keywords and identifiers (Postgres folds unquoted identifiers to
  lower-case, so this matches its own behaviour),
* replaces every literal (numbers, quoted strings, parameter markers) with a
  single ``?`` placeholder,
* collapses ``IN (?, ?, ?)`` lists of any arity to ``IN (?)`` so a 2-element and
  a 200-element IN share a shape,
* squeezes runs of whitespace to one space and trims.

``fingerprint`` hashes that skeleton (sha1, hex) — a deterministic, stable id.

This is a *lexical* normaliser, not a parser: it tokenises, it does not build a
tree. That is deliberate — it must never raise on a query it does not fully
understand; the worst case is two genuinely-different queries colliding, which
only ever loses an optimisation, never causes a wrong result (the result cache
additionally keys on the parameter hash, and the rewriter independently proves
equivalence). For *structure*, see :mod:`app.datascale.optimize.sqlshape`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# A single-quoted string literal, honouring SQL's doubled-quote escape ('' inside).
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
# A dollar-quoted string ($tag$ ... $tag$); tag may be empty.
_DOLLAR_QUOTED = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)
# A numeric literal (int / float / scientific), not part of an identifier.
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")
# Bound-parameter markers: $1 / $2 (asyncpg), :name (SQLAlchemy), ? (qmark).
_PARAM_MARKER = re.compile(r"(?<![\w])(?:\$\d+|:\w+|%\(\w+\)s|%s|\?)")
# Line + block comments.
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# A collapsed ``in ( ?, ?, ? )`` of any length.
_IN_LIST = re.compile(r"in\s*\(\s*\?(?:\s*,\s*\?)*\s*\)")
# Runs of whitespace.
_WHITESPACE = re.compile(r"\s+")
# A ``values (?, ?), (?, ?)`` multi-row tail (collapses to a single representative row).
_VALUES_ROWS = re.compile(
    r"values\s*(\(\s*\?(?:\s*,\s*\?)*\s*\))(\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))+"
)

_PLACEHOLDER = "?"


def _strip_comments(sql: str) -> str:
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    return sql


def normalize_sql(sql: str) -> str:
    """Return the canonical skeleton of ``sql`` (literals → ``?``, lowered, tidy).

    Idempotent: ``normalize_sql(normalize_sql(s)) == normalize_sql(s)``.
    """
    if not sql or not sql.strip():
        return ""
    s = _strip_comments(sql)
    # Order matters: dollar-quoted and single-quoted strings first (they may
    # contain digits/keywords we must not touch), then params, then numbers.
    s = _DOLLAR_QUOTED.sub(_PLACEHOLDER, s)
    s = _STRING_LITERAL.sub(_PLACEHOLDER, s)
    s = _PARAM_MARKER.sub(_PLACEHOLDER, s)
    s = _NUMBER_LITERAL.sub(_PLACEHOLDER, s)
    s = s.lower()
    # Collapse whitespace before the IN/VALUES regexes (they assume single spaces
    # are possible but tolerate runs via ``\s*``; collapsing keeps output stable).
    s = _WHITESPACE.sub(" ", s).strip()
    s = _IN_LIST.sub("in (?)", s)
    s = _VALUES_ROWS.sub(r"values \1", s)
    # Trim trailing semicolons + surrounding spaces for a stable skeleton.
    s = s.rstrip("; ").strip()
    return s


def fingerprint(sql: str) -> str:
    """Return a stable hex fingerprint of ``sql``'s normalised shape."""
    skeleton = normalize_sql(sql)
    return hashlib.sha1(skeleton.encode("utf-8")).hexdigest()  # noqa: S324 - id, not crypto


@dataclass(frozen=True, slots=True)
class QueryFingerprint:
    """A query's normalised skeleton paired with its fingerprint hash."""

    skeleton: str
    hexdigest: str

    @property
    def short(self) -> str:
        """First 12 hex chars — enough to disambiguate in logs/reports."""
        return self.hexdigest[:12]


def make_fingerprint(sql: str) -> QueryFingerprint:
    """Build a :class:`QueryFingerprint` from raw ``sql``."""
    skeleton = normalize_sql(sql)
    digest = hashlib.sha1(skeleton.encode("utf-8")).hexdigest()  # noqa: S324
    return QueryFingerprint(skeleton=skeleton, hexdigest=digest)


# --------------------------------------------------------------------------- #
# Cheap table extraction (lexical; the structured version lives in sqlshape)
# --------------------------------------------------------------------------- #

_TABLE_AFTER = re.compile(
    r"\b(?:from|join|into|update)\s+([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)",
    re.IGNORECASE,
)


def referenced_tables(sql: str) -> frozenset[str]:
    """Best-effort set of base tables a statement reads/writes (lower-cased).

    Lexical and conservative: it finds identifiers after ``FROM``/``JOIN``/
    ``INTO``/``UPDATE``. Aliases and subqueries are not resolved here — for that
    use :func:`app.datascale.optimize.sqlshape.parse_select`. The result cache
    uses this for dependency tracking, so over-collecting (a false table) only
    over-invalidates (safe); under-collecting would be unsafe, which is why the
    cache lets callers pass an explicit dependency set to override it.
    """
    tables = {m.group(1).lower() for m in _TABLE_AFTER.finditer(sql)}
    return frozenset(tables)


__all__ = [
    "QueryFingerprint",
    "fingerprint",
    "make_fingerprint",
    "normalize_sql",
    "referenced_tables",
]
