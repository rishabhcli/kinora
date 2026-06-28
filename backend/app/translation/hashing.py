"""Content-hash keys for translation artifacts (mirrors db/hashing.py, §8.7).

§8.7 makes a re-read free by keying a render on a content hash: identical inputs
→ identical hash → cache hit → zero cost. The translation layer applies the same
discipline to *text*: a translated segment is keyed to the hash of everything
that determines its output, so re-translating the same source into the same
language with the same glossary is a cache hit and costs nothing.

    translation_hash = sha256(source_text + source_lang + target_lang
                              + content_kind + glossary_version)

Like the shot hash, components are joined with a unit-separator (``\\x1f``) that
cannot appear in the inputs, removing boundary ambiguity. The function is pure
and deterministic. ``source_text`` is hashed verbatim (not the masked form) so a
markup change invalidates the cache — the markup is part of the deliverable.
"""

from __future__ import annotations

import hashlib

from .types import ContentKind

_SEP = "\x1f"


def source_content_hash(text: str) -> str:
    """A stable SHA-256 of a raw source string (independent of language/glossary).

    This is the *source* identity used to detect that the underlying book text
    changed (the artifact must be re-translated). It deliberately ignores target
    language and glossary, unlike :func:`translation_key`.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def translation_key(
    *,
    source_text: str,
    source_lang: str,
    target_lang: str,
    content_kind: ContentKind | str,
    glossary_version: int = 0,
) -> str:
    """Return the deterministic cache key for one translated segment.

    Args:
        source_text: The raw source string (verbatim, markup included).
        source_lang: Canonical source language tag.
        target_lang: Canonical target language tag.
        content_kind: Namespaces the key so a page string and a same-worded
            narration line do not collide.
        glossary_version: Bumping the active glossary invalidates dependent
            translations (a renamed proper noun must re-translate).
    """
    kind = content_kind.value if isinstance(content_kind, ContentKind) else str(content_kind)
    payload = _SEP.join(
        (
            source_text,
            source_lang,
            target_lang,
            kind,
            str(glossary_version),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def artifact_key(*, book_id: str, target_lang: str, content_kind: ContentKind | str) -> str:
    """Stable key for a whole-book translation *artifact* (one per kind+lang)."""
    kind = content_kind.value if isinstance(content_kind, ContentKind) else str(content_kind)
    payload = _SEP.join((book_id, target_lang, kind))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


__all__ = ["artifact_key", "source_content_hash", "translation_key"]
