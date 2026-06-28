"""Operator-facing report builders — the dashboards that prove the system works.

Pure functions over :mod:`app.reports.sources` aggregates:

* :func:`build_budget_report` — §11 video-second burn: ceiling, committed,
  reserved, remaining, and the top spenders.
* :func:`build_quality_report` — the §13 proof: accepted-footage efficiency,
  regeneration rate, CCS, defect breakdown.
* :func:`build_throughput_report` — §12 render-queue health: job statuses,
  success rate, retries, dead-letters.
* :func:`build_library_overview_report` — fleet rollup: books by status, totals.
"""

from __future__ import annotations

from datetime import datetime

from app.reports.format import (
    fmt_date,
    fmt_datetime,
    fmt_float,
    fmt_int,
    fmt_minutes,
    fmt_pct,
    fmt_pct_value,
    fmt_seconds,
)
from app.reports.model import (
    Badge,
    BadgeTone,
    Callout,
    CalloutTone,
    Chart,
    ChartKind,
    ColumnKind,
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
from app.reports.sources import (
    BudgetSnapshot,
    LibrarySnapshot,
    QualitySnapshot,
    SceneRow,
    ThroughputSnapshot,
)

# §13 pre-registered thresholds (mirrors app.eval.harness.PRE_REGISTERED intent).
EFFICIENCY_TARGET = 70.0  # accepted-footage efficiency (%)
CCS_TARGET = 0.82  # character consistency score
REGEN_TARGET = 0.35  # regeneration rate (lower is better)


def _kv(label: str, display: str, value: float, *, emph: bool = False) -> KeyValueItem:
    return KeyValueItem(label=label, stat=Stat(value=value, display=display), emphasis=emph)


def _scope_subtitle(book_title: str | None) -> str:
    return f"Book · {book_title}" if book_title else "Fleet-wide"


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def build_budget_report(
    snap: BudgetSnapshot,
    *,
    generated_at: datetime,
    book_title: str | None = None,
    book_titles: dict[str, str] | None = None,
) -> Report:
    """A §11 budget-burn operator report."""
    titles = book_titles or {}
    meta = ReportMeta(
        title="Budget Report",
        subtitle=_scope_subtitle(book_title),
        kind="budget",
        generated_at=fmt_datetime(generated_at),
        footer=f"Kinora operations · budget · {fmt_date(generated_at)}",
    )
    used_frac = snap.used_fraction
    over = snap.remaining_seconds <= 0
    headline = KeyValue(
        items=(
            _kv("Ceiling", fmt_seconds(snap.ceiling_seconds), snap.ceiling_seconds),
            _kv(
                "Committed",
                fmt_seconds(snap.committed_seconds),
                snap.committed_seconds,
                emph=True,
            ),
            _kv("Reserved", fmt_seconds(snap.reserved_seconds), snap.reserved_seconds),
            _kv("Remaining", fmt_seconds(snap.remaining_seconds), snap.remaining_seconds),
            _kv("Used", fmt_pct(used_frac), used_frac * 100),
            _kv("Reservations", fmt_int(snap.reservation_count), snap.reservation_count),
        ),
        columns=3,
    )
    burn = Chart(
        kind=ChartKind.PROGRESS,
        series=(Series(name="used", values=(used_frac,)),),
        height=44,
        options={"label": f"{fmt_pct(used_frac)} of {fmt_seconds(snap.ceiling_seconds)} used"},
    )
    split = Chart(
        kind=ChartKind.DONUT,
        series=(
            Series(name="Committed", values=(snap.committed_seconds,)),
            Series(name="Reserved", values=(snap.reserved_seconds,)),
            Series(name="Free", values=(snap.remaining_seconds,)),
        ),
        title="Budget split",
        height=200,
    )
    verdict = Callout(
        "Budget exhausted — live render is gated until seconds are released or the "
        "ceiling is raised."
        if over
        else f"{fmt_seconds(snap.remaining_seconds)} of video budget remain "
        f"({fmt_pct(1 - used_frac)} free).",
        tone=CalloutTone.DANGER if over else CalloutTone.SUCCESS,
        title="Status",
    )
    sections = [
        Section(
            title=None,
            blocks=(
                Heading("Video-second budget", level=1),
                Paragraph(
                    "Video-seconds are Kinora's hard-capped currency (§11). Every render "
                    "reserves before it runs and commits its actual cost on completion.",
                    muted=True,
                ),
                Spacer(6),
                headline,
                burn,
                verdict,
            ),
        ),
        Section(title="Where it went", blocks=(split,)),
    ]
    if snap.by_book:
        rows = tuple(
            {
                "book": titles.get(bid, bid),
                "seconds": fmt_seconds(secs),
                "minutes": fmt_minutes(secs),
            }
            for bid, secs in snap.by_book
        )
        sections.append(
            Section(
                title="Top spenders",
                blocks=(
                    Table(
                        columns=(
                            TableColumn("book", "Book"),
                            TableColumn("seconds", "Video", ColumnKind.SECONDS),
                            TableColumn("minutes", "Minutes", ColumnKind.NUMBER),
                        ),
                        rows=rows,
                        caption="Committed video-seconds by book",
                        total_row={
                            "book": "Total committed",
                            "seconds": fmt_seconds(snap.committed_seconds),
                            "minutes": fmt_minutes(snap.committed_seconds),
                        },
                    ),
                ),
            )
        )
    return Report(meta=meta, sections=tuple(sections))


# --------------------------------------------------------------------------- #
# Quality (§13)
# --------------------------------------------------------------------------- #


def build_quality_report(
    snap: QualitySnapshot,
    *,
    generated_at: datetime,
    book_title: str | None = None,
    baseline: QualitySnapshot | None = None,
) -> Report:
    """The §13 quality proof: efficiency, regen rate, CCS, defects.

    When ``baseline`` (a single-agent control arm) is supplied, the report charts
    the crew-vs-baseline comparison that converts the thesis into a number.
    """
    meta = ReportMeta(
        title="Quality Report",
        subtitle=_scope_subtitle(book_title),
        kind="quality",
        generated_at=fmt_datetime(generated_at),
        footer=f"Kinora operations · §13 metrics · {fmt_date(generated_at)}",
    )
    eff = snap.accepted_footage_efficiency
    regen = snap.regeneration_rate
    ccs = snap.mean_ccs
    eff_pass = eff >= EFFICIENCY_TARGET
    ccs_pass = ccs is None or ccs >= CCS_TARGET
    regen_pass = regen <= REGEN_TARGET
    overall_pass = eff_pass and ccs_pass and regen_pass
    headline = KeyValue(
        items=(
            _kv("Accepted-footage efficiency", fmt_pct_value(eff), eff, emph=True),
            _kv("Regeneration rate", fmt_pct(regen), regen * 100),
            _kv("Mean CCS", fmt_float(ccs, 3) if ccs is not None else "—", ccs or 0.0),
            _kv("Shots accepted", fmt_int(snap.accepted_shots), snap.accepted_shots),
            _kv("Shots total", fmt_int(snap.total_shots), snap.total_shots),
            _kv("Defects", fmt_int(snap.defect_count), snap.defect_count),
        ),
        columns=3,
    )
    blocks: list = [
        Heading("The §13 proof", level=1),
        Paragraph(
            "Kinora's bet: consistency is a memory problem. The crew + shared canon should "
            "beat a single-agent baseline on both accepted-footage efficiency and character "
            "consistency. The numbers below are measured over rendered shots.",
            muted=True,
        ),
        Spacer(6),
        headline,
        Badge(
            "PASS" if overall_pass else "REVIEW",
            tone=BadgeTone.SUCCESS if overall_pass else BadgeTone.WARNING,
        ),
    ]
    # Shot-status mix.
    blocks.append(
        Chart(
            kind=ChartKind.DONUT,
            series=(
                Series(name="Accepted", values=(snap.accepted_shots,)),
                Series(name="Degraded", values=(snap.degraded_shots,)),
                Series(name="Conflict", values=(snap.conflict_shots,)),
                Series(
                    name="Other",
                    values=(
                        max(
                            0,
                            snap.total_shots
                            - snap.accepted_shots
                            - snap.degraded_shots
                            - snap.conflict_shots,
                        ),
                    ),
                ),
            ),
            title="Shot outcomes",
            height=210,
        )
    )
    sections = [Section(title=None, blocks=tuple(blocks))]

    # Crew vs baseline comparison.
    if baseline is not None:
        comparison = Chart(
            kind=ChartKind.GROUPED_BAR,
            series=(
                Series(
                    name="Crew",
                    values=(
                        snap.accepted_footage_efficiency,
                        (snap.mean_ccs or 0.0) * 100.0,
                        (1.0 - snap.regeneration_rate) * 100.0,
                    ),
                ),
                Series(
                    name="Baseline",
                    values=(
                        baseline.accepted_footage_efficiency,
                        (baseline.mean_ccs or 0.0) * 100.0,
                        (1.0 - baseline.regeneration_rate) * 100.0,
                    ),
                ),
            ),
            labels=("Efficiency", "CCS×100", "First-try×100"),
            title="Crew vs single-agent baseline",
            height=240,
        )
        sections.append(
            Section(
                title="Crew vs baseline",
                blocks=(
                    Paragraph(
                        "Higher is better on every axis. Same book, same seeds, same prompts "
                        "— the only difference is memory + crew vs a single agent.",
                        muted=True,
                    ),
                    comparison,
                    Table(
                        columns=(
                            TableColumn("metric", "Metric"),
                            TableColumn("crew", "Crew", ColumnKind.NUMBER),
                            TableColumn("base", "Baseline", ColumnKind.NUMBER),
                        ),
                        rows=(
                            {
                                "metric": "Accepted-footage efficiency",
                                "crew": fmt_pct_value(snap.accepted_footage_efficiency),
                                "base": fmt_pct_value(baseline.accepted_footage_efficiency),
                            },
                            {
                                "metric": "Mean CCS",
                                "crew": fmt_float(snap.mean_ccs or 0.0, 3),
                                "base": fmt_float(baseline.mean_ccs or 0.0, 3),
                            },
                            {
                                "metric": "Regeneration rate",
                                "crew": fmt_pct(snap.regeneration_rate),
                                "base": fmt_pct(baseline.regeneration_rate),
                            },
                        ),
                    ),
                ),
            )
        )

    # Defects.
    if snap.defects_by_kind:
        sections.append(
            Section(
                title="Defects",
                blocks=(
                    Chart(
                        kind=ChartKind.BAR,
                        series=(
                            Series(name="count", values=tuple(c for _, c in snap.defects_by_kind)),
                        ),
                        labels=tuple(k for k, _ in snap.defects_by_kind),
                        title="Defects by kind",
                        height=200,
                    ),
                    Table(
                        columns=(
                            TableColumn("kind", "Defect kind"),
                            TableColumn("count", "Count", ColumnKind.NUMBER),
                        ),
                        rows=tuple(
                            {"kind": k, "count": fmt_int(c)} for k, c in snap.defects_by_kind
                        ),
                        total_row={"kind": "Total", "count": fmt_int(snap.defect_count)},
                    ),
                ),
            )
        )
    return Report(meta=meta, sections=tuple(sections))


# --------------------------------------------------------------------------- #
# Render throughput (§12)
# --------------------------------------------------------------------------- #


def build_throughput_report(
    snap: ThroughputSnapshot, *, generated_at: datetime, book_title: str | None = None
) -> Report:
    """A §12 render-queue throughput / health report."""
    meta = ReportMeta(
        title="Render Throughput",
        subtitle=_scope_subtitle(book_title),
        kind="render_throughput",
        generated_at=fmt_datetime(generated_at),
        footer=f"Kinora operations · render queue · {fmt_date(generated_at)}",
    )
    success = snap.success_rate
    healthy = snap.deadletter == 0 and success >= 0.9
    headline = KeyValue(
        items=(
            _kv("Jobs", fmt_int(snap.jobs_total), snap.jobs_total),
            _kv("Succeeded", fmt_int(snap.succeeded), snap.succeeded, emph=True),
            _kv("Success rate", fmt_pct(success), success * 100),
            _kv("Dead-lettered", fmt_int(snap.deadletter), snap.deadletter),
            _kv("Cancelled", fmt_int(snap.cancelled), snap.cancelled),
            _kv("Mean attempts", fmt_float(snap.mean_attempts, 2), snap.mean_attempts),
        ),
        columns=3,
    )
    status_chart = Chart(
        kind=ChartKind.BAR,
        series=(Series(name="jobs", values=tuple(c for _, c in snap.by_status)),),
        labels=tuple(s for s, _ in snap.by_status),
        title="Jobs by status",
        height=220,
    )
    verdict = Callout(
        "Queue healthy — no dead-letters and a high success rate."
        if healthy
        else f"{snap.deadletter} dead-lettered job(s); success rate {fmt_pct(success)}. "
        "Investigate retries / provider errors.",
        tone=CalloutTone.SUCCESS if healthy else CalloutTone.WARNING,
        title="Queue health",
    )
    return Report(
        meta=meta,
        sections=(
            Section(
                title=None,
                blocks=(
                    Heading("Render queue", level=1),
                    Paragraph(
                        "The §12 render queue is cancellable, idempotent, and dead-lettered. "
                        "These counts cover every job recorded.",
                        muted=True,
                    ),
                    Spacer(6),
                    headline,
                    verdict,
                ),
            ),
            Section(
                title="Status breakdown",
                blocks=(
                    status_chart,
                    Table(
                        columns=(
                            TableColumn("status", "Status"),
                            TableColumn("count", "Jobs", ColumnKind.NUMBER),
                        ),
                        rows=tuple(
                            {"status": s, "count": fmt_int(c)} for s, c in snap.by_status
                        ),
                        total_row={"status": "Total", "count": fmt_int(snap.jobs_total)},
                    ),
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Library overview (fleet)
# --------------------------------------------------------------------------- #


def build_library_overview_report(
    snap: LibrarySnapshot,
    *,
    generated_at: datetime,
    scenes: tuple[SceneRow, ...] = (),
    book_title: str | None = None,
) -> Report:
    """A fleet-level library overview (books by status + optional scene drill-down)."""
    meta = ReportMeta(
        title="Library Overview",
        subtitle=_scope_subtitle(book_title),
        kind="library_overview",
        generated_at=fmt_datetime(generated_at),
        footer=f"Kinora operations · library · {fmt_date(generated_at)}",
    )
    accept_frac = (snap.accepted_shots / snap.total_shots) if snap.total_shots else 0.0
    headline = KeyValue(
        items=(
            _kv("Books", fmt_int(snap.total_books), snap.total_books, emph=True),
            _kv("Readers", fmt_int(snap.total_users), snap.total_users),
            _kv("Shots", fmt_int(snap.total_shots), snap.total_shots),
            _kv("Accepted shots", fmt_int(snap.accepted_shots), snap.accepted_shots),
            _kv("Acceptance", fmt_pct(accept_frac), accept_frac * 100),
        ),
        columns=3,
    )
    status_chart = Chart(
        kind=ChartKind.PIE,
        series=tuple(Series(name=s, values=(c,)) for s, c in snap.by_status),
        title="Books by status",
        height=210,
    )
    sections = [
        Section(
            title=None,
            blocks=(
                Heading("Library at a glance", level=1),
                Spacer(4),
                headline,
                status_chart,
                Table(
                    columns=(
                        TableColumn("status", "Status"),
                        TableColumn("count", "Books", ColumnKind.NUMBER),
                    ),
                    rows=tuple({"status": s, "count": fmt_int(c)} for s, c in snap.by_status),
                    total_row={"status": "Total", "count": fmt_int(snap.total_books)},
                ),
            ),
        )
    ]
    if scenes:
        sections.append(
            Section(
                title="Per-scene spend",
                blocks=(
                    Table(
                        columns=(
                            TableColumn("scene", "Scene"),
                            TableColumn("shots", "Shots", ColumnKind.NUMBER),
                            TableColumn("accepted", "Accepted", ColumnKind.NUMBER),
                            TableColumn("secs", "Video", ColumnKind.SECONDS),
                        ),
                        rows=tuple(
                            {
                                "scene": s.title or s.scene_id,
                                "shots": fmt_int(s.shots),
                                "accepted": fmt_int(s.accepted),
                                "secs": fmt_seconds(s.video_seconds),
                            }
                            for s in scenes
                        ),
                        caption="Shots + spend per scene",
                    ),
                ),
            )
        )
    return Report(meta=meta, sections=tuple(sections))


__all__ = [
    "CCS_TARGET",
    "EFFICIENCY_TARGET",
    "REGEN_TARGET",
    "build_budget_report",
    "build_library_overview_report",
    "build_quality_report",
    "build_throughput_report",
]
