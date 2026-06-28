"""Query parsing: free-text query string → a structured :class:`ParsedQuery`.

The query language is a small, search-engine-conventional grammar so a power
user (or the desktop search bar) can express intent precisely while a plain
phrase still "just works":

* **Phrases** — ``"snow queen"`` matches the words adjacent and in order.
* **Boolean** — ``frost AND castle``, ``frost OR ice``, ``frost NOT summer``.
  ``+term`` forces a clause (must), ``-term`` excludes it (must-not).
* **Field filters** — ``kind:character``, ``book_id:abc123`` scope to a field.
* **Facet selectors** — same ``field:value`` syntax; the parser collects them as
  ``filters`` and the index turns equality filters into post-filters / facets.
* **Range filters** — ``page:>=10``, ``score:<0.5``, ``page:[3 TO 9]``.

The output is *backend-agnostic*: both the in-memory and Postgres backends
consume the same :class:`ParsedQuery`. Tokenization of the free-text clauses is
deferred to the :class:`~app.search.analyzer.Analyzer` at search time, so the
parser only structures the query — it does not stem.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field


class Occur(enum.StrEnum):
    """Boolean occurrence of a clause (Lucene-style must / should / must-not)."""

    MUST = "must"
    SHOULD = "should"
    MUST_NOT = "must_not"


class RangeOp(enum.StrEnum):
    """A comparison operator for a numeric range filter."""

    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "="


@dataclass(frozen=True)
class TermClause:
    """A single free-text term clause with its boolean occurrence + field scope."""

    text: str
    occur: Occur = Occur.SHOULD
    field: str | None = None  # restrict matching to one text field
    fuzzy: bool = True  # allow typo-tolerant matching for this term


@dataclass(frozen=True)
class PhraseClause:
    """A quoted phrase clause (ordered, adjacent terms)."""

    text: str
    occur: Occur = Occur.SHOULD
    field: str | None = None
    slop: int = 0  # max intervening tokens allowed between phrase terms


@dataclass(frozen=True)
class FieldFilter:
    """An equality filter / facet selector on a keyword field (``kind:beat``)."""

    field: str
    value: str
    negate: bool = False


@dataclass(frozen=True)
class RangeFilter:
    """A numeric range filter (``page:>=10`` or ``page:[3 TO 9]``)."""

    field: str
    op: RangeOp | None = None  # None => use lo/hi bounds (a TO range)
    value: float | None = None
    lo: float | None = None
    hi: float | None = None


@dataclass
class ParsedQuery:
    """The structured result of parsing a raw query string.

    ``terms`` / ``phrases`` are the free-text clauses (with boolean occurrence);
    ``filters`` are keyword equality selectors (also the facet drill-downs);
    ``ranges`` are numeric constraints. ``raw`` is the original string (kept for
    highlighting + suggestions). ``free_text`` is the concatenation of all
    SHOULD/MUST free-text clauses — the string the embedder embeds for the dense
    arm of hybrid search.
    """

    raw: str
    terms: list[TermClause] = field(default_factory=list)
    phrases: list[PhraseClause] = field(default_factory=list)
    filters: list[FieldFilter] = field(default_factory=list)
    ranges: list[RangeFilter] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing matchable was parsed (only whitespace)."""
        return not (self.terms or self.phrases or self.filters or self.ranges)

    @property
    def has_text(self) -> bool:
        """True when there is at least one free-text clause to score on."""
        return bool(self.terms or self.phrases)

    @property
    def free_text(self) -> str:
        """The positive free-text of the query (for the dense / embedding arm)."""
        parts: list[str] = []
        for t in self.terms:
            if t.occur is not Occur.MUST_NOT:
                parts.append(t.text)
        for p in self.phrases:
            if p.occur is not Occur.MUST_NOT:
                parts.append(p.text)
        return " ".join(parts)

    @property
    def positive_terms(self) -> list[str]:
        """The MUST/SHOULD term texts (used by highlighting + suggestions)."""
        out = [t.text for t in self.terms if t.occur is not Occur.MUST_NOT]
        for p in self.phrases:
            if p.occur is not Occur.MUST_NOT:
                out.append(p.text)
        return out


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #

# A token is one of: a quoted phrase, a +/-prefixed word, an operator, or a bare
# word — optionally field-scoped (``field:...``). Quotes may follow the colon.
_TOKEN_RE = re.compile(
    r"""
    (?P<neg>[-+])?                      # optional +/- occurrence prefix
    (?:(?P<field>[A-Za-z_][\w]*):)?     # optional field scope
    (?:
        "(?P<phrase>[^"]*)"             # a quoted phrase
      | \[(?P<rlo>[^\]\s]+)\s+TO\s+(?P<rhi>[^\]\s]+)\]   # a [lo TO hi] range
      | (?P<rop><=|>=|<|>)?(?P<word>[^\s"]+)             # word w/ optional range op
    )
    """,
    re.VERBOSE,
)

_OPERATORS = {"AND", "OR", "NOT"}
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

#: Field names the parser treats as keyword *filters* (equality / facet) rather
#: than free-text search. Everything else is matched as text. This keeps a stray
#: ``foo:bar`` from being silently dropped — unknown fields fall back to text.
KEYWORD_FIELDS: frozenset[str] = frozenset(
    {"kind", "book_id", "scene_id", "beat_id", "entity_type", "status", "render_mode", "lang"}
)
#: Numeric fields eligible for range filters.
NUMERIC_FIELDS: frozenset[str] = frozenset(
    {"page", "page_number", "scene_index", "beat_index", "version", "score", "duration_s"}
)
#: Text fields a ``field:value`` scope may restrict to.
TEXT_FIELDS: frozenset[str] = frozenset({"title", "author", "name", "body", "text", "summary"})


def _as_number(token: str) -> float | None:
    return float(token) if _NUM_RE.match(token) else None


def parse_query(raw: str) -> ParsedQuery:
    """Parse a raw query string into a structured :class:`ParsedQuery`.

    Robust by design: any clause that doesn't fit the grammar degrades to a
    plain SHOULD term, so a user never gets an error — they get the closest
    reasonable interpretation. Boolean operators bind the *next* clause:
    ``a AND b`` makes ``b`` MUST; ``a NOT b`` makes ``b`` MUST_NOT; ``a OR b``
    keeps both SHOULD.
    """
    pq = ParsedQuery(raw=raw)
    pending_occur: Occur | None = None

    for match in _TOKEN_RE.finditer(raw):
        word = match.group("word")
        phrase = match.group("phrase")
        field_name = match.group("field")
        neg = match.group("neg")
        rop = match.group("rop")
        rlo = match.group("rlo")
        rhi = match.group("rhi")

        # Bare boolean operator -> set occurrence for the next clause.
        if word and field_name is None and neg is None and word.upper() in _OPERATORS:
            op = word.upper()
            pending_occur = (
                Occur.MUST if op == "AND" else Occur.MUST_NOT if op == "NOT" else Occur.SHOULD
            )
            continue

        occur = _resolve_occur(neg, pending_occur)
        pending_occur = None

        # A [lo TO hi] range on a numeric field.
        if rlo is not None and rhi is not None and field_name in NUMERIC_FIELDS:
            lo, hi = _as_number(rlo), _as_number(rhi)
            if lo is not None or hi is not None:
                pq.ranges.append(RangeFilter(field=field_name, lo=lo, hi=hi))
                continue

        # A quoted phrase.
        if phrase is not None:
            text = phrase.strip()
            if text:
                tfield = field_name if field_name in TEXT_FIELDS else None
                pq.phrases.append(PhraseClause(text=text, occur=occur, field=tfield))
            continue

        if word is None:
            continue
        word = word.strip()
        if not word:
            continue

        # A field-scoped clause.
        if field_name is not None:
            if field_name in KEYWORD_FIELDS:
                pq.filters.append(
                    FieldFilter(field=field_name, value=word, negate=occur is Occur.MUST_NOT)
                )
                continue
            if field_name in NUMERIC_FIELDS:
                num = _as_number(word)
                if num is not None:
                    op = _range_op(rop)
                    pq.ranges.append(RangeFilter(field=field_name, op=op, value=num))
                    continue
            if field_name in TEXT_FIELDS:
                pq.terms.append(TermClause(text=word, occur=occur, field=field_name))
                continue
            # Unknown field -> fall back to a free-text term carrying the colon
            # form so the user's literal intent isn't silently lost.
            pq.terms.append(TermClause(text=f"{field_name}:{word}", occur=occur))
            continue

        # Plain free-text term. A leading range op without a field is literal.
        text = (rop or "") + word if rop else word
        pq.terms.append(TermClause(text=text, occur=occur, fuzzy=len(text) > 3))

    return pq


def _resolve_occur(neg: str | None, pending: Occur | None) -> Occur:
    if neg == "-":
        return Occur.MUST_NOT
    if neg == "+":
        return Occur.MUST
    return pending or Occur.SHOULD


def _range_op(rop: str | None) -> RangeOp:
    if rop == ">=":
        return RangeOp.GTE
    if rop == "<=":
        return RangeOp.LTE
    if rop == ">":
        return RangeOp.GT
    if rop == "<":
        return RangeOp.LT
    return RangeOp.EQ


def range_matches(rf: RangeFilter, value: float | int | None) -> bool:
    """Evaluate a :class:`RangeFilter` against a numeric field value."""
    if value is None:
        return False
    v = float(value)
    if rf.lo is not None and v < rf.lo:
        return False
    if rf.hi is not None and v > rf.hi:
        return False
    if rf.op is not None and rf.value is not None:
        if rf.op is RangeOp.GT:
            return v > rf.value
        if rf.op is RangeOp.GTE:
            return v >= rf.value
        if rf.op is RangeOp.LT:
            return v < rf.value
        if rf.op is RangeOp.LTE:
            return v <= rf.value
        if rf.op is RangeOp.EQ:
            return v == rf.value
    return True


__all__ = [
    "KEYWORD_FIELDS",
    "NUMERIC_FIELDS",
    "TEXT_FIELDS",
    "FieldFilter",
    "Occur",
    "ParsedQuery",
    "PhraseClause",
    "RangeFilter",
    "RangeOp",
    "TermClause",
    "parse_query",
    "range_matches",
]
