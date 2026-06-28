"""Batching + cost accounting for translation.

Two concerns:

* **Batching.** Sending one provider call per segment is wasteful (per-call
  overhead, rate-limit pressure). The :func:`batch_requests` helper packs masked
  requests into batches bounded by both a count and an estimated-token budget, so
  one call carries many segments without exceeding a model's context window.
  Batches are *stable* (input order preserved) so the 1:1 output mapping holds.

* **Cost accounting.** Every provider call returns a
  :class:`~app.translation.types.TranslationCost`; :class:`CostLedger` accumulates
  them, broken down by target language and content kind, and exposes a token
  estimate so a caller can predict spend before committing. Cache hits are tracked
  separately — they are the §8.7 win and the headline metric for "re-reading is
  free."
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .provider import ProviderRequest
from .types import ContentKind, TranslationCost


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token, min 1).

    Used for batch packing and a pre-flight spend prediction. Intentionally
    transport-agnostic — the real provider reports authoritative usage, this is
    only a planning estimate.
    """
    return max(1, (len(text) + 3) // 4)


def batch_requests(
    requests: list[ProviderRequest],
    *,
    max_batch_size: int = 32,
    max_batch_tokens: int = 6000,
) -> list[list[ProviderRequest]]:
    """Pack requests into batches bounded by count and estimated tokens.

    A single request larger than ``max_batch_tokens`` still ships alone (we never
    drop content). Order is preserved across and within batches so the caller can
    flatten the responses back 1:1.
    """
    batches: list[list[ProviderRequest]] = []
    current: list[ProviderRequest] = []
    current_tokens = 0
    for req in requests:
        tokens = estimate_tokens(req.masked_text) + (
            estimate_tokens(req.context) if req.context else 0
        )
        too_many = len(current) >= max_batch_size
        too_big = current and (current_tokens + tokens) > max_batch_tokens
        if too_many or too_big:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(req)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


@dataclass
class CostLedger:
    """Accumulates translation spend, broken down for telemetry.

    Mirrors the provider layer's :class:`~app.providers.types.UsageTotals` so a
    future budget cross-walk is mechanical. ``video_seconds`` stays 0 here.
    """

    total: TranslationCost = field(default_factory=TranslationCost)
    by_language: dict[str, TranslationCost] = field(default_factory=dict)
    by_kind: dict[str, TranslationCost] = field(default_factory=dict)
    _provider_calls: int = 0

    def record(
        self,
        cost: TranslationCost,
        *,
        target_lang: str,
        content_kind: ContentKind | str = ContentKind.PAGE_TEXT,
    ) -> None:
        """Fold a provider call's cost into the running totals + breakdowns."""
        self.total = self.total.merge(cost)
        self.by_language[target_lang] = self.by_language.get(
            target_lang, TranslationCost()
        ).merge(cost)
        kind = content_kind.value if isinstance(content_kind, ContentKind) else str(content_kind)
        self.by_kind[kind] = self.by_kind.get(kind, TranslationCost()).merge(cost)

    def record_cache_hit(
        self, *, target_lang: str, content_kind: ContentKind | str = ContentKind.PAGE_TEXT
    ) -> None:
        """Count a zero-cost cache hit (the §8.7 win)."""
        hit = TranslationCost(cache_hits=1, segments=1)
        self.record(hit, target_lang=target_lang, content_kind=content_kind)

    @property
    def cache_hit_rate(self) -> float:
        return self.total.cache_hit_rate

    def summary(self) -> dict[str, object]:
        """A JSON-safe spend summary (for the API / logs)."""
        return {
            "input_tokens": self.total.input_tokens,
            "output_tokens": self.total.output_tokens,
            "total_tokens": self.total.total_tokens,
            "provider_calls": self.total.provider_calls,
            "segments": self.total.segments,
            "cache_hits": self.total.cache_hits,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "by_language": {
                lang: {"total_tokens": c.total_tokens, "cache_hits": c.cache_hits}
                for lang, c in self.by_language.items()
            },
            "by_kind": {
                kind: {"total_tokens": c.total_tokens, "segments": c.segments}
                for kind, c in self.by_kind.items()
            },
        }


def predict_cost(requests: list[ProviderRequest]) -> TranslationCost:
    """Predict the cost of translating a request set (no provider call).

    Output tokens are estimated as the input scaled by a conservative 1.3x
    expansion (the worst common case among the registry's length ratios).
    """
    in_tokens = sum(estimate_tokens(r.masked_text) for r in requests)
    by_lang: dict[str, int] = defaultdict(int)
    for r in requests:
        by_lang[r.target_lang] += estimate_tokens(r.masked_text)
    out_tokens = int(in_tokens * 1.3)
    return TranslationCost(
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        provider_calls=len(batch_requests(requests)),
        segments=len(requests),
    )


__all__ = ["CostLedger", "batch_requests", "estimate_tokens", "predict_cost"]
