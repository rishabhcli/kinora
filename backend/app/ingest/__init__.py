"""Phase A ingest — the cheap, global, token-only import pipeline (kinora.md §9.1).

Runs once when a book is added and spends **zero video-seconds**: PyMuPDF extract
(page images + text + per-word boxes + the book-global word index) → Qwen-VL page
analysis (entities, visuals, illustrations) → versioned canon population (+ Style
node + initial continuity states) → the Adapter's shot list reconciled to REAL
global word ranges as the §4.2 source-span index → identity lock (canonical
keyframes + distinct preset voices for principal characters).

:func:`ingest_pdf` / :func:`ingest_book` orchestrate the whole flow end-to-end
with a progress callback; :mod:`app.ingest.worker` is the runnable entrypoint.
"""

from __future__ import annotations

from app.ingest.analyze import (
    AnalyzedEntity,
    AnalyzedState,
    DetectedIllustration,
    PageAnalysis,
    analyze_pages,
)
from app.ingest.canon_build import (
    CanonBuildResult,
    CanonEntity,
    build_canon,
    entity_key_for,
    normalize_name,
)
from app.ingest.epub_extract import (
    EPUB_CONTENT_TYPE,
    epub_page_count,
    epub_to_pdf_bytes,
    extract_epub_cover,
    extract_epub_metadata,
    looks_like_epub,
    sniff_image_content_type,
)
from app.ingest.identity_lock import (
    IdentityLockResult,
    PresetVoice,
    assign_voices,
    lock_identities,
)
from app.ingest.pdf_extract import (
    PageExtract,
    PdfExtractResult,
    WordBox,
    extract_pdf,
    page_image_key,
)
from app.ingest.service import (
    IngestError,
    IngestOptions,
    IngestResult,
    ProgressCallback,
    ingest_book,
    ingest_pdf,
)
from app.ingest.shot_plan import (
    BeatSpanInput,
    PageWords,
    SceneGroup,
    ShotPlanResult,
    group_scenes,
    plan_and_persist,
    reconcile_beat_word_ranges,
    resolve_beat_entities,
)
from app.ingest.worker import run_ingest

__all__ = [
    "EPUB_CONTENT_TYPE",
    "AnalyzedEntity",
    "AnalyzedState",
    "BeatSpanInput",
    "CanonBuildResult",
    "CanonEntity",
    "DetectedIllustration",
    "IdentityLockResult",
    "IngestError",
    "IngestOptions",
    "IngestResult",
    "PageAnalysis",
    "PageExtract",
    "PageWords",
    "PdfExtractResult",
    "PresetVoice",
    "ProgressCallback",
    "SceneGroup",
    "ShotPlanResult",
    "WordBox",
    "analyze_pages",
    "assign_voices",
    "build_canon",
    "entity_key_for",
    "epub_page_count",
    "epub_to_pdf_bytes",
    "extract_epub_cover",
    "extract_epub_metadata",
    "extract_pdf",
    "group_scenes",
    "ingest_book",
    "ingest_pdf",
    "lock_identities",
    "looks_like_epub",
    "normalize_name",
    "page_image_key",
    "plan_and_persist",
    "reconcile_beat_word_ranges",
    "resolve_beat_entities",
    "run_ingest",
    "sniff_image_content_type",
]
