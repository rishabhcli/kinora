"""Page analysis — Qwen-VL reads each page for the canon (§9.1 step 2).

For every extracted page this calls ``providers.vl.analyze_json`` with the
rendered page image **and** its text, asking the model to emit, as JSON:

* ``entities`` — characters / locations / props named on the page, each with a
  described appearance (the raw material the canon dedups in
  :mod:`app.ingest.canon_build`);
* ``described_visuals`` — the concrete visual content of the page;
* ``states`` — simple establishing facts (possessions, locations) that become
  the initial continuity states;
* ``illustrations`` — any illustrations / manga panels detected in the image.

Pages are analysed with **bounded real concurrency** (an ``asyncio.Semaphore``
over genuine parallel ``vl.analyze_json`` calls), not a batch stub. The shot
list's narrative beats come from the Adapter in :mod:`app.ingest.shot_plan`; this
VL pass is dedicated to populating the canon and detecting illustrations, so the
two model passes have clean, separable responsibilities.

For a large back-catalogue ingest, Alibaba Model Studio's **batch API** (≈50% off,
§9.1/§11) would slot in exactly here — replacing the per-page concurrent calls
with one batch submission — but real concurrent calls are implemented now.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.logging import get_logger
from app.ingest.pdf_extract import PageExtract
from app.memory.interfaces import BlobStore
from app.providers import Providers

logger = get_logger("app.ingest.analyze")

EntityKind = Literal["character", "location", "prop"]

#: Default bound on simultaneous in-flight VL calls (real parallelism).
DEFAULT_CONCURRENCY = 4
#: Cap the per-page JSON generation; page analyses are short structured objects.
DEFAULT_MAX_TOKENS = 1500

_ANALYZE_PROMPT = (
    "You are a story analyst preparing a book for adaptation. You are given ONE "
    "page of a book as an image and its extracted text. Read both the text and "
    "any illustration on the page, then return a SINGLE JSON object describing "
    "the page:\n"
    "{\n"
    '  "summary": "<one or two lines of what happens on this page>",\n'
    '  "described_visuals": "<the concrete visual content to depict>",\n'
    '  "entities": [\n'
    '    {"name": "<the entity name exactly as the text refers to it>",\n'
    '     "kind": "character" | "location" | "prop",\n'
    '     "appearance": "<a concrete visual description of how it looks>",\n'
    '     "aliases": ["<other names the same entity is called>"]}\n'
    "  ],\n"
    '  "states": [\n'
    '    {"subject": "<entity name>", "predicate": "<possesses|located_in|wears|'
    'holds|is>", "object": "<entity name or short literal>"}\n'
    "  ],\n"
    '  "illustrations": [\n'
    '    {"description": "<what the picture shows>", "kind": "illustration" | '
    '"manga_panel"}\n'
    "  ]\n"
    "}\n"
    "\n"
    "RULES: Only list entities, states, and illustrations that are actually "
    "supported by the page — never invent them. Give every character and "
    "location a concrete appearance so the look can be locked. If the page has no "
    "picture, return an empty illustrations list. Output ONLY the JSON object — "
    "no prose, no markdown fences."
)


class AnalyzedEntity(BaseModel):
    """One entity the VL model found on a page (pre-dedup canon material)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    kind: EntityKind = "character"
    appearance: str = ""
    aliases: list[str] = Field(default_factory=list)


class AnalyzedState(BaseModel):
    """An establishing fact on a page → an initial continuity state (§8.1)."""

    model_config = ConfigDict(extra="ignore")

    subject: str
    predicate: str
    object: str


class DetectedIllustration(BaseModel):
    """An illustration / manga panel detected in the page image (§9.1 step 2)."""

    model_config = ConfigDict(extra="ignore")

    description: str = ""
    kind: str = "illustration"


class PageAnalysis(BaseModel):
    """The VL model's structured reading of one page."""

    model_config = ConfigDict(extra="ignore")

    page_number: int
    summary: str = ""
    described_visuals: str = ""
    entities: list[AnalyzedEntity] = Field(default_factory=list)
    states: list[AnalyzedState] = Field(default_factory=list)
    illustrations: list[DetectedIllustration] = Field(default_factory=list)


async def _analyze_one(
    page: PageExtract,
    *,
    providers: Providers,
    blob_store: BlobStore,
    semaphore: asyncio.Semaphore,
    max_tokens: int,
    model: str | None,
) -> PageAnalysis:
    """Analyse a single page; never raises — a bad page yields an empty analysis."""
    async with semaphore:
        try:
            image = await anyio.to_thread.run_sync(blob_store.get_bytes, page.image_key)
            prompt = f"{_ANALYZE_PROMPT}\n\nPAGE {page.page_number} TEXT:\n{page.text}"
            raw = await providers.vl.analyze_json(
                [image], prompt, max_tokens=max_tokens, model=model
            )
            payload = raw if isinstance(raw, dict) else {}
            payload["page_number"] = page.page_number
            return PageAnalysis.model_validate(payload)
        except (ValidationError, ValueError, KeyError) as exc:
            logger.warning(
                "ingest.analyze.page_failed", page_number=page.page_number, error=str(exc)
            )
            return PageAnalysis(page_number=page.page_number)
        except Exception as exc:  # noqa: BLE001 - one flaky page must not kill ingest
            logger.warning(
                "ingest.analyze.page_error", page_number=page.page_number, error=str(exc)
            )
            return PageAnalysis(page_number=page.page_number)


async def analyze_pages(
    pages: list[PageExtract],
    *,
    providers: Providers,
    blob_store: BlobStore,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str | None = None,
) -> list[PageAnalysis]:
    """Analyse every page with bounded real concurrency; returns analyses in order.

    Args:
        pages: the extracted pages (each carries its image key + text).
        providers: the live provider bundle (uses ``providers.vl``).
        blob_store: object store the page PNGs were uploaded to.
        concurrency: max simultaneous in-flight VL calls.
        max_tokens: per-page generation cap.
        model: optional VL model override (defaults to ``settings.vl_model``).
    """
    if not pages:
        return []
    semaphore = asyncio.Semaphore(max(1, concurrency))
    tasks = [
        _analyze_one(
            page,
            providers=providers,
            blob_store=blob_store,
            semaphore=semaphore,
            max_tokens=max_tokens,
            model=model,
        )
        for page in pages
    ]
    analyses = await asyncio.gather(*tasks)
    logger.info(
        "ingest.analyze.done",
        num_pages=len(analyses),
        entities=sum(len(a.entities) for a in analyses),
        illustrations=sum(len(a.illustrations) for a in analyses),
    )
    return list(analyses)


__all__ = [
    "AnalyzedEntity",
    "AnalyzedState",
    "DetectedIllustration",
    "PageAnalysis",
    "analyze_pages",
]
