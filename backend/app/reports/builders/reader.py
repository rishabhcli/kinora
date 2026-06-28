"""Reader-facing report builders — keepsakes a reader actually wants to keep.

Pure functions over :mod:`app.reports.sources` aggregates:

* :func:`build_reading_progress_report` — where you are in one book, how much
  film you've watched, what's left.
* :func:`build_completion_certificate` — a celebratory one-page certificate for
  a finished book (light brand, big seal).
* :func:`build_year_in_review` — a Spotify-Wrapped-style annual rollup over the
  whole library.
* :func:`build_highlights_digest` — a compact multi-book digest of recent
  reading activity.
"""

from __future__ import annotations

from datetime import datetime

from app.reports.format import (
    fmt_date,
    fmt_datetime,
    fmt_duration_clock,
    fmt_int,
    fmt_minutes,
    fmt_pct,
    pluralize,
)
from app.reports.model import (
    Badge,
    BadgeTone,
    Callout,
    CalloutTone,
    Chart,
    ChartKind,
    ColumnKind,
    Divider,
    Heading,
    KeyValue,
    KeyValueItem,
    Paragraph,
    Report,
    ReportMeta,
    Section,
    Series,
    Spacer,
    Stat,
    Table,
    TableColumn,
)
from app.reports.sources import BookProgress, ReaderSummary


def _kv(label: str, display: str, value: float, *, emph: bool = False) -> KeyValueItem:
    return KeyValueItem(label=label, stat=Stat(value=value, display=display), emphasis=emph)


# --------------------------------------------------------------------------- #
# Reading progress (one book)
# --------------------------------------------------------------------------- #


def build_reading_progress_report(
    progress: BookProgress, *, generated_at: datetime, reader_name: str | None = None
) -> Report:
    """A single-book reading-progress report."""
    pct = progress.percent_complete
    words_left = max(0, progress.total_words - progress.furthest_word)
    subject = progress.title
    meta = ReportMeta(
        title="Reading Progress",
        subtitle=f"{progress.title}" + (f" · {progress.author}" if progress.author else ""),
        kind="reading_progress",
        subject=subject,
        generated_at=fmt_datetime(generated_at),
        footer=f"Generated for {reader_name or 'you'} · {fmt_date(generated_at)}",
    )
    headline = KeyValue(
        items=(
            _kv("Complete", fmt_pct(pct), pct * 100, emph=True),
            _kv("Words read", fmt_int(progress.furthest_word), progress.furthest_word),
            _kv("Film watched", fmt_minutes(progress.watched_seconds), progress.watched_seconds),
        ),
        columns=3,
    )
    progress_chart = Chart(
        kind=ChartKind.PROGRESS,
        series=(Series(name="complete", values=(pct,)),),
        height=44,
        options={"label": fmt_pct(pct)},
    )
    pace = Paragraph(
        f"You've reached word {fmt_int(progress.furthest_word)} of "
        f"{fmt_int(progress.total_words)} — about {pluralize(words_left, 'word')} to go. "
        f"{progress.accepted_shots} of {progress.total_shots} shots have been rendered and "
        f"accepted into your film so far."
        if progress.total_words
        else "This book hasn't been opened yet — start reading to see your film appear.",
        muted=True,
    )
    verdict_tone = CalloutTone.SUCCESS if progress.is_complete else CalloutTone.INFO
    verdict_text = (
        "You finished this book — request a completion certificate to mark it."
        if progress.is_complete
        else f"Keep going: you're {fmt_pct(pct)} of the way through."
    )
    sections = [
        Section(
            title=None,
            blocks=(
                Heading(progress.title, level=1),
                Paragraph(progress.author or "Unknown author", muted=True),
                Spacer(8),
                headline,
                progress_chart,
                pace,
                Callout(verdict_text, tone=verdict_tone, title="Where you stand"),
            ),
        ),
        Section(
            title="The numbers",
            blocks=(
                Table(
                    columns=(
                        TableColumn("metric", "Metric"),
                        TableColumn("value", "Value", ColumnKind.NUMBER),
                    ),
                    rows=(
                        {"metric": "Pages", "value": fmt_int(progress.num_pages)},
                        {"metric": "Words in book", "value": fmt_int(progress.total_words)},
                        {"metric": "Words read", "value": fmt_int(progress.furthest_word)},
                        {"metric": "Shots accepted", "value": fmt_int(progress.accepted_shots)},
                        {"metric": "Shots total", "value": fmt_int(progress.total_shots)},
                        {
                            "metric": "Film watched",
                            "value": fmt_duration_clock(progress.watched_seconds),
                        },
                        {
                            "metric": "Last read",
                            "value": fmt_date(progress.last_read_at)
                            if progress.last_read_at
                            else "—",
                        },
                    ),
                    caption="Your progress, in full",
                ),
            ),
        ),
    ]
    return Report(meta=meta, sections=tuple(sections))


# --------------------------------------------------------------------------- #
# Completion certificate (one finished book)
# --------------------------------------------------------------------------- #


def build_completion_certificate(
    progress: BookProgress,
    *,
    generated_at: datetime,
    reader_name: str | None = None,
    certificate_no: str | None = None,
) -> Report:
    """A one-page completion certificate for a finished book."""
    name = reader_name or "Reader"
    meta = ReportMeta(
        title="Certificate of Completion",
        subtitle=progress.title,
        kind="completion_certificate",
        subject=progress.title,
        generated_at=fmt_datetime(generated_at),
        footer=(
            f"Kinora · {fmt_date(generated_at)}"
            + (f" · No. {certificate_no}" if certificate_no else "")
        ),
    )
    blocks = (
        Spacer(10),
        Badge("COMPLETED", tone=BadgeTone.SUCCESS),
        Spacer(6),
        Heading("This certifies that", level=3),
        Heading(name, level=1),
        Paragraph("has watched and read to the end of", muted=True),
        Heading(f"“{progress.title}”", level=1),
        Paragraph(
            (f"by {progress.author}" if progress.author else "")
            + f" — {pluralize(progress.num_pages, 'page')}, "
            + f"{pluralize(progress.accepted_shots, 'shot')} of generated film, "
            + f"{fmt_minutes(progress.watched_seconds)} of viewing.",
            muted=True,
        ),
        Spacer(12),
        KeyValue(
            items=(
                _kv("Pages", fmt_int(progress.num_pages), progress.num_pages),
                _kv(
                    "Film",
                    fmt_minutes(progress.watched_seconds),
                    progress.watched_seconds,
                    emph=True,
                ),
                _kv("Completed", fmt_date(generated_at), 0),
            ),
            columns=3,
        ),
        Spacer(10),
        Callout(
            "Every shot in this film was kept visually consistent by Kinora's shared "
            "canon — one story, one look, start to finish.",
            tone=CalloutTone.NEUTRAL,
        ),
    )
    return Report(meta=meta, sections=(Section(title=None, blocks=blocks),))


# --------------------------------------------------------------------------- #
# Year in review (whole library)
# --------------------------------------------------------------------------- #


def build_year_in_review(
    summary: ReaderSummary, *, year: int, generated_at: datetime, reader_name: str | None = None
) -> Report:
    """An annual rollup over the reader's whole library."""
    meta = ReportMeta(
        title=f"{year} in Reading",
        subtitle="Your year, watched",
        kind="year_in_review",
        subject=str(year),
        generated_at=fmt_datetime(generated_at),
        footer=f"Kinora year in review · {reader_name or 'you'} · {year}",
    )
    completed = [b for b in summary.books if b.is_complete]
    top = sorted(summary.books, key=lambda b: -b.watched_seconds)[:8]
    headline = KeyValue(
        items=(
            _kv(
                "Books finished",
                fmt_int(summary.books_completed),
                summary.books_completed,
                emph=True,
            ),
            _kv("Books opened", fmt_int(summary.books_started), summary.books_started),
            _kv("Pages turned", fmt_int(summary.total_pages), summary.total_pages),
            _kv(
                "Time watched",
                fmt_minutes(summary.total_watched_seconds),
                summary.total_watched_seconds,
            ),
            _kv(
                "Shots watched",
                fmt_int(summary.total_accepted_shots),
                summary.total_accepted_shots,
            ),
            _kv("Library size", fmt_int(len(summary.books)), len(summary.books)),
        ),
        columns=3,
    )
    watch_chart = Chart(
        kind=ChartKind.BAR,
        series=(Series(name="minutes", values=tuple(b.watched_seconds / 60.0 for b in top)),),
        labels=tuple(_short(b.title) for b in top),
        title="Minutes watched, by book",
        height=240,
    )
    finished_rows = tuple(
        {
            "title": b.title,
            "author": b.author or "—",
            "pages": fmt_int(b.num_pages),
            "watched": fmt_minutes(b.watched_seconds),
        }
        for b in sorted(completed, key=lambda b: -b.watched_seconds)
    )
    sections = [
        Section(
            title=None,
            blocks=(
                Heading(f"Your {year}", level=1),
                Paragraph(
                    f"You opened {pluralize(summary.books_started, 'book')} this year "
                    f"and saw {pluralize(summary.books_completed, 'book')} all the way "
                    f"to the credits.",
                    muted=True,
                ),
                Spacer(6),
                headline,
            ),
        ),
        Section(title="Where the time went", blocks=(watch_chart,)),
    ]
    if finished_rows:
        sections.append(
            Section(
                title="Finished this year",
                blocks=(
                    Table(
                        columns=(
                            TableColumn("title", "Book"),
                            TableColumn("author", "Author"),
                            TableColumn("pages", "Pages", ColumnKind.NUMBER),
                            TableColumn("watched", "Watched", ColumnKind.NUMBER),
                        ),
                        rows=finished_rows,
                    ),
                ),
            )
        )
    else:
        sections.append(
            Section(
                title="Finished this year",
                blocks=(
                    Callout(
                        "No books finished yet — there's still time. Your most-watched so "
                        f"far is “{top[0].title}”." if top else "Start a book to begin your year.",
                        tone=CalloutTone.INFO,
                    ),
                ),
            )
        )
    return Report(meta=meta, sections=tuple(sections))


# --------------------------------------------------------------------------- #
# Highlights digest (recent activity, compact)
# --------------------------------------------------------------------------- #


def build_highlights_digest(
    summary: ReaderSummary,
    *,
    generated_at: datetime,
    reader_name: str | None = None,
    limit: int = 6,
) -> Report:
    """A compact digest of the reader's most active books (e.g. weekly email)."""
    meta = ReportMeta(
        title="Your Reading Digest",
        subtitle="Recent highlights from your library",
        kind="highlights_digest",
        generated_at=fmt_datetime(generated_at),
        footer=f"Kinora digest · {fmt_date(generated_at)}",
    )
    active = sorted(
        (b for b in summary.books if b.furthest_word > 0),
        key=lambda b: (b.last_read_at or datetime.min.replace(tzinfo=generated_at.tzinfo)),
        reverse=True,
    )[:limit]
    blocks: list = [
        Heading("Recently in your reading room", level=1),
        Paragraph(
            f"{pluralize(len(active), 'book')} in progress · "
            f"{fmt_minutes(summary.total_watched_seconds)} watched overall.",
            muted=True,
        ),
        Spacer(6),
    ]
    if not active:
        blocks.append(
            Callout("Nothing in progress — pick a book and press play.", tone=CalloutTone.INFO)
        )
    for i, b in enumerate(active):
        if i:
            blocks.append(Divider())
        blocks.append(Heading(b.title, level=3))
        blocks.append(
            Paragraph(
                (f"{b.author} · " if b.author else "")
                + f"{fmt_pct(b.percent_complete)} complete · "
                + f"{fmt_minutes(b.watched_seconds)} watched"
                + (f" · last read {fmt_date(b.last_read_at)}" if b.last_read_at else ""),
                muted=True,
            )
        )
        blocks.append(
            Chart(
                kind=ChartKind.PROGRESS,
                series=(Series(name="p", values=(b.percent_complete,)),),
                height=36,
                options={"label": fmt_pct(b.percent_complete)},
            )
        )
    return Report(meta=meta, sections=(Section(title=None, blocks=tuple(blocks)),))


def _short(title: str, n: int = 18) -> str:
    """Truncate a title for a chart axis label."""
    return title if len(title) <= n else title[: n - 1] + "…"


__all__ = [
    "build_completion_certificate",
    "build_highlights_digest",
    "build_reading_progress_report",
    "build_year_in_review",
]
