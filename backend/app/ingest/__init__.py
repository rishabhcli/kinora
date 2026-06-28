"""Phase A ingest — the cheap, global, token-only import pipeline (kinora.md §9.1).

Runs once when a book is added and spends **zero video-seconds**: a multi-format
funnel (:mod:`app.ingest.formats` — PDF/EPUB/DOCX/Markdown/HTML/TXT) normalises
the upload to PDF → streaming PyMuPDF extract (page images + text + per-word boxes
+ the book-global word index, with multi-column reading-order from
:mod:`app.ingest.layout` and an OCR fallback from :mod:`app.ingest.ocr` for
scanned pages) → rate-controlled Qwen-VL page analysis → versioned canon →
the Adapter's shot list reconciled to REAL global word ranges as the §4.2
source-span index → identity lock. Each milestone is checkpointed
(:mod:`app.ingest.checkpoints`) so a crashed import resumes, and a changed source
can be diffed for incremental re-ingest (:mod:`app.ingest.diff`).

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
from app.ingest.checkpoints import (
    clear_checkpoints,
    completed_milestones,
    record_milestone,
)
from app.ingest.diff import (
    IngestDiff,
    PageChange,
    PageDiff,
    diff_pages,
    should_full_reingest,
    text_hash,
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
from app.ingest.formats import (
    NormalizedSource,
    SourceFormat,
    UnsupportedFormatError,
    detect_format,
    format_from_extension,
    html_to_pdf_bytes,
    markdown_to_html,
    normalize_to_pdf,
)
from app.ingest.identity_lock import (
    IdentityLockResult,
    PresetVoice,
    assign_voices,
    lock_identities,
)
from app.ingest.layout import (
    Column,
    LayoutResult,
    Word,
    detect_columns,
    order_raw_words,
    reading_order,
)
from app.ingest.ocr import (
    OcrEngine,
    OcrResult,
    VlOcrEngine,
    looks_scanned,
    ocr_page,
    synthesize_word_boxes,
)
from app.ingest.pdf_extract import (
    PageExtract,
    PdfExtractResult,
    WordBox,
    extract_pdf,
    page_image_key,
)
from app.ingest.ratelimit import TokenBucket, is_transient, retrying
from app.ingest.service import (
    IngestError,
    IngestOptions,
    IngestResult,
    ProgressCallback,
    ReingestPlan,
    ingest_book,
    ingest_pdf,
    plan_reingest,
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
    "Column",
    "DetectedIllustration",
    "IdentityLockResult",
    "IngestDiff",
    "IngestError",
    "IngestOptions",
    "IngestResult",
    "LayoutResult",
    "NormalizedSource",
    "OcrEngine",
    "OcrResult",
    "PageAnalysis",
    "PageChange",
    "PageDiff",
    "PageExtract",
    "PageWords",
    "PdfExtractResult",
    "PresetVoice",
    "ProgressCallback",
    "ReingestPlan",
    "SceneGroup",
    "ShotPlanResult",
    "SourceFormat",
    "TokenBucket",
    "UnsupportedFormatError",
    "VlOcrEngine",
    "Word",
    "WordBox",
    "analyze_pages",
    "assign_voices",
    "build_canon",
    "clear_checkpoints",
    "completed_milestones",
    "detect_columns",
    "detect_format",
    "diff_pages",
    "entity_key_for",
    "epub_page_count",
    "epub_to_pdf_bytes",
    "extract_epub_cover",
    "extract_epub_metadata",
    "extract_pdf",
    "format_from_extension",
    "group_scenes",
    "html_to_pdf_bytes",
    "ingest_book",
    "ingest_pdf",
    "is_transient",
    "lock_identities",
    "looks_like_epub",
    "looks_scanned",
    "markdown_to_html",
    "normalize_name",
    "normalize_to_pdf",
    "ocr_page",
    "order_raw_words",
    "page_image_key",
    "plan_and_persist",
    "plan_reingest",
    "reading_order",
    "reconcile_beat_word_ranges",
    "record_milestone",
    "resolve_beat_entities",
    "retrying",
    "run_ingest",
    "should_full_reingest",
    "sniff_image_content_type",
    "synthesize_word_boxes",
    "text_hash",
]
