"""Theme & branding — the palette + type scale a report renders in.

A :class:`Brand` is the single knob that white-labels a document. Every renderer
(HTML + PDF) and the chart engine resolve their colors and font sizes through
the brand, so the *same* :class:`~app.reports.model.Report` content can render in
Kinora's house style or a partner's without touching the builders.

Colors are plain ``#rrggbb`` hex strings (HTML uses them verbatim; the PDF
renderer parses them to PyMuPDF ``(r,g,b)`` floats via :func:`hex_to_rgb`). The
default brand is Kinora's dark, cinematic palette.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    """Parse ``#rrggbb`` (or ``#rgb``) to a 0–1 ``(r, g, b)`` triple.

    Tolerant of a missing leading ``#`` and of the 3-digit shorthand. Used by the
    PyMuPDF renderer, which wants float color components.

    Raises:
        ValueError: if the string is not a 3- or 6-hex-digit color.
    """
    s = value.lstrip("#").strip()
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"not a hex color: {value!r}")
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError as exc:  # pragma: no cover - re-raise with context
        raise ValueError(f"not a hex color: {value!r}") from exc
    return (r / 255.0, g / 255.0, b / 255.0)


def mix(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colors, ``t`` in ``[0,1]`` (0 == a, 1 == b)."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = hex_to_rgb(a)
    br, bg, bb = hex_to_rgb(b)
    r = round((ar + (br - ar) * t) * 255)
    g = round((ag + (bg - ag) * t) * 255)
    bl = round((ab + (bb - ab) * t) * 255)
    return f"#{r:02x}{g:02x}{bl:02x}"


@dataclass(frozen=True, slots=True)
class Palette:
    """The named colors a report draws with."""

    background: str = "#0d1117"
    surface: str = "#161b22"
    surface_alt: str = "#1c2330"
    border: str = "#2a3242"
    text: str = "#e6edf3"
    text_muted: str = "#9da7b3"
    heading: str = "#ffffff"
    accent: str = "#7c6cff"
    accent_soft: str = "#a99bff"
    info: str = "#4aa3ff"
    success: str = "#3fb950"
    warning: str = "#d29922"
    danger: str = "#f85149"

    #: An ordered categorical cycle for multi-series charts.
    series: tuple[str, ...] = (
        "#7c6cff",
        "#4aa3ff",
        "#3fb950",
        "#d29922",
        "#f85149",
        "#bc8cff",
        "#39d0d8",
    )

    def series_color(self, i: int) -> str:
        """The categorical color at index ``i`` (cycles)."""
        return self.series[i % len(self.series)]

    def tone(self, name: str) -> str:
        """Resolve a tone name (info/success/warning/danger/accent/neutral)."""
        return {
            "info": self.info,
            "success": self.success,
            "warning": self.warning,
            "danger": self.danger,
            "accent": self.accent,
            "neutral": self.text_muted,
        }.get(name, self.text_muted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "background": self.background,
            "surface": self.surface,
            "surface_alt": self.surface_alt,
            "border": self.border,
            "text": self.text,
            "text_muted": self.text_muted,
            "heading": self.heading,
            "accent": self.accent,
            "accent_soft": self.accent_soft,
            "info": self.info,
            "success": self.success,
            "warning": self.warning,
            "danger": self.danger,
            "series": list(self.series),
        }


#: A light palette for print-friendly / paper reports (certificates).
LIGHT_PALETTE = Palette(
    background="#ffffff",
    surface="#f7f8fa",
    surface_alt="#eef1f5",
    border="#d8dee6",
    text="#1b2330",
    text_muted="#5b6573",
    heading="#10141c",
    accent="#5b46e0",
    accent_soft="#7c6cff",
    info="#1f6feb",
    success="#1a7f37",
    warning="#9a6700",
    danger="#cf222e",
)


@dataclass(frozen=True, slots=True)
class TypeScale:
    """Font sizes (points) for the document's type hierarchy."""

    title: float = 30.0
    subtitle: float = 15.0
    h1: float = 20.0
    h2: float = 16.0
    h3: float = 13.0
    h4: float = 11.5
    body: float = 10.5
    small: float = 9.0
    stat: float = 24.0
    stat_label: float = 8.5

    def heading_size(self, level: int) -> float:
        """Map a heading level (1–4) to a point size."""
        return {1: self.h1, 2: self.h2, 3: self.h3, 4: self.h4}.get(level, self.h2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "h1": self.h1,
            "h2": self.h2,
            "h3": self.h3,
            "h4": self.h4,
            "body": self.body,
            "small": self.small,
            "stat": self.stat,
            "stat_label": self.stat_label,
        }


@dataclass(frozen=True, slots=True)
class Brand:
    """The full branding bundle a report renders in."""

    name: str = "Kinora"
    tagline: str = "watch the book"
    palette: Palette = field(default_factory=Palette)
    type_scale: TypeScale = field(default_factory=TypeScale)
    #: Font family stack for HTML; the PDF uses a built-in helv/Times by weight.
    font_family: str = (
        "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, "
        "Helvetica, Arial, sans-serif"
    )
    #: Monospace stack for code-ish values (ids, hashes).
    mono_family: str = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    #: Optional inline SVG mark (no external asset → self-contained output).
    logo_svg: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tagline": self.tagline,
            "palette": self.palette.to_dict(),
            "type_scale": self.type_scale.to_dict(),
            "font_family": self.font_family,
            "mono_family": self.mono_family,
        }


#: The bundled Kinora mark — a film-reel ring around a play triangle.
_KINORA_MARK = (
    '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" '
    'width="40" height="40" aria-hidden="true">'
    '<circle cx="24" cy="24" r="21" fill="none" stroke="currentColor" '
    'stroke-width="2.5"/>'
    '<circle cx="24" cy="7.5" r="2.4" fill="currentColor"/>'
    '<circle cx="40.5" cy="24" r="2.4" fill="currentColor"/>'
    '<circle cx="24" cy="40.5" r="2.4" fill="currentColor"/>'
    '<circle cx="7.5" cy="24" r="2.4" fill="currentColor"/>'
    '<path d="M19 16 L34 24 L19 32 Z" fill="currentColor"/>'
    "</svg>"
)


def default_brand() -> Brand:
    """Kinora's house brand (dark cinematic palette + the reel mark)."""
    return Brand(logo_svg=_KINORA_MARK)


def certificate_brand() -> Brand:
    """A light, print-friendly brand for completion certificates."""
    return Brand(palette=LIGHT_PALETTE, logo_svg=_KINORA_MARK)


__all__ = [
    "LIGHT_PALETTE",
    "Brand",
    "Palette",
    "TypeScale",
    "certificate_brand",
    "default_brand",
    "hex_to_rgb",
    "mix",
]
