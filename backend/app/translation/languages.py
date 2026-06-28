"""Language registry: BCP-47 normalization, scripts, and RTL/text-direction.

The content-translation pipeline keys everything on a *normalized* language tag
so a target language stays a single canonical string across the cache, the
glossary, the persisted artifacts, and the API. Real BCP-47 is large; we carry a
curated registry of the languages a book adaptation realistically targets plus a
permissive normalizer that:

* lowercases the primary subtag and Title-cases a 4-letter script subtag,
* upper-cases a 2-letter region subtag,
* maps common aliases (``zh-cn`` → ``zh-Hans``, ``iw`` → ``he``, ``pt-br`` → ``pt-BR``),
* and resolves direction (LTR/RTL) from the *script*, not the language — because
  the same language can be written in either (e.g. Azerbaijani in Latin vs
  Arabic script).

Direction matters downstream: the read-along highlight layer and the narration
sync map must know whether words flow right-to-left so the karaoke paint and the
page-turn geometry are correct (§9.4). RTL handling lives in :mod:`.rtl`; this
module is the source of truth for *which* languages are RTL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import UnknownLanguageError


class TextDirection(StrEnum):
    """Writing direction of a script."""

    LTR = "ltr"
    RTL = "rtl"


#: Scripts that are written right-to-left. Direction is a property of the
#: *script* (ISO 15924), so a language in one of these scripts is RTL.
RTL_SCRIPTS: frozenset[str] = frozenset(
    {"Arab", "Hebr", "Syrc", "Thaa", "Nkoo", "Samr", "Mand", "Adlm"}
)


@dataclass(frozen=True, slots=True)
class Language:
    """A normalized language in the registry.

    Attributes:
        tag: Canonical BCP-47 tag (e.g. ``en``, ``pt-BR``, ``zh-Hans``).
        name: English display name.
        endonym: Native display name (what the language calls itself).
        script: ISO 15924 script subtag (e.g. ``Latn``, ``Arab``, ``Hans``).
        direction: Derived writing direction (LTR/RTL).
        sentence_terminators: Characters that end a sentence in this language —
            used by the segmenter when the language uses non-ASCII punctuation
            (e.g. Arabic ``؟``, Chinese ``。``).
    """

    tag: str
    name: str
    endonym: str
    script: str
    direction: TextDirection
    sentence_terminators: tuple[str, ...] = (".", "!", "?")

    @property
    def is_rtl(self) -> bool:
        return self.direction is TextDirection.RTL

    @property
    def primary_subtag(self) -> str:
        """The language part of the tag (``pt`` for ``pt-BR``)."""
        return self.tag.split("-", 1)[0]


def _direction_for_script(script: str) -> TextDirection:
    return TextDirection.RTL if script in RTL_SCRIPTS else TextDirection.LTR


def _lang(
    tag: str,
    name: str,
    endonym: str,
    script: str,
    terminators: tuple[str, ...] = (".", "!", "?"),
) -> Language:
    return Language(
        tag=tag,
        name=name,
        endonym=endonym,
        script=script,
        direction=_direction_for_script(script),
        sentence_terminators=terminators,
    )


#: The curated registry, keyed by canonical tag. Covers the major adaptation
#: targets plus the RTL set so direction is exercised.
_REGISTRY: dict[str, Language] = {
    lang.tag: lang
    for lang in (
        _lang("en", "English", "English", "Latn"),
        _lang("es", "Spanish", "Español", "Latn", (".", "!", "?", "¡", "¿")),
        _lang("fr", "French", "Français", "Latn"),
        _lang("de", "German", "Deutsch", "Latn"),
        _lang("it", "Italian", "Italiano", "Latn"),
        _lang("pt", "Portuguese", "Português", "Latn"),
        _lang("pt-BR", "Portuguese (Brazil)", "Português (Brasil)", "Latn"),
        _lang("nl", "Dutch", "Nederlands", "Latn"),
        _lang("pl", "Polish", "Polski", "Latn"),
        _lang("ru", "Russian", "Русский", "Cyrl"),
        _lang("uk", "Ukrainian", "Українська", "Cyrl"),
        _lang("ja", "Japanese", "日本語", "Jpan", ("。", "！", "？")),
        _lang("ko", "Korean", "한국어", "Kore", (".", "！", "？")),
        _lang("zh-Hans", "Chinese (Simplified)", "简体中文", "Hans", ("。", "！", "？")),
        _lang("zh-Hant", "Chinese (Traditional)", "繁體中文", "Hant", ("。", "！", "？")),
        _lang("hi", "Hindi", "हिन्दी", "Deva", ("।", "!", "?")),
        # RTL languages — direction derived from the Arabic/Hebrew scripts.
        _lang("ar", "Arabic", "العربية", "Arab", (".", "؟", "!")),
        _lang("he", "Hebrew", "עברית", "Hebr"),
        _lang("fa", "Persian", "فارسی", "Arab", (".", "؟", "!")),
        _lang("ur", "Urdu", "اردو", "Arab", ("۔", "؟", "!")),
        _lang("tr", "Turkish", "Türkçe", "Latn"),
        _lang("vi", "Vietnamese", "Tiếng Việt", "Latn"),
        _lang("id", "Indonesian", "Bahasa Indonesia", "Latn"),
        _lang("th", "Thai", "ไทย", "Thai"),
    )
}

#: Alias → canonical tag. Handles legacy ISO codes, region collapses, and the
#: ``zh-CN``/``zh-TW`` script shorthands the web sends.
_ALIASES: dict[str, str] = {
    "iw": "he",  # legacy Hebrew
    "ji": "yi",
    "in": "id",  # legacy Indonesian
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-hans-cn": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant-tw": "zh-Hant",
    "pt-pt": "pt",
    "pt-br": "pt-BR",
    "nb": "no",
    "nn": "no",
    "fil": "tl",
    "pes": "fa",  # Western Persian macrolanguage
    "fa-ir": "fa",
}


def _split_tag(raw: str) -> tuple[str, str | None, str | None]:
    """Split a raw tag into (primary, script, region) with no normalization."""
    parts = [p for p in raw.replace("_", "-").split("-") if p]
    if not parts:
        return ("", None, None)
    primary = parts[0]
    script: str | None = None
    region: str | None = None
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            script = part
        elif len(part) in (2, 3) and part.isalpha() or len(part) == 3 and part.isdigit():
            region = part
    return (primary, script, region)


def canonical_tag(raw: str) -> str:
    """Normalize an arbitrary BCP-47-ish tag to its canonical registry form.

    Casing rules: primary lowercased, script Title-cased, region upper-cased.
    Aliases are resolved both on the raw and the recomposed tag, and we fall
    back from a fully-qualified tag (``en-US``) to its primary (``en``) when the
    primary alone is registered.

    Raises:
        UnknownLanguageError: if the tag is empty or unparseable.
    """
    if not raw or not raw.strip():
        raise UnknownLanguageError("empty language tag")
    lowered = raw.strip().lower().replace("_", "-")
    if lowered in _ALIASES:
        lowered = _ALIASES[lowered].lower()
    primary, script, region = _split_tag(lowered)
    if not primary:
        raise UnknownLanguageError(f"unparseable language tag: {raw!r}")
    recomposed_parts = [primary]
    if script:
        recomposed_parts.append(script.title())
    if region:
        recomposed_parts.append(region.upper())
    recomposed = "-".join(recomposed_parts)
    # Resolve aliases that only appear in the recomposed (e.g. zh-Hant-TW).
    if recomposed.lower() in _ALIASES:
        return _ALIASES[recomposed.lower()]
    return recomposed


def get_language(raw: str) -> Language:
    """Resolve a tag to a registered :class:`Language` (with sensible fallbacks).

    Resolution order:
      1. exact canonical tag,
      2. primary subtag alone (``fr-CA`` → ``fr``),
      3. alias of the primary.

    Raises:
        UnknownLanguageError: when nothing in the registry matches.
    """
    canonical = canonical_tag(raw)
    if canonical in _REGISTRY:
        return _REGISTRY[canonical]
    primary = canonical.split("-", 1)[0]
    if primary in _REGISTRY:
        return _REGISTRY[primary]
    if primary in _ALIASES and _ALIASES[primary] in _REGISTRY:
        return _REGISTRY[_ALIASES[primary]]
    raise UnknownLanguageError(f"no registered language for tag {raw!r}")


def is_known(raw: str) -> bool:
    """True iff :func:`get_language` would resolve ``raw`` without raising."""
    try:
        get_language(raw)
    except UnknownLanguageError:
        return False
    return True


def is_rtl(raw: str) -> bool:
    """True iff the resolved language is written right-to-left."""
    return get_language(raw).is_rtl


def supported_languages() -> list[Language]:
    """All registered languages, sorted by English name (stable for the API)."""
    return sorted(_REGISTRY.values(), key=lambda lang: lang.name)


def same_language(a: str, b: str) -> bool:
    """True iff two tags resolve to the same registered language."""
    try:
        return get_language(a).tag == get_language(b).tag
    except UnknownLanguageError:
        return canonical_tag(a) == canonical_tag(b)


__all__ = [
    "RTL_SCRIPTS",
    "Language",
    "TextDirection",
    "canonical_tag",
    "get_language",
    "is_known",
    "is_rtl",
    "same_language",
    "supported_languages",
]
