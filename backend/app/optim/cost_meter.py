"""USD cost metering on top of the physical ``providers.types.Usage`` spend units.

Kinora tracks spend in *physical* units only — tokens, images, audio-seconds, video-seconds —
and enforces a hard cap on video-seconds (``BudgetService``). There is no money anywhere. This
module adds a thin, **observe-only** money layer:

* :class:`Price` / :data:`PRICING` — a per-model USD price table. The defaults are *illustrative
  list prices* (DashScope/Qwen/Wan ids are past the training cutoff and vary by region); tune via
  ``Settings.optim_pricing_json``. Cost accuracy is exactly as good as this table — but the
  per-book / per-session / per-model *breakdown* and the routing-savings *percentage* are correct
  regardless of the absolute calibration.
* :func:`cost_of` — pure ``Usage -> Decimal`` USD. Unknown model ⇒ ``Decimal(0)`` (never raises).
* :class:`CostMeter` — a ``UsageSink`` (``Callable[[Usage], None]``) that accumulates cost
  rollups. It attaches via the designed ``create_providers(usage_sink=...)`` seam, so every
  provider call already funnels exactly one ``Usage`` through it. Per-book / per-session
  attribution comes from :func:`cost_context`, a ``ContextVar`` set around a unit of work.

Safe on every hot path: it never raises and never blocks (a cheap lock guards the in-memory
rollups so concurrent provider callbacks can't corrupt them).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.providers.types import Usage

logger = get_logger("app.optim.cost_meter")

#: Zero default for every price dimension, so a model that bills only one dimension (e.g. images)
#: needs only that field. Prices use ``Decimal(str(...))`` to avoid binary-float drift.
_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Price:
    """Per-model unit prices in USD (per 1k tokens, per image, per audio/video second)."""

    input_per_1k: Decimal = _ZERO
    output_per_1k: Decimal = _ZERO
    per_image: Decimal = _ZERO
    per_audio_second: Decimal = _ZERO
    per_video_second: Decimal = _ZERO


def _d(value: Any) -> Decimal:
    """Coerce a JSON scalar (str/int/float) to ``Decimal`` without float drift."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# --------------------------------------------------------------------------- #
# Default price table (ILLUSTRATIVE — tune via Settings.optim_pricing_json)
# --------------------------------------------------------------------------- #
# Relative ordering is what routing depends on: max > plus > adapter for chat; VL like a premium
# chat model; image per-image; tts per audio-second; Wan video per video-second (the budget-critical
# unit). Absolute USD values are placeholders — calibrate against your DashScope contract.
PRICING: dict[str, Price] = {
    # chat / text (USD per 1k tokens, input / output)
    "qwen3.7-max": Price(input_per_1k=_d("0.0024"), output_per_1k=_d("0.0096")),
    "qwen3.7-plus": Price(input_per_1k=_d("0.0008"), output_per_1k=_d("0.0020")),
    "qwen3.5-plus": Price(input_per_1k=_d("0.0005"), output_per_1k=_d("0.0015")),
    # vision-language (Critic)
    "qwen-vl-max": Price(input_per_1k=_d("0.0030"), output_per_1k=_d("0.0090")),
    # image generation / edit (USD per image)
    "qwen-image-2.0-pro": Price(per_image=_d("0.040")),
    "qwen-image-edit-max": Price(per_image=_d("0.040")),
    # text-to-speech / clone / asr (USD per audio-second)
    "qwen3-tts-flash": Price(per_audio_second=_d("0.0008")),
    "qwen3-tts-vc": Price(per_audio_second=_d("0.0008")),
    "qwen3-asr-flash": Price(per_audio_second=_d("0.0004")),
    # embeddings (USD per 1k input tokens)
    "tongyi-embedding-vision-plus": Price(input_per_1k=_d("0.00007")),
    # Wan video (USD per video-second) — config placeholders + the real intl ids (see CLAUDE.md)
    "wan2.7-t2v": Price(per_video_second=_d("0.10")),
    "wan2.7-i2v": Price(per_video_second=_d("0.12")),
    "wan2.7-r2v": Price(per_video_second=_d("0.12")),
    "wan2.5-t2v-preview": Price(per_video_second=_d("0.10")),
    "wan2.1-t2v-turbo": Price(per_video_second=_d("0.05")),
    "wan2.2-i2v-plus": Price(per_video_second=_d("0.12")),
    "wan2.1-i2v-turbo": Price(per_video_second=_d("0.06")),
}


def cost_of(usage: Usage, pricing: Mapping[str, Price] = PRICING) -> Decimal:
    """USD cost of a single :class:`~app.providers.types.Usage`. Unknown model ⇒ ``Decimal(0)``.

    Pure and total: never raises (a price-table miss is logged at debug and costed as zero, so a
    hot-path sink can never break a provider call).
    """
    price = pricing.get(usage.model)
    if price is None:
        logger.debug("cost_meter.unpriced_model", model=usage.model, operation=usage.operation)
        return _ZERO
    thousand = Decimal(1000)
    return (
        price.input_per_1k * Decimal(usage.input_tokens) / thousand
        + price.output_per_1k * Decimal(usage.output_tokens) / thousand
        + price.per_image * Decimal(usage.images)
        + price.per_audio_second * _d(usage.audio_seconds)
        + price.per_video_second * _d(usage.video_seconds)
    )


# --------------------------------------------------------------------------- #
# Per-book / per-session attribution context
# --------------------------------------------------------------------------- #
_ctx: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "kinora_cost_ctx", default=(None, None)
)


@contextmanager
def cost_context(*, book_id: str | None = None, session_id: str | None = None) -> Iterator[None]:
    """Attribute every ``Usage`` recorded inside the block to ``book_id`` / ``session_id``.

    No-op safe: unset ⇒ the meter only moves the global + per-model + per-operation totals.
    """
    token = _ctx.set((book_id, session_id))
    try:
        yield
    finally:
        _ctx.reset(token)


@dataclass
class CostRollup:
    """A mutable accumulator: physical units + their USD cost over some slice of calls."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    cost_usd: Decimal = field(default_factory=lambda: _ZERO)

    def add(self, usage: Usage, cost: Decimal) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.images += usage.images
        self.audio_seconds += usage.audio_seconds
        self.video_seconds += usage.video_seconds
        self.cost_usd += cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "images": self.images,
            "audio_seconds": round(self.audio_seconds, 3),
            "video_seconds": round(self.video_seconds, 3),
            "cost_usd": str(self.cost_usd),
        }


class CostMeter:
    """A ``UsageSink`` that rolls USD cost up per model / operation / book / session.

    Wire via ``create_providers(usage_sink=CostMeter.from_settings(settings))``. Observe-only: it
    adds no latency of consequence and never raises, so it is safe to leave attached in production.
    """

    def __init__(self, pricing: Mapping[str, Price] = PRICING) -> None:
        self._pricing = pricing
        self._lock = threading.Lock()
        self._total = CostRollup()
        self._by_model: dict[str, CostRollup] = {}
        self._by_operation: dict[str, CostRollup] = {}
        self._by_book: dict[str, CostRollup] = {}
        self._by_session: dict[str, CostRollup] = {}

    def __call__(self, usage: Usage) -> None:
        cost = cost_of(usage, self._pricing)
        book_id, session_id = _ctx.get()
        with self._lock:
            self._total.add(usage, cost)
            self._by_model.setdefault(usage.model, CostRollup()).add(usage, cost)
            self._by_operation.setdefault(usage.operation, CostRollup()).add(usage, cost)
            if book_id:
                self._by_book.setdefault(book_id, CostRollup()).add(usage, cost)
            if session_id:
                self._by_session.setdefault(session_id, CostRollup()).add(usage, cost)

    def reset(self) -> None:
        with self._lock:
            self._total = CostRollup()
            self._by_model.clear()
            self._by_operation.clear()
            self._by_book.clear()
            self._by_session.clear()

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable rollup (``cost_usd`` rendered as a decimal string)."""
        with self._lock:
            return {
                "total": self._total.as_dict(),
                "by_model": {k: v.as_dict() for k, v in self._by_model.items()},
                "by_operation": {k: v.as_dict() for k, v in self._by_operation.items()},
                "by_book": {k: v.as_dict() for k, v in self._by_book.items()},
                "by_session": {k: v.as_dict() for k, v in self._by_session.items()},
            }

    @classmethod
    def from_settings(cls, settings: Any) -> CostMeter:
        """Build a meter, layering ``settings.optim_pricing_json`` (if any) over :data:`PRICING`."""
        raw = getattr(settings, "optim_pricing_json", None)
        if not raw:
            return cls(pricing=PRICING)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("cost_meter.bad_pricing_json", chars=len(str(raw)))
            return cls(pricing=PRICING)
        table = dict(PRICING)
        for model, fields in parsed.items():
            if isinstance(fields, Mapping):
                table[model] = Price(
                    input_per_1k=_d(fields.get("input_per_1k", 0)),
                    output_per_1k=_d(fields.get("output_per_1k", 0)),
                    per_image=_d(fields.get("per_image", 0)),
                    per_audio_second=_d(fields.get("per_audio_second", 0)),
                    per_video_second=_d(fields.get("per_video_second", 0)),
                )
        return cls(pricing=table)


__all__ = ["PRICING", "CostMeter", "CostRollup", "Price", "cost_context", "cost_of"]
