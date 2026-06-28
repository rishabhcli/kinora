"""Capacity planning — readers → render demand → workers → budget (kinora.md §4/§11).

The generation-on-scroll thesis (§4.1) rests on an asymmetry: a reader *consumes*
only ~0.15–0.30 video-seconds per wall-clock second, far below 1×, because a page
of video (~10s) is dwelt on for ~45–90s. This module turns that asymmetry into
the numbers an operator needs:

* **Render demand** — how many shot-renders/second a population of ``N`` concurrent
  readers generates (Little's law against reading pace + the §4.2 shot spacing).
* **Worker sizing** — an M/M/c queueing estimate: given a per-shot render latency
  and the §4.9 committed-slot count, what utilisation and queue wait does the
  population produce, and is it stable (``utilisation < 1``)?
* **Budget runway** — how long the §11 1,650-second video budget lasts under that
  population, and the max concurrency the budget sustains for a target session
  length.
* **Watermark feasibility** — whether ``c`` workers can keep the §4.5 committed
  buffer above ``L`` for the fastest reader (the buffer-health guarantee, §13).

All pure arithmetic. The unit tests pin the Little's-law identities, the M/M/c
formulas against textbook values, and the watermark-feasibility verdict against
the §4.10 worked example.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Reader → render demand (Little's law)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReadingProfile:
    """The per-reader consumption profile (kinora.md §4.1/§4.2).

    Defaults are the §4.1 midpoints: ~4 wps reading (240 wpm), a shot every ~30
    words of ~5 video-seconds, so a reader consumes ~0.67 shots/s of *content*
    but only when actively reading. ``active_fraction`` discounts idle/think time
    (§4.7) — a reader is not generating during pauses.
    """

    velocity_wps: float = 4.0
    words_per_shot: float = 30.0
    seconds_per_shot: float = 5.0
    active_fraction: float = 0.7  # fraction of wall-clock spent actively reading

    @property
    def shots_per_second(self) -> float:
        """Shots a single active reader consumes per wall-clock second."""
        return self.velocity_wps / max(1e-9, self.words_per_shot)

    @property
    def video_seconds_per_wallclock(self) -> float:
        """§4.1 consumption rate: video-seconds consumed per wall-clock second.

        ``shots/s × seconds-of-video/shot``, discounted by the active fraction —
        this is the 0.15–0.30 number the whole architecture turns on.
        """
        return self.shots_per_second * self.seconds_per_shot * self.active_fraction


@dataclass(frozen=True, slots=True)
class RenderDemand:
    """The aggregate render demand a reader population offers (Little's law)."""

    readers: int
    profile: ReadingProfile

    @property
    def arrival_rate_shots_per_s(self) -> float:
        """λ — shot-render arrivals/second across all active readers.

        Little's law in the offered-load direction: each active reader offers
        ``shots_per_second`` (discounted by active fraction); the population
        offers ``N × that``. Cache hits on re-reads (§8.7) would reduce this; this
        is the pessimistic, no-cache offered rate.
        """
        return self.readers * self.profile.shots_per_second * self.profile.active_fraction

    @property
    def offered_video_seconds_per_s(self) -> float:
        """Aggregate video-seconds/second the population would commit (no cache)."""
        return self.readers * self.profile.video_seconds_per_wallclock


# --------------------------------------------------------------------------- #
# Worker sizing — M/M/c queueing
# --------------------------------------------------------------------------- #


def erlang_c(servers: int, offered_load_erlangs: float) -> float:
    """The Erlang-C probability that an arriving job must wait (queues).

    ``offered_load_erlangs`` is ``a = λ / μ`` (arrival rate × mean service time).
    Returns the probability a job finds all ``c`` servers busy and waits. Requires
    ``a < c`` for a stable queue; at/above ``c`` the wait probability is 1.0.
    """
    if servers <= 0:
        raise ValueError("servers must be positive")
    a = offered_load_erlangs
    if a <= 0.0:
        return 0.0
    if a >= servers:
        return 1.0
    # Numerically stable Erlang-B recursion, then B → C.
    inv_b = 1.0
    for n in range(1, servers + 1):
        inv_b = 1.0 + (n / a) * inv_b
    erlang_b = 1.0 / inv_b
    rho = a / servers
    return erlang_b / (1.0 - rho * (1.0 - erlang_b))


@dataclass(frozen=True, slots=True)
class QueueingResult:
    """An M/M/c estimate for the committed render lane (§4.9/§12.2)."""

    servers: int
    arrival_rate_per_s: float
    service_time_s: float
    offered_load_erlangs: float
    utilisation: float
    wait_probability: float
    #: Mean time a job waits in queue before a worker starts it (seconds).
    mean_wait_s: float
    #: Mean total time in system (wait + service).
    mean_response_s: float
    stable: bool

    def to_dict(self) -> dict[str, float | int | bool]:
        """JSON projection of the estimate."""
        return {
            "servers": self.servers,
            "arrival_rate_per_s": round(self.arrival_rate_per_s, 5),
            "service_time_s": round(self.service_time_s, 3),
            "offered_load_erlangs": round(self.offered_load_erlangs, 4),
            "utilisation": round(self.utilisation, 4),
            "wait_probability": round(self.wait_probability, 4),
            "mean_wait_s": round(self.mean_wait_s, 3),
            "mean_response_s": round(self.mean_response_s, 3),
            "stable": self.stable,
        }


def mmc_queue(
    *, arrival_rate_per_s: float, service_time_s: float, servers: int
) -> QueueingResult:
    """Estimate an M/M/c queue for ``servers`` render workers (§4.9/§12.2).

    ``arrival_rate_per_s`` (λ) is the offered shot-render rate; ``service_time_s``
    (1/μ) is the mean per-shot render wall-clock. Returns utilisation, the
    Erlang-C wait probability, and the mean wait/response — the inputs to "do we
    need a 5th committed slot?". Beyond saturation (``utilisation >= 1``) the
    queue is unstable and the wait is reported as infinite.
    """
    if servers <= 0:
        raise ValueError("servers must be positive")
    if service_time_s < 0.0:
        raise ValueError("service_time_s must be non-negative")
    mu = 0.0 if service_time_s == 0.0 else 1.0 / service_time_s
    offered = arrival_rate_per_s * service_time_s  # a = λ/μ
    utilisation = offered / servers if servers else math.inf
    stable = utilisation < 1.0 and mu > 0.0
    if not stable:
        return QueueingResult(
            servers=servers,
            arrival_rate_per_s=arrival_rate_per_s,
            service_time_s=service_time_s,
            offered_load_erlangs=offered,
            utilisation=utilisation,
            wait_probability=1.0,
            mean_wait_s=math.inf,
            mean_response_s=math.inf,
            stable=False,
        )
    pw = erlang_c(servers, offered)
    # Mean wait Wq = C / (c·μ − λ); mean response = Wq + service time.
    denom = servers * mu - arrival_rate_per_s
    mean_wait = pw / denom if denom > 0 else math.inf
    return QueueingResult(
        servers=servers,
        arrival_rate_per_s=arrival_rate_per_s,
        service_time_s=service_time_s,
        offered_load_erlangs=offered,
        utilisation=utilisation,
        wait_probability=pw,
        mean_wait_s=mean_wait,
        mean_response_s=mean_wait + service_time_s,
        stable=True,
    )


def min_servers_for_utilisation(
    *, arrival_rate_per_s: float, service_time_s: float, max_utilisation: float = 0.8
) -> int:
    """Smallest server count keeping utilisation at/under ``max_utilisation``.

    Sizing rule of thumb: keep the committed lane under ~80% so the Erlang-C wait
    stays small and a burst (§4.5) doesn't tip it into a stall.
    """
    if not 0.0 < max_utilisation < 1.0:
        raise ValueError("max_utilisation must be in (0, 1)")
    offered = arrival_rate_per_s * service_time_s
    if offered <= 0.0:
        return 1
    return max(1, math.ceil(offered / max_utilisation))


# --------------------------------------------------------------------------- #
# Budget runway (§11)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BudgetRunway:
    """How long the §11 video budget lasts under a reader population."""

    ceiling_video_s: float
    burn_rate_video_s_per_s: float
    #: Cache hit ratio on re-reads/dedup (§8.7/§12.3) discounts the burn.
    cache_hit_ratio: float = 0.0

    @property
    def effective_burn_per_s(self) -> float:
        """Video-seconds/second actually spent after cache/dedup savings."""
        return self.burn_rate_video_s_per_s * (1.0 - self.cache_hit_ratio)

    @property
    def runway_seconds(self) -> float:
        """Wall-clock seconds until the budget is exhausted (``inf`` if no burn)."""
        burn = self.effective_burn_per_s
        if burn <= 0.0:
            return math.inf
        return self.ceiling_video_s / burn

    def reader_seconds(self, readers: int) -> float:
        """Total reader-seconds of viewing the budget funds for ``readers`` readers."""
        if readers <= 0:
            return math.inf
        return self.runway_seconds * readers


def max_concurrent_readers(
    *,
    ceiling_video_s: float,
    profile: ReadingProfile,
    target_session_s: float,
    cache_hit_ratio: float = 0.0,
) -> int:
    """Max readers the budget sustains for ``target_session_s`` each (§11).

    Each reader burns ``video_seconds_per_wallclock × target_session_s`` (less
    cache savings). The budget divides into that per-reader cost; the floor is the
    sustainable concurrent population for a session of that length.
    """
    per_reader = (
        profile.video_seconds_per_wallclock
        * target_session_s
        * (1.0 - cache_hit_ratio)
    )
    if per_reader <= 0.0:
        return 0
    return int(ceiling_video_s // per_reader)


# --------------------------------------------------------------------------- #
# Watermark feasibility (§4.5/§4.10) — can c workers hold the buffer above L?
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WatermarkFeasibility:
    """Whether the render fleet can keep the committed buffer above ``L`` (§4.5)."""

    #: Video-seconds produced per wall-clock second by the committed fleet.
    production_video_s_per_s: float
    #: Video-seconds consumed per wall-clock second by one reader (§4.1).
    consumption_video_s_per_s: float
    feasible: bool
    #: Headroom ratio = production / consumption (>= 1 means sustainable).
    headroom_ratio: float
    #: Time to fill from empty to the high watermark H (seconds).
    time_to_fill_high_s: float


def watermark_feasibility(
    *,
    servers: int,
    service_time_s: float,
    seconds_per_shot: float,
    profile: ReadingProfile,
    high_watermark_s: float,
) -> WatermarkFeasibility:
    """Can ``servers`` workers hold the §4.5 buffer above ``L`` for one reader?

    Production: each worker finishes a shot every ``service_time_s`` and each shot
    is ``seconds_per_shot`` of video, so the fleet produces
    ``servers × seconds_per_shot / service_time_s`` video-seconds/second.
    Consumption is the §4.1 per-reader rate. Feasible when production ≥
    consumption (the buffer fills faster than it drains, §4.10 sawtooth).
    """
    if service_time_s <= 0.0:
        raise ValueError("service_time_s must be positive")
    production = servers * seconds_per_shot / service_time_s
    consumption = profile.video_seconds_per_wallclock
    feasible = production >= consumption
    headroom = production / consumption if consumption > 0 else math.inf
    # Net fill rate is production minus consumption; time to fill the high band.
    net = production - consumption
    time_to_fill = high_watermark_s / net if net > 0 else math.inf
    return WatermarkFeasibility(
        production_video_s_per_s=production,
        consumption_video_s_per_s=consumption,
        feasible=feasible,
        headroom_ratio=headroom,
        time_to_fill_high_s=time_to_fill,
    )


__all__ = [
    "BudgetRunway",
    "QueueingResult",
    "ReadingProfile",
    "RenderDemand",
    "WatermarkFeasibility",
    "erlang_c",
    "max_concurrent_readers",
    "min_servers_for_utilisation",
    "mmc_queue",
    "watermark_feasibility",
]
