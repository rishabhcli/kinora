"""Cost-attribution breakdowns: where the money went + $/finished-minute.

Pure analysis over a :class:`~app.usageanalytics.store.UsageMetricStore`. Two
breakdowns the operator dashboard wants that the raw leaderboards don't give
directly:

* :func:`cost_breakdown` — total USD over the window, plus the per-provider,
  per-model, and per-book split, each with its **share** of the total. Answers
  "which providers/books cost the most?".
* :func:`unit_economics` — the efficiency metric: **$/finished-minute-of-film**.
  The "finished minutes" are the accepted *video-seconds* delivered (a minute of
  film = 60 video-seconds), so the cost per finished minute is
  ``total_cost / (video_seconds / 60)``. Computed overall and per book, so a
  dashboard can rank books by how expensive their film is to produce. Books that
  cost money but produced no video yield ``None`` (undefined, not infinite).

All math uses :class:`~decimal.Decimal` for money and renders it as a string for
JSON. Never raises; a zero denominator yields ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.usageanalytics.aggregate import grouped, totals
from app.usageanalytics.store import BOOK, MODEL, PROVIDER, Dimension, UsageMetricStore

_ZERO = Decimal("0")
_SECONDS_PER_MINUTE = Decimal("60")


@dataclass(frozen=True, slots=True)
class CostShare:
    """One row of a cost breakdown: a key, its cost, and its share of the total."""

    key: str
    cost_usd: Decimal
    share: float  # fraction of the window's total cost in [0, 1]
    calls: int
    video_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "cost_usd": str(self.cost_usd),
            "share": round(self.share, 6),
            "calls": self.calls,
            "video_seconds": round(self.video_seconds, 3),
        }


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Total spend over a window, split by provider / model / book."""

    total_usd: Decimal
    by_provider: list[CostShare]
    by_model: list[CostShare]
    by_book: list[CostShare] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_usd": str(self.total_usd),
            "by_provider": [s.as_dict() for s in self.by_provider],
            "by_model": [s.as_dict() for s in self.by_model],
            "by_book": [s.as_dict() for s in self.by_book],
        }


def _shares(
    store: UsageMetricStore,
    axis: str,
    *,
    since: datetime,
    until: datetime,
    where: Dimension | None,
    total_cost: Decimal,
) -> list[CostShare]:
    table = grouped(store, axes=[axis], since=since, until=until, where=where)
    rows: list[CostShare] = []
    for key, cell in table.items():
        label = key[0] if key else ""
        share = float(cell.cost_usd / total_cost) if total_cost > 0 else 0.0
        rows.append(
            CostShare(
                key=label,
                cost_usd=cell.cost_usd,
                share=share,
                calls=cell.calls,
                video_seconds=cell.video_seconds,
            )
        )
    rows.sort(key=lambda r: r.cost_usd, reverse=True)
    return rows


def cost_breakdown(
    store: UsageMetricStore,
    *,
    since: datetime,
    until: datetime,
    where: Dimension | None = None,
    include_books: bool = True,
) -> CostBreakdown:
    """Total spend over the window split by provider, model, and (optionally) book."""
    agg = totals(store, since=since, until=until, where=where)
    total = agg.cost_usd
    return CostBreakdown(
        total_usd=total,
        by_provider=_shares(
            store, PROVIDER, since=since, until=until, where=where, total_cost=total
        ),
        by_model=_shares(store, MODEL, since=since, until=until, where=where, total_cost=total),
        by_book=(
            _shares(store, BOOK, since=since, until=until, where=where, total_cost=total)
            if include_books
            else []
        ),
    )


@dataclass(frozen=True, slots=True)
class UnitEconomics:
    """$/finished-minute-of-film, overall and per book.

    A "finished minute" is 60 accepted video-seconds. ``cost_per_finished_minute``
    is ``None`` when no video was produced (cost with zero output → undefined).
    """

    total_cost_usd: Decimal
    finished_minutes: float
    cost_per_finished_minute_usd: Decimal | None
    per_book: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cost_usd": str(self.total_cost_usd),
            "finished_minutes": round(self.finished_minutes, 4),
            "cost_per_finished_minute_usd": (
                None
                if self.cost_per_finished_minute_usd is None
                else str(self.cost_per_finished_minute_usd)
            ),
            "per_book": self.per_book,
        }


def _per_minute(cost: Decimal, video_seconds: float) -> Decimal | None:
    if video_seconds <= 0:
        return None
    minutes = Decimal(str(video_seconds)) / _SECONDS_PER_MINUTE
    if minutes <= 0:
        return None
    return (cost / minutes).quantize(Decimal("0.000001"))


def unit_economics(
    store: UsageMetricStore,
    *,
    since: datetime,
    until: datetime,
    where: Dimension | None = None,
) -> UnitEconomics:
    """Compute $/finished-minute overall and per book over the window."""
    agg = totals(store, since=since, until=until, where=where)
    total_minutes = agg.video_seconds / 60.0
    overall = _per_minute(agg.cost_usd, agg.video_seconds)

    table = grouped(store, axes=[BOOK], since=since, until=until, where=where)
    per_book: list[dict[str, Any]] = []
    for key, cell in table.items():
        label = key[0] if key else ""
        if not label:
            continue
        cpm = _per_minute(cell.cost_usd, cell.video_seconds)
        per_book.append(
            {
                "book_id": label,
                "cost_usd": str(cell.cost_usd),
                "finished_minutes": round(cell.video_seconds / 60.0, 4),
                "cost_per_finished_minute_usd": None if cpm is None else str(cpm),
            }
        )
    # Costliest film per finished minute first; books with no video sort last.
    per_book.sort(
        key=lambda r: (
            r["cost_per_finished_minute_usd"] is None,
            -float(r["cost_per_finished_minute_usd"] or 0),
        )
    )
    return UnitEconomics(
        total_cost_usd=agg.cost_usd,
        finished_minutes=total_minutes,
        cost_per_finished_minute_usd=overall,
        per_book=per_book,
    )


__all__ = [
    "CostBreakdown",
    "CostShare",
    "UnitEconomics",
    "cost_breakdown",
    "unit_economics",
]
