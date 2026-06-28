"""Number / duration / date formatting helpers shared by every builder.

Builders format **once** through these helpers so the rendered string lands in
the model (``Stat.display`` / table cells) and every renderer — HTML, PDF, CSV —
agrees byte-for-byte. Keeping formatting out of the renderers is what makes the
golden-file tests stable.
"""

from __future__ import annotations

from datetime import UTC, datetime


def fmt_int(value: float) -> str:
    """Thousands-grouped integer: ``12345`` → ``"12,345"``."""
    return f"{int(round(value)):,}"


def fmt_float(value: float, places: int = 1) -> str:
    """Fixed-places float with thousands grouping: ``1234.5`` → ``"1,234.5"``."""
    return f"{value:,.{places}f}"


def fmt_pct(fraction: float, places: int = 1) -> str:
    """A 0–1 fraction as a percentage string: ``0.873`` → ``"87.3%"``."""
    return f"{fraction * 100:.{places}f}%"


def fmt_pct_value(value: float, places: int = 1) -> str:
    """An already-0–100 value as a percentage string: ``87.3`` → ``"87.3%"``."""
    return f"{value:.{places}f}%"


def fmt_seconds(seconds: float) -> str:
    """A duration in seconds → a compact human string.

    ``42`` → ``"42s"``, ``95`` → ``"1m 35s"``, ``3725`` → ``"1h 2m"``.
    """
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{m}m {rem}s" if rem else f"{m}m"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h {m}m" if m else f"{h}h"


def fmt_minutes(seconds: float) -> str:
    """Seconds → decimal minutes string: ``150`` → ``"2.5 min"``."""
    return f"{seconds / 60.0:.1f} min"


def fmt_duration_clock(seconds: float) -> str:
    """Seconds → ``H:MM:SS`` / ``M:SS`` clock form (for reading-time displays)."""
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_date(dt: datetime) -> str:
    """A datetime → ``"28 Jun 2026"`` (UTC, locale-independent)."""
    d = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return d.strftime("%d %b %Y")


def fmt_datetime(dt: datetime) -> str:
    """A datetime → ``"28 Jun 2026 14:05 UTC"``."""
    d = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return d.strftime("%d %b %Y %H:%M UTC")


def fmt_iso(dt: datetime) -> str:
    """A datetime → a stable ISO-8601 UTC string with a ``Z`` suffix."""
    d = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return d.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ordinal(n: int) -> str:
    """English ordinal: ``1`` → ``"1st"``, ``22`` → ``"22nd"``."""
    suffix = "th" if 10 <= (n % 100) <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """``"1 book"`` / ``"3 books"`` — count + the right noun form."""
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count:,} {word}"


__all__ = [
    "fmt_date",
    "fmt_datetime",
    "fmt_duration_clock",
    "fmt_float",
    "fmt_int",
    "fmt_iso",
    "fmt_minutes",
    "fmt_pct",
    "fmt_pct_value",
    "fmt_seconds",
    "ordinal",
    "pluralize",
]
