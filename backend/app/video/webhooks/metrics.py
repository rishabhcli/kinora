"""In-process counters for the callback ingress (kinora.md §12.5 observability).

A webhook gateway is exactly the kind of "unglamorous 30%" surface where you want
numbers: how many callbacks arrived, how many were forged, how many were stale
replays, how many were duplicates collapsed by the idempotency layer, how many
were for unknown tasks. These feed the §13 demo metrics panel and post-hoc
debugging.

Deliberately a tiny dependency-free counter (not Prometheus) so it has no infra
requirement and is trivially assertable in tests; a real metrics exporter can
read :meth:`snapshot` on a scrape. Counting is process-local and lock-free — fine
on the single event loop the API runs on.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class IngressMetrics:
    """Monotonic counters describing the gateway's callback ingestion."""

    #: Total bodies received (before any verification).
    received: int = 0
    #: Verified, parsed, and handed to the sink for the first time.
    accepted: int = 0
    #: Collapsed by idempotency (a duplicate at-least-once delivery).
    duplicates: int = 0
    #: Rejected: bad/missing signature.
    bad_signature: int = 0
    #: Rejected: signed timestamp outside the replay window.
    replays: int = 0
    #: Rejected: body too large for the ingress guard.
    too_large: int = 0
    #: Rejected: body verified but unparseable (4xx).
    malformed: int = 0
    #: Rejected: unknown provider slug.
    unknown_provider: int = 0
    #: Accepted callbacks whose canonical status was UNKNOWN (tolerated).
    unknown_status: int = 0
    #: Per-provider accepted counts, for a quick "which providers are live" view.
    by_provider: Counter[str] = field(default_factory=Counter)

    def snapshot(self) -> dict[str, int | dict[str, int]]:
        """A JSON-serialisable view for a metrics scrape / the demo panel."""
        return {
            "received": self.received,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "bad_signature": self.bad_signature,
            "replays": self.replays,
            "too_large": self.too_large,
            "malformed": self.malformed,
            "unknown_provider": self.unknown_provider,
            "unknown_status": self.unknown_status,
            "by_provider": dict(self.by_provider),
        }


__all__ = ["IngressMetrics"]
