"""Tiny stdlib HTML → readable-text extractor (no third-party dependency).

The web-article connector needs to turn a raw HTML page into clean reading
blocks. Rather than pull in ``readability``/``beautifulsoup4`` (and grow the
dependency surface), this module does a deliberately small, robust job with only
``html.parser`` from the stdlib:

* strip ``<script>``/``<style>``/nav/footer boilerplate,
* keep block-level text (``<p>``, ``<h1..h3>``, ``<blockquote>``, ``<li>``),
* collapse whitespace and decode entities.

It is heuristic, not a full readability port — but it is offline, deterministic,
and good enough to feed clean prose to the ingest pipeline. Anything fancier
(JS-rendered SPAs) is explicitly out of scope; the connector surfaces an empty
document and the caller skips it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: Tags whose entire subtree is discarded (boilerplate / non-content).
_DROP_SUBTREES = frozenset(
    {"script", "style", "noscript", "nav", "footer", "header", "aside", "form",
     "svg", "button", "iframe", "figure"}
)
#: Block tags that start a new text block; the tuple maps tag -> block kind name.
_BLOCK_TAGS = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "pre", "div", "section",
     "article", "br"}
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_WS = re.compile(r"\s+")


@dataclass
class ExtractedBlock:
    """A block of extracted text with a coarse role tag."""

    role: str  # "heading" | "subheading" | "quote" | "paragraph"
    text: str


@dataclass
class _Collector(HTMLParser):
    """An HTML parser that accumulates block-level readable text."""

    blocks: list[ExtractedBlock] = field(default_factory=list)
    _drop_depth: int = 0
    _buf: list[str] = field(default_factory=list)
    _role: str = "paragraph"
    _title: str | None = None
    _in_title: bool = False

    def __post_init__(self) -> None:
        super().__init__(convert_charrefs=True)

    # -- block boundary helpers -------------------------------------------- #
    def _flush(self) -> None:
        text = _WS.sub(" ", "".join(self._buf)).strip()
        self._buf.clear()
        if text:
            self.blocks.append(ExtractedBlock(role=self._role, text=text))
        self._role = "paragraph"

    # -- HTMLParser hooks --------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_SUBTREES:
            self._drop_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if self._drop_depth:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS:
                self._role = "heading" if tag in {"h1", "h2"} else "subheading"
            elif tag == "blockquote":
                self._role = "quote"

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_SUBTREES:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if self._drop_depth:
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title and self._title is None:
            t = data.strip()
            if t:
                self._title = t
            return
        if self._drop_depth:
            return
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


@dataclass(frozen=True)
class ExtractedArticle:
    """The result of extracting readable content from an HTML page."""

    title: str | None
    blocks: tuple[ExtractedBlock, ...]

    def word_count(self) -> int:
        """Total words across all extracted blocks."""
        return sum(len(b.text.split()) for b in self.blocks)


def extract_article(html: str, *, min_block_words: int = 3) -> ExtractedArticle:
    """Extract a title and readable text blocks from raw HTML.

    Args:
        html: the raw page source.
        min_block_words: drop blocks shorter than this many words (kills nav
            crumbs, share-button labels, and other one/two-word noise).

    Returns:
        An :class:`ExtractedArticle`; ``blocks`` may be empty for a page with no
        extractable prose (the caller then skips the item).
    """
    collector = _Collector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # noqa: BLE001 - a malformed page yields whatever parsed
        pass
    kept = tuple(
        b for b in collector.blocks
        if b.role in {"heading", "subheading"} or len(b.text.split()) >= min_block_words
    )
    return ExtractedArticle(title=collector._title, blocks=kept)


__all__ = ["ExtractedArticle", "ExtractedBlock", "extract_article"]
