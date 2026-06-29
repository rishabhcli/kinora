"""Content-scraping / enumeration detection.

A scraper walks the catalogue: many *distinct* resource paths, fast, often with
a non-browser user-agent and suspiciously regular timing (a human browses
irregularly; a script ticks like a metronome). Kinora's books and generated
media are exactly the kind of content a competitor would scrape, so this matters.

Keyed by source ip over a sliding window, the detector combines three weak
signals into one score:

* **breadth** — distinct resource paths touched (the dominant signal);
* **cadence regularity** — low coefficient-of-variation of inter-request gaps
  (robot-like evenness); and
* **client suspicion** — a non-browser / scripted user-agent.

Breadth alone with enough volume is sufficient; the other two raise confidence
and severity. A reader paging through one book hits the same handful of paths and
never trips the breadth threshold.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from ..clock import Clock
from ..stats import clamp01
from ..types import Alert, EventKind, SecurityEvent, Severity, ThreatCategory, make_evidence
from ..windows import DistinctWindow, SlidingCounter
from .base import DetectorBase

_BROWSER_HINTS = ("mozilla", "chrome", "safari", "firefox", "edge", "webkit")
_BOT_HINTS = (
    "bot",
    "spider",
    "crawl",
    "scrap",
    "python-requests",
    "httpx",
    "curl",
    "wget",
    "go-http",
    "java/",
    "okhttp",
    "headless",
    "scrapy",
    "aiohttp",
)


def ua_suspicion(user_agent: str | None) -> float:
    """A ``0..1`` "this looks scripted" score from the user-agent string."""
    if not user_agent:
        return 0.8  # absent UA is itself suspicious for a content client
    ua = user_agent.lower()
    if any(h in ua for h in _BOT_HINTS):
        return 1.0
    if any(h in ua for h in _BROWSER_HINTS):
        return 0.0
    return 0.5  # unrecognised, neither obviously browser nor obviously bot


@dataclass(slots=True)
class ScrapingConfig:
    """Tuning for :class:`ScrapingDetector`."""

    window: float = 60.0
    distinct_path_threshold: int = 30
    """Distinct resource paths within the window to suspect enumeration."""
    min_requests: int = 30
    regularity_cv_threshold: float = 0.25
    """Inter-arrival coefficient-of-variation below which timing looks robotic."""
    distinct_cap: int = 16384
    state_ttl: float = 3600.0

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window must be positive")
        if self.distinct_path_threshold < 2:
            raise ValueError("distinct_path_threshold must be >= 2")


@dataclass(slots=True)
class _ClientState:
    paths: DistinctWindow
    requests: SlidingCounter
    last_ts: float | None = None
    gap_n: int = 0
    gap_mean: float = 0.0
    gap_m2: float = 0.0
    last_mono: float = 0.0

    def observe_gap(self, ts: float) -> None:
        if self.last_ts is not None:
            gap = ts - self.last_ts
            self.gap_n += 1
            delta = gap - self.gap_mean
            self.gap_mean += delta / self.gap_n
            self.gap_m2 += delta * (gap - self.gap_mean)
        self.last_ts = ts

    @property
    def gap_cv(self) -> float:
        if self.gap_n < 2 or self.gap_mean <= 1e-9:
            return math.inf
        var = self.gap_m2 / self.gap_n
        return math.sqrt(max(0.0, var)) / self.gap_mean


class ScrapingDetector(DetectorBase):
    """Detect catalogue enumeration by path breadth + cadence + client signals."""

    name = "scraping"
    kinds = frozenset({EventKind.ACCESS, EventKind.HTTP})

    def __init__(
        self,
        *,
        name: str = "scraping",
        config: ScrapingConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self.name = name
        self.config = config or ScrapingConfig()
        self._state: dict[str, _ClientState] = {}

    def _state_for(self, ip: str) -> _ClientState:
        st = self._state.get(ip)
        if st is None:
            st = _ClientState(
                paths=DistinctWindow(self.config.window, cap=self.config.distinct_cap),
                requests=SlidingCounter(self.config.window),
            )
            self._state[ip] = st
        return st

    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        ip = event.source_ip
        now = self.clock.mono()
        st = self._state_for(ip)
        st.last_mono = now
        st.observe_gap(event.ts)

        distinct = st.paths.add(event.target or "?", now)
        requests = st.requests.hit(now)
        cfg = self.config

        if requests < cfg.min_requests or distinct < cfg.distinct_path_threshold:
            return ()

        breadth = clamp01((distinct - cfg.distinct_path_threshold) / (cfg.distinct_path_threshold))
        regular = st.gap_cv <= cfg.regularity_cv_threshold
        suspicion = ua_suspicion(event.user_agent)

        score = clamp01(0.5 + 0.3 * breadth + (0.15 if regular else 0.0) + 0.1 * suspicion)
        return (
            Alert(
                detector=self.name,
                category=ThreatCategory.SCRAPING,
                severity=Severity.for_score(score),
                score=score,
                subject=ip,
                source_ip=ip,
                ts=event.ts,
                title=f"Content scraping from {ip}",
                description=(
                    f"{distinct} distinct paths in {cfg.window:.0f}s "
                    f"(cadence CV {st.gap_cv:.2f}, UA suspicion {suspicion:.1f})."
                ),
                evidence=make_evidence(
                    distinct_paths=distinct,
                    requests=requests,
                    cadence_cv=round(st.gap_cv, 3) if math.isfinite(st.gap_cv) else "inf",
                    regular_cadence=regular,
                    ua_suspicion=suspicion,
                    user_agent=event.user_agent or "",
                ),
                recommended_action="rate_limit_or_challenge",
                dedup_key=f"{self.name}:{ip}",
            ),
        )

    def sweep(self, now_mono: float) -> None:
        ttl = self.config.state_ttl
        stale = [ip for ip, st in self._state.items() if now_mono - st.last_mono > ttl]
        for ip in stale:
            del self._state[ip]


__all__ = ["ScrapingConfig", "ScrapingDetector", "ua_suspicion"]
