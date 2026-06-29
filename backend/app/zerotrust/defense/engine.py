"""The streaming threat-detection engine.

:class:`ThreatEngine` is the composition root of the runtime-defense side. It
owns a list of :class:`~app.zerotrust.defense.detectors.base.Detector`\\ s, fans
each event out to the interested ones, runs every produced alert through the
:class:`~app.zerotrust.defense.alerting.Deduper`, and writes survivors to the
injected :class:`~app.zerotrust.defense.store.AlertSink`. It also drives a
periodic sweep that drops idle per-key detector state to bound memory.

Everything is synchronous and clock-driven, so an entire synthetic attack trace
can be replayed through :meth:`ingest` and the resulting alerts asserted exactly.
The engine performs no I/O beyond the sink call.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .alerting import DedupConfig, Deduper
from .clock import Clock, SystemClock
from .detectors.base import Detector
from .store import AlertSink, InMemoryAlertStore
from .types import Alert, SecurityEvent


@dataclass(slots=True)
class EngineStats:
    """Running counters for observability / the §13 metrics panel."""

    events: int = 0
    raw_alerts: int = 0
    emitted_alerts: int = 0
    suppressed_alerts: int = 0
    sweeps: int = 0
    by_detector: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "events": self.events,
            "raw_alerts": self.raw_alerts,
            "emitted_alerts": self.emitted_alerts,
            "suppressed_alerts": self.suppressed_alerts,
            "sweeps": self.sweeps,
            "by_detector": dict(self.by_detector),
        }


class ThreatEngine:
    """Fan events out to detectors, dedupe alerts, persist via the sink."""

    def __init__(
        self,
        detectors: Iterable[Detector] | None = None,
        *,
        sink: AlertSink | None = None,
        deduper: Deduper | None = None,
        clock: Clock | None = None,
        sweep_interval: float = 60.0,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.sink: AlertSink = sink if sink is not None else InMemoryAlertStore()
        self.deduper = deduper or Deduper(DedupConfig())
        self.detectors: list[Detector] = list(detectors or ())
        self.stats = EngineStats()
        self._sweep_interval = sweep_interval
        self._last_sweep = self.clock.mono()

    def register(self, detector: Detector) -> ThreatEngine:
        """Add a detector (additive — order is not significant)."""
        self.detectors.append(detector)
        return self

    def ingest(self, event: SecurityEvent) -> list[Alert]:
        """Process one event; return the alerts actually emitted (post-dedup)."""
        self.stats.events += 1
        emitted: list[Alert] = []
        for det in self.detectors:
            if not det.consumes(event):
                continue
            for raw in det.observe(event):
                self.stats.raw_alerts += 1
                self.stats.by_detector[raw.detector] = (
                    self.stats.by_detector.get(raw.detector, 0) + 1
                )
                kept = self.deduper.admit(raw, now=self.clock.mono())
                if kept is None:
                    self.stats.suppressed_alerts += 1
                    continue
                self.sink.record(kept)
                self.stats.emitted_alerts += 1
                emitted.append(kept)
        self._maybe_sweep()
        return emitted

    def ingest_all(self, events: Iterable[SecurityEvent]) -> list[Alert]:
        """Replay a whole trace; return every emitted alert in order."""
        out: list[Alert] = []
        for ev in events:
            out.extend(self.ingest(ev))
        return out

    def _maybe_sweep(self) -> None:
        now = self.clock.mono()
        if now - self._last_sweep < self._sweep_interval:
            return
        for det in self.detectors:
            det.sweep(now)
        self._last_sweep = now
        self.stats.sweeps += 1

    def force_sweep(self) -> None:
        """Sweep every detector now (test / shutdown hook)."""
        now = self.clock.mono()
        for det in self.detectors:
            det.sweep(now)
        self._last_sweep = now
        self.stats.sweeps += 1


__all__ = ["EngineStats", "ThreatEngine"]
