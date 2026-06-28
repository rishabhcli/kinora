"""Pure value-formatting helpers shared by the CLI renderers.

Deliberately dependency-free and side-effect-free so they are trivially unit
tested. These produce the *human* strings the table renderer shows; the JSON
renderer uses the raw values from each result's payload instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

_TIME_UNITS: tuple[tuple[str, float], ...] = (
    ("d", 86_400.0),
    ("h", 3_600.0),
    ("m", 60.0),
    ("s", 1.0),
)


def humanize_seconds(seconds: float | int | None, *, places: int = 1) -> str:
    """Render a video-seconds / duration value compactly.

    ``95.0 -> "1m35s"``, ``5.0 -> "5s"``, ``3725 -> "1h2m5s"``. ``None`` renders
    as ``"-"``. Sub-second residue is dropped once we are above a minute; below a
    minute we keep ``places`` decimals so a 4.5s shot reads as ``4.5s``.
    """
    if seconds is None:
        return "-"
    total = float(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    if total < 60.0:
        text = f"{total:.{places}f}".rstrip("0").rstrip(".")
        return f"{sign}{text}s"
    parts: list[str] = []
    remaining = int(round(total))
    for label, unit in _TIME_UNITS:
        unit_i = int(unit)
        if remaining >= unit_i:
            value, remaining = divmod(remaining, unit_i)
            parts.append(f"{value}{label}")
    return sign + "".join(parts) if parts else f"{sign}0s"


def humanize_bytes(num: int | float | None) -> str:
    """Render a byte count as a binary-prefixed string (``1536 -> "1.5KiB"``)."""
    if num is None:
        return "-"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}TiB"  # pragma: no cover - unreachable, loop returns


def pct(numerator: float, denominator: float, *, places: int = 1) -> str:
    """Format a ratio as a percentage string; a zero denominator renders ``"-"``."""
    if denominator == 0:
        return "-"
    return f"{(numerator / denominator) * 100:.{places}f}%"


def ago(when: datetime | None, *, now: datetime | None = None) -> str:
    """Render a timestamp as a coarse "time ago" string (``"3m ago"``)."""
    if when is None:
        return "-"
    reference = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    delta = (reference - when).total_seconds()
    if delta < 0:
        return "in " + humanize_seconds(-delta, places=0)
    if delta < 1:
        return "just now"
    return humanize_seconds(delta, places=0) + " ago"


def truncate(value: object, width: int = 48) -> str:
    """Stringify and ellipsize ``value`` to at most ``width`` characters."""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def yesno(flag: object) -> str:
    """Render a boolean-ish value as ``yes``/``no``."""
    return "yes" if bool(flag) else "no"


def isoformat(when: datetime | None) -> str | None:
    """ISO-8601 (UTC) string for JSON payloads, or ``None``."""
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


__all__ = [
    "ago",
    "humanize_bytes",
    "humanize_seconds",
    "isoformat",
    "pct",
    "truncate",
    "yesno",
]
