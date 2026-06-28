"""Text analysis for the search engine: tokenize → normalize → stem → expand.

The analyzer is the shared text pipeline both backends run so that *index time*
and *query time* produce comparable terms. It is deliberately dependency-free
(no NLTK / no DB) so it runs in the in-memory backend, in tests, and inside the
Postgres backend's Python-side query parsing without any infrastructure.

Pipeline (kinora.md §8 — search complements the canon; the canon stays the
authoritative store, this is the lexical projection):

    raw text
      → fold (lowercase, strip accents, normalize punctuation)
      → tokenize (unicode-word split, keep alphanumerics)
      → drop stopwords (English closed-class words)
      → stem (a compact Porter-ish suffix stripper)
      → expand synonyms (a small, domain-tuned thesaurus)

The same :class:`Analyzer` instance is used to analyze documents and queries, so
"running" indexed as ``run`` matches a query for "ran" once both stem to ``run``
(via the irregular map) and "movie" matches "film" via the synonym table.

Typo tolerance is :func:`damerau_levenshtein` (bounded edit distance with
transposition), used by the in-memory backend's fuzzy term expansion and by the
service's "did you mean" suggestion.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

# --------------------------------------------------------------------------- #
# Stopwords
# --------------------------------------------------------------------------- #

#: A compact English stopword list — closed-class words that carry no lexical
#: signal for ranking. Kept small on purpose: an over-aggressive list hurts
#: phrase recall ("to be or not to be" must still be findable).
_STOPWORDS_RAW = (
    "a an and are as at be but by for if in into is it no not of on or such "
    "that the their then there these they this to was will with from your you "
    "we our us he she his her its them they i me my mine ours yours theirs"
)
STOPWORDS: frozenset[str] = frozenset(_STOPWORDS_RAW.split(" "))

# --------------------------------------------------------------------------- #
# Synonyms — a small, domain-tuned thesaurus (book / film vocabulary)
# --------------------------------------------------------------------------- #

#: Bidirectional synonym groups. Each token in a group expands to the canonical
#: head term so a query for any member retrieves documents using any other. The
#: groups lean into Kinora's domain (book ⇄ film vocabulary) on top of a few
#: generic equivalences.
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("film", "movie", "video", "clip", "footage"),
    ("book", "novel", "story", "tale"),
    ("character", "person", "protagonist", "hero", "heroine"),
    ("location", "place", "setting", "scene", "locale"),
    ("palette", "colour", "color", "colors", "colours"),
    ("fast", "quick", "rapid", "speedy"),
    ("slow", "sluggish", "languid", "leisurely"),
    ("big", "large", "huge", "giant"),
    ("small", "tiny", "little", "miniature"),
    ("happy", "joyful", "cheerful", "glad"),
    ("sad", "unhappy", "sorrowful", "mournful"),
)


def _build_synonym_map(groups: Iterable[Sequence[str]]) -> dict[str, str]:
    """Map every member of a synonym group to the group's canonical head term."""
    out: dict[str, str] = {}
    for group in groups:
        if not group:
            continue
        head = group[0]
        for member in group:
            out[member] = head
    return out


# --------------------------------------------------------------------------- #
# Stemmer — a compact Porter-ish suffix stripper
# --------------------------------------------------------------------------- #

#: Irregular forms that suffix stripping can't reach; mapped directly to a stem.
_IRREGULAR: Mapping[str, str] = {
    "ran": "run",
    "running": "run",
    "runs": "run",
    "went": "go",
    "gone": "go",
    "going": "go",
    "goes": "go",
    "better": "good",
    "best": "good",
    "worse": "bad",
    "worst": "bad",
    "children": "child",
    "men": "man",
    "women": "woman",
    "feet": "foot",
    "teeth": "tooth",
    "mice": "mouse",
    "people": "person",
}

# Order matters: longer / more specific suffixes are tried first.
_STEP1_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("alize", "al"),
    ("icate", "ic"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ness", ""),
    (" ent", "ent"),
)


def _strip_plural(word: str) -> str:
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("ies"):
        return word[:-3] + "i" if len(word) > 4 else word[:-1]
    if word.endswith("ss"):
        return word
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def _strip_ed_ing(word: str) -> str:
    for suffix in ("ingly", "edly", "ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[: -len(suffix)]
            # Restore an elided 'e' (hoping → hope) for short stems.
            if stem.endswith(("at", "bl", "iz")):
                return stem + "e"
            # Collapse a doubled final consonant (hopping → hop).
            if len(stem) >= 2 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
                return stem[:-1]
            return stem
    return word


def stem(word: str) -> str:
    """Reduce a token to a crude stem (Porter-ish, dependency-free).

    Not linguistically perfect — it only needs to be *consistent* between index
    and query time so morphological variants conflate to one term. The irregular
    map covers the high-frequency exceptions suffix stripping can't reach.
    """
    if not word:
        return word
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    w = word
    w = _strip_plural(w)
    w = _strip_ed_ing(w)
    for suffix, repl in _STEP1_SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 2:
            w = w[: -len(suffix)] + repl
            break
    # Trailing 'y' → 'i' so "happy"/"happily" share a stem (but keep 1-2 char).
    if len(w) > 2 and w.endswith("y"):
        w = w[:-1] + "i"
    # Drop a final 'e' on longer stems (advise → advis) for conflation.
    if len(w) > 4 and w.endswith("e") and not w.endswith(("le", "ee")):
        w = w[:-1]
    return w


# --------------------------------------------------------------------------- #
# Tokenization & folding
# --------------------------------------------------------------------------- #

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
_APOS = re.compile(r"['’]")


def fold(text: str) -> str:
    """Lowercase + strip diacritics so "café" matches "cafe"."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def tokenize(text: str) -> list[str]:
    """Split folded text into word tokens (alphanumerics; apostrophes removed)."""
    folded = fold(text)
    tokens: list[str] = []
    for match in _WORD_RE.finditer(folded):
        tok = _APOS.sub("", match.group(0))
        if tok:
            tokens.append(tok)
    return tokens


# --------------------------------------------------------------------------- #
# Edit distance (typo tolerance)
# --------------------------------------------------------------------------- #


def damerau_levenshtein(a: str, b: str, *, max_distance: int | None = None) -> int:
    """Optimal-string-alignment distance (Damerau-Levenshtein with transposition).

    Counts insert / delete / substitute / *adjacent transpose* edits. When
    ``max_distance`` is given the computation early-exits with ``max_distance+1``
    once every cell in a row exceeds it, which keeps fuzzy term expansion cheap
    over a large vocabulary.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    if max_distance is not None and abs(la - lb) > max_distance:
        return max_distance + 1

    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            val = min(
                prev[j] + 1,  # deletion
                cur[j - 1] + 1,  # insertion
                prev[j - 1] + cost,  # substitution
            )
            if (
                i > 1
                and j > 1
                and ca == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                val = min(val, prev2[j - 2] + 1)  # transposition
            cur[j] = val
            if val < row_min:
                row_min = val
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        prev2 = prev
        prev = cur
    return prev[lb]


def within_distance(a: str, b: str, max_distance: int) -> bool:
    """True when ``a`` and ``b`` are within ``max_distance`` edits."""
    return damerau_levenshtein(a, b, max_distance=max_distance) <= max_distance


def auto_fuzziness(term: str) -> int:
    """The fuzziness budget for a term by length (Elasticsearch's heuristic).

    Short terms get 0 (too risky), medium 1, long 2 — so "cat" is matched
    exactly while "characrer" still finds "character".
    """
    n = len(term)
    if n <= 3:
        return 0
    if n <= 6:
        return 1
    return 2


# --------------------------------------------------------------------------- #
# The Analyzer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalyzedTerm:
    """One analyzed token: its surface form, position, and stemmed term."""

    surface: str
    position: int
    term: str


@dataclass
class Analyzer:
    """The shared analysis pipeline run at both index and query time.

    Configurable so callers can disable stemming/synonyms for exact-match fields
    (e.g. a faceted ``status`` keyword field should not be stemmed).
    """

    use_stopwords: bool = True
    use_stemming: bool = True
    use_synonyms: bool = True
    stopwords: frozenset[str] = STOPWORDS
    synonym_map: Mapping[str, str] = field(
        default_factory=lambda: _build_synonym_map(SYNONYM_GROUPS)
    )

    def normalize_token(self, token: str) -> str:
        """Apply synonym expansion then stemming to a single folded token."""
        tok = token
        if self.use_synonyms:
            tok = self.synonym_map.get(tok, tok)
        if self.use_stemming:
            tok = stem(tok)
        return tok

    def is_stop(self, token: str) -> bool:
        """True when ``token`` should be dropped as a stopword."""
        return self.use_stopwords and token in self.stopwords

    def analyze(self, text: str) -> list[str]:
        """Full pipeline → the list of index/query terms (stopwords removed)."""
        out: list[str] = []
        for tok in tokenize(text):
            if self.is_stop(tok):
                continue
            out.append(self.normalize_token(tok))
        return out

    def analyze_positions(self, text: str) -> list[AnalyzedTerm]:
        """Like :meth:`analyze` but keep token positions (for phrase matching).

        Stopwords keep their *position* (so phrase gaps are preserved) but are
        themselves emitted as their surface so an exact phrase like "to be" still
        positionally aligns; the matcher treats stopword positions specially.
        """
        out: list[AnalyzedTerm] = []
        for pos, tok in enumerate(tokenize(text)):
            term = tok if self.is_stop(tok) else self.normalize_token(tok)
            out.append(AnalyzedTerm(surface=tok, position=pos, term=term))
        return out

    def analyze_phrase(self, phrase: str) -> list[str]:
        """Analyze a quoted phrase keeping order *and* stopwords (positional match)."""
        return [
            tok if self.is_stop(tok) else self.normalize_token(tok)
            for tok in tokenize(phrase)
        ]


@lru_cache(maxsize=1)
def default_analyzer() -> Analyzer:
    """A process-wide default :class:`Analyzer` (stem + stopwords + synonyms)."""
    return Analyzer()


__all__ = [
    "STOPWORDS",
    "SYNONYM_GROUPS",
    "AnalyzedTerm",
    "Analyzer",
    "auto_fuzziness",
    "damerau_levenshtein",
    "default_analyzer",
    "fold",
    "stem",
    "tokenize",
    "within_distance",
]
