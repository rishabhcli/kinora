"""Object-store key collection + book-id rewriting for portability.

A book's binary assets live in object storage under keys that embed the
``book_id`` as a path segment (see :class:`app.storage.object_store.Keys`):

* ``pdfs/{book}.pdf`` · ``epubs/{book}.epub`` · ``covers/{book}``
* ``pages/{book}/{n}.png`` · ``keyframes/{book}/{beat}.png``
* ``clips/{book}/{shot}.mp4`` · ``lastframes/{book}/{shot}.png``
* ``audio/{book}/{shot}.wav`` · ``refs/{book}/{entity}/{name}``
* ``canon/{book}/{name}``

Two jobs live here, both pure (no I/O):

1. :func:`collect_book_keys` — given a book's rows, produce the de-duplicated set
   of object keys to pull into an archive. It unions the *deterministic* per-book
   keys (source doc, cover, page images) with every key *referenced in the row
   data* (clip/last-frame/audio keys in ``shots.output``/``narration``,
   reference-image keys in ``entities.appearance``, ``shot_cache`` keys, etc.).
   Referencing the rows means we never miss a key the pipeline wrote under a
   non-obvious name and never copy keys for assets a book does not have.

2. :func:`rewrite_key_book_id` — rewrite the ``{book}`` segment of a key from the
   old book id to the new one on import, so a restored asset lands at the key the
   re-homed rows point to.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: The key prefixes whose **second** path segment is the book id
#: (``prefix/{book}/...``).
_BOOK_DIR_PREFIXES = ("pages/", "keyframes/", "clips/", "lastframes/", "audio/", "refs/", "canon/")
#: The key prefixes whose **filename stem** is the book id (``prefix/{book}.ext``
#: or ``prefix/{book}``).
_BOOK_FILE_PREFIXES = ("pdfs/", "epubs/", "covers/")


def deterministic_book_keys(book_id: str, *, page_count: int | None) -> list[str]:
    """The always-present per-book keys derivable from the book id alone."""
    from app.storage.object_store import keys as obj_keys

    out = [obj_keys.pdf(book_id), obj_keys.epub(book_id), obj_keys.cover(book_id)]
    if page_count:
        out.extend(obj_keys.page_image(book_id, n) for n in range(1, page_count + 1))
    return out


def _extend_with(keys_out: set[str], value: Any) -> None:
    """Add a key (or a list/dict of keys) to the accumulator if it is a string key."""
    if isinstance(value, str) and value:
        keys_out.add(value)


def keys_from_shot(row: Mapping[str, Any]) -> set[str]:
    """Every object key referenced by one ``shots`` row."""
    out: set[str] = set()
    output = row.get("output") or {}
    if isinstance(output, Mapping):
        _extend_with(out, output.get("clip_key"))
        _extend_with(out, output.get("last_frame_key"))
    narration = row.get("narration") or {}
    if isinstance(narration, Mapping):
        _extend_with(out, narration.get("audio_key"))
    return out


def keys_from_entity(row: Mapping[str, Any]) -> set[str]:
    """Every object key referenced by one ``entities`` row (refs + voice ref)."""
    out: set[str] = set()
    appearance = row.get("appearance") or {}
    if isinstance(appearance, Mapping):
        for item in appearance.get("reference_images") or []:
            if isinstance(item, Mapping):
                _extend_with(out, item.get("key") or item.get("oss_key"))
        for key in appearance.get("reference_image_keys") or []:
            _extend_with(out, key)
    voice = row.get("voice") or {}
    if isinstance(voice, Mapping):
        _extend_with(out, voice.get("reference_audio_key"))
    return out


def keys_from_page(row: Mapping[str, Any]) -> set[str]:
    """The image key referenced by one ``pages`` row."""
    out: set[str] = set()
    _extend_with(out, row.get("image_key"))
    return out


def keys_from_book(row: Mapping[str, Any]) -> set[str]:
    """The source-pdf + cover keys referenced by the ``books`` row itself."""
    out: set[str] = set()
    _extend_with(out, row.get("source_pdf_key"))
    _extend_with(out, row.get("cover_key"))
    return out


def keys_from_shot_cache(row: Mapping[str, Any]) -> set[str]:
    """Clip + last-frame keys referenced by one ``shot_cache`` row."""
    out: set[str] = set()
    _extend_with(out, row.get("clip_key"))
    _extend_with(out, row.get("last_frame_key"))
    return out


def collect_book_keys(
    book_id: str,
    *,
    page_count: int | None,
    book_row: Mapping[str, Any] | None,
    pages: Iterable[Mapping[str, Any]],
    entities: Iterable[Mapping[str, Any]],
    shots: Iterable[Mapping[str, Any]],
    shot_cache: Iterable[Mapping[str, Any]],
) -> set[str]:
    """Union of deterministic + referenced object keys for one book."""
    out: set[str] = set(deterministic_book_keys(book_id, page_count=page_count))
    if book_row is not None:
        out |= keys_from_book(book_row)
    for p in pages:
        out |= keys_from_page(p)
    for e in entities:
        out |= keys_from_entity(e)
    for s in shots:
        out |= keys_from_shot(s)
    for c in shot_cache:
        out |= keys_from_shot_cache(c)
    return out


def rewrite_key_book_id(key: str, old_book_id: str, new_book_id: str) -> str:
    """Rewrite the book-id segment of an object key from old to new.

    Handles both layouts: ``prefix/{book}/...`` (a directory segment) and
    ``prefix/{book}.ext`` / ``prefix/{book}`` (a filename stem). A key that does
    not embed the old book id is returned unchanged (so an unexpected key never
    silently lands in the wrong place).
    """
    for prefix in _BOOK_DIR_PREFIXES:
        if key.startswith(prefix):
            rest = key[len(prefix) :]
            seg, sep, tail = rest.partition("/")
            if seg == old_book_id:
                return f"{prefix}{new_book_id}{sep}{tail}"
            return key
    for prefix in _BOOK_FILE_PREFIXES:
        if key.startswith(prefix):
            rest = key[len(prefix) :]
            # rest is "{book}.ext" or "{book}"
            if rest == old_book_id:
                return f"{prefix}{new_book_id}"
            stem, dot, ext = rest.partition(".")
            if stem == old_book_id:
                return f"{prefix}{new_book_id}{dot}{ext}"
            return key
    return key


def rewrite_keys_in_book_row(
    row: dict[str, Any], old_book_id: str, new_book_id: str
) -> None:
    """Rewrite ``source_pdf_key`` / ``cover_key`` in a ``books`` row (in place)."""
    for col in ("source_pdf_key", "cover_key"):
        val = row.get(col)
        if isinstance(val, str) and val:
            row[col] = rewrite_key_book_id(val, old_book_id, new_book_id)


def rewrite_keys_in_page_row(
    row: dict[str, Any], old_book_id: str, new_book_id: str
) -> None:
    """Rewrite ``image_key`` in a ``pages`` row (in place)."""
    val = row.get("image_key")
    if isinstance(val, str) and val:
        row["image_key"] = rewrite_key_book_id(val, old_book_id, new_book_id)


def rewrite_keys_in_shot_row(
    row: dict[str, Any], old_book_id: str, new_book_id: str
) -> None:
    """Rewrite clip/last-frame/audio keys in a ``shots`` row (in place)."""
    output = row.get("output")
    if isinstance(output, dict):
        for col in ("clip_key", "last_frame_key"):
            val = output.get(col)
            if isinstance(val, str) and val:
                output[col] = rewrite_key_book_id(val, old_book_id, new_book_id)
    narration = row.get("narration")
    if isinstance(narration, dict):
        val = narration.get("audio_key")
        if isinstance(val, str) and val:
            narration["audio_key"] = rewrite_key_book_id(val, old_book_id, new_book_id)


def rewrite_keys_in_entity_row(
    row: dict[str, Any], old_book_id: str, new_book_id: str
) -> None:
    """Rewrite reference-image + voice keys in an ``entities`` row (in place)."""
    appearance = row.get("appearance")
    if isinstance(appearance, dict):
        for item in appearance.get("reference_images") or []:
            if isinstance(item, dict):
                for col in ("key", "oss_key"):
                    val = item.get(col)
                    if isinstance(val, str) and val:
                        item[col] = rewrite_key_book_id(val, old_book_id, new_book_id)
        refk = appearance.get("reference_image_keys")
        if isinstance(refk, list):
            appearance["reference_image_keys"] = [
                rewrite_key_book_id(k, old_book_id, new_book_id) if isinstance(k, str) else k
                for k in refk
            ]
    voice = row.get("voice")
    if isinstance(voice, dict):
        val = voice.get("reference_audio_key")
        if isinstance(val, str) and val:
            voice["reference_audio_key"] = rewrite_key_book_id(val, old_book_id, new_book_id)


def rewrite_keys_in_shot_cache_row(
    row: dict[str, Any], old_book_id: str, new_book_id: str
) -> None:
    """Rewrite clip/last-frame keys in a ``shot_cache`` row (in place)."""
    for col in ("clip_key", "last_frame_key"):
        val = row.get(col)
        if isinstance(val, str) and val:
            row[col] = rewrite_key_book_id(val, old_book_id, new_book_id)


#: table -> in-place key-rewriter, applied during import after id-remapping.
KEY_REWRITERS = {
    "books": rewrite_keys_in_book_row,
    "pages": rewrite_keys_in_page_row,
    "shots": rewrite_keys_in_shot_row,
    "entities": rewrite_keys_in_entity_row,
    "shot_cache": rewrite_keys_in_shot_cache_row,
}


__all__ = [
    "KEY_REWRITERS",
    "collect_book_keys",
    "deterministic_book_keys",
    "keys_from_book",
    "keys_from_entity",
    "keys_from_page",
    "keys_from_shot",
    "keys_from_shot_cache",
    "rewrite_key_book_id",
]
