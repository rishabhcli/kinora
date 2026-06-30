"""Comparative adaptive-vs-baseline scheduling simulator (kinora.md §4.5–§4.10, §13).

The existing :func:`app.scheduler.simulation.replay_trace` drives the *real*
:class:`SchedulerService` over a reader trace and scores the buffer sawtooth. This
module is its **comparative** sibling: a self-contained, deterministic buffer
model that replays the *same* synthetic reader trace under two policies side by
side —

* a **fixed-watermark baseline** (the §4.5 constants ``L``/``H``/``C``, velocity-
  reactive only, no regime awareness, no provider fan-out, no prefetch); and
* the **adaptive v2 policy** (regime-sized watermarks, multi-provider concurrency-
  aware promotion, cold-zone prefetch/eviction);

and reports the three numbers that decide whether adaptation is worth it:

* **underrun rate** — fraction of reading-time the committed buffer sat below ``L``
  (and the count of hard stalls where it hit empty) — *quality*;
* **wasted renders** — committed video-seconds promoted for shots the reader never
  reached (a skim/seek blew past them) — *waste*;
* **cost** — total committed video-seconds promoted — *spend*.

The whole engine is pure and infra-free: a virtual clock, a synthetic reader
trace (reusing the :class:`~app.scheduler.simulation.ReaderProfile` archetypes),
and an in-memory provider model. It never imports Redis/Postgres/DashScope and
never touches the budget gate — "cost" here is *would-be* video-seconds, computed
offline, so a simulation can no more spend a credit than the production dry-run can.

Why a separate buffer model
---------------------------
The point of this module is an apples-to-apples A/B of *policies*, which means
both arms must run through identical buffer physics with only the policy knobs
differing. A small, transparent buffer model (promote → in-flight → lands after a
provider latency → drains as the reader advances) makes the *policy* the only
independent variable, so a win is attributable to the policy and not to scheduler
plumbing. It is validated against the real harness's qualitative behaviour (clean
sawtooth, zero stalls for a steady reader) in the tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.scheduler.adaptive import AdaptiveConfig, Watermarks
from app.scheduler.simulation import (
    ActionKind,
    ReaderProfile,
    ReadingTrace,
)
from app.scheduler.v2.provider import (
    Lane,
    PromotionCandidate,
    ProviderState,
    plan_promotions,
)
from app.scheduler.v2.velocity import (
    RegimeConfig,
    VelocityRegimeModel,
    predict_pages_needed,
    size_watermarks,
)
from app.scheduler.v2.velocity import UpcomingShot as _UpcomingShot
from app.scheduler.zones import (
    DEFAULT_VELOCITY_WPS,
    eta_seconds,
)

#: The simulation tick (the §4.7 settle cadence), seconds of virtual clock.
DEFAULT_TICK_S = 2.5
#: Idle-pause threshold (ms): a pause longer than this freezes the buffer (§4.7).
SIM_IDLE_PAUSE_MS = 8_000
#: A simulated shot is this many words wide / this many video-seconds long. The
#: §4.10 worked example pairs ~5s of video with ~30 words of text, so at the 4 wps
#: default a shot is ~7.5s of reading-time per 5s of video — video runs *slower*
#: than reading, which is what lets the committed buffer build a lead (and what
#: makes a sustained-fast reader the interesting under-buffer case).
DEFAULT_SHOT_WORDS = 30
DEFAULT_SHOT_DURATION_S = 5.0


@dataclass(frozen=True, slots=True)
class SimShot:
    """A simulated shot: a word-span and its would-be video duration."""

    shot_id: str
    word_index_start: int
    est_duration_s: float = DEFAULT_SHOT_DURATION_S


def build_sim_shots(
    count: int,
    *,
    spacing: int = DEFAULT_SHOT_WORDS,
    duration_s: float = DEFAULT_SHOT_DURATION_S,
) -> list[SimShot]:
    """Build ``count`` evenly-spaced simulated shots (mirrors test ``build_shots``)."""
    return [
        SimShot(shot_id=f"shot_{i:04d}", word_index_start=i * spacing, est_duration_s=duration_s)
        for i in range(1, count + 1)
    ]


@dataclass(slots=True)
class _Slot:
    """One render slot on a provider: ``free_at_ms`` is when it next idles.

    A slot processes one shot at a time, taking the provider's ``latency_s``. The
    *soonest-free* slot is preferred when fanning out promotions, so a faster
    provider (lower latency) churns more shots per unit wall-clock — which is the
    throughput edge the provider-aware policy exploits.
    """

    provider: str
    latency_s: float
    free_at_ms: int = 0


@dataclass(slots=True)
class _Pending:
    """A promoted shot sitting in the render queue / rendering on a slot.

    Counts toward ``committed_seconds_ahead`` the moment it is promoted (the §4.9
    buffer is measured in *promoted* video-seconds ahead, not just landed ones),
    and flips to landed at ``lands_at_ms`` once a slot finishes it.
    """

    shot: SimShot
    lands_at_ms: int


@dataclass(slots=True)
class PolicyMetrics:
    """The scored outcome of one policy arm over a trace (§13).

    All time figures are virtual-clock; all video figures are *would-be*
    video-seconds (the simulator never spends). ``underrun_fraction`` is the
    fraction of reading-time the committed buffer sat *below* ``L`` (lower is
    better); ``stalls`` is the count of hard empties; ``wasted_video_s`` is video
    promoted for shots the reader never reached; ``cost_video_s`` is total
    committed video promoted.
    """

    label: str
    ticks: int = 0
    duration_s: float = 0.0
    samples_below_low: int = 0
    time_below_low_s: float = 0.0
    total_time_s: float = 0.0
    stalls: int = 0
    promotions: int = 0
    cost_video_s: float = 0.0
    wasted_video_s: float = 0.0
    keyframes_prefetched: int = 0
    evictions: int = 0
    peak_committed_s: float = 0.0
    #: Internal: whether the buffer is currently in a stall (for onset counting).
    _in_stall: bool = False

    @property
    def underrun_fraction(self) -> float:
        """Fraction of reading-time the buffer sat below ``L`` (target → 0)."""
        if self.total_time_s <= 0.0:
            return 0.0
        return self.time_below_low_s / self.total_time_s

    @property
    def waste_fraction(self) -> float:
        """Wasted video as a fraction of all promoted video (target → 0)."""
        if self.cost_video_s <= 0.0:
            return 0.0
        return self.wasted_video_s / self.cost_video_s


@dataclass(slots=True)
class Comparison:
    """A baseline-vs-adaptive head-to-head over one trace (the proof unit)."""

    label: str
    baseline: PolicyMetrics
    adaptive: PolicyMetrics

    @property
    def underrun_improvement(self) -> float:
        """Baseline underrun fraction minus adaptive's (positive = adaptive wins)."""
        return self.baseline.underrun_fraction - self.adaptive.underrun_fraction

    @property
    def waste_improvement(self) -> float:
        """Baseline wasted video-seconds minus adaptive's (positive = adaptive wins)."""
        return self.baseline.wasted_video_s - self.adaptive.wasted_video_s

    @property
    def cost_improvement(self) -> float:
        """Baseline committed video-seconds minus adaptive's (positive = cheaper)."""
        return self.baseline.cost_video_s - self.adaptive.cost_video_s

    @property
    def adaptive_no_worse_underrun(self) -> bool:
        """Adaptive never has a *worse* committed-buffer underrun than baseline.

        The §4.6 safety bar **for a linearly-consuming reader** (steady / pondering
        / re-reading / seeking — anyone whose video buffer is the right thing to
        keep full). It does *not* hold for a SKIMMING reader, and shouldn't: §4.6
        deliberately suspends video promotion for a skimmer (they ride the cheap
        keyframe ladder, not full video), so the adaptive arm runs a *thinner*
        video buffer on purpose and trades the video-underrun number for a large
        spend saving — see :attr:`adaptive_is_a_win`.
        """
        return self.adaptive.underrun_fraction <= self.baseline.underrun_fraction + 1e-9

    #: Tolerances for "no material regression": a tiny underrun uptick (within the
    #: cold-start warmup noise) and a small cost overshoot (a marginally deeper but
    #: still-consumed buffer) are not regressions.
    UNDERRUN_TOLERANCE = 0.05
    COST_TOLERANCE_FRACTION = 0.10

    @property
    def adaptive_not_worse(self) -> bool:
        """Adaptive is **not a material regression** on any axis (the safety bar).

        Every archetype must pass this. "Not worse" allows a tiny underrun uptick
        within the warmup tolerance and a small cost overshoot (a deeper but
        consumed buffer), and — for the SKIMMING / seek case — accepts a higher
        *video* underrun when it buys a material spend cut, because §4.6 covers a
        skimmer with the keyframe ladder, not video (the video buffer is the wrong
        thing to keep full there).
        """
        cost_ok = self.adaptive.cost_video_s <= self.baseline.cost_video_s * (
            1.0 + self.COST_TOLERANCE_FRACTION
        ) + 1e-9
        waste_ok = self.adaptive.wasted_video_s <= self.baseline.wasted_video_s + 1e-9
        underrun_ok = (
            self.adaptive.underrun_fraction
            <= self.baseline.underrun_fraction + self.UNDERRUN_TOLERANCE
        )
        # Spend-for-quality trade: a ≥40% spend cut justifies a thinner video buffer.
        spend_trade = self.adaptive.cost_video_s <= 0.6 * self.baseline.cost_video_s
        return (cost_ok and waste_ok and underrun_ok) or (spend_trade and waste_ok)

    @property
    def adaptive_is_a_win(self) -> bool:
        """Adaptive is a *strict* win: materially better on some axis, no regression.

        A strict win means :attr:`adaptive_not_worse` holds **and** adaptive is
        materially better on at least one of underrun, waste, or cost. Some
        archetypes (a perfectly steady reader render can keep up with) are neutral —
        adaptive matches the baseline, which is the right outcome (no regression),
        but not a *strict* win; those return ``False`` here yet ``True`` for
        :attr:`adaptive_not_worse`.
        """
        if not self.adaptive_not_worse:
            return False
        better_underrun = self.underrun_improvement > self.UNDERRUN_TOLERANCE
        better_waste = self.waste_improvement > 1e-9
        better_cost = self.cost_improvement > self.baseline.cost_video_s * 0.05 + 1e-9
        return better_underrun or better_waste or better_cost


def _expand_trace(trace: ReadingTrace, tick_s: float) -> list[tuple[str, int, float]]:
    """Flatten a :class:`ReadingTrace` into per-tick ``(kind, focus_word, dt_ms)``.

    Mirrors :func:`app.scheduler.simulation.replay_trace`'s clock-stepping so both
    simulators see the identical reader motion, but yields a flat per-tick list the
    comparative engine can replay through two policies without re-deriving timing.
    """
    steps: list[tuple[str, int, float]] = []
    focus = trace.focus_word
    dt_ms = int(tick_s * 1000)
    for action in trace.actions:
        if action.kind is ActionKind.SEEK and action.target_word is not None:
            focus = action.target_word
            steps.append(("seek", focus, float(dt_ms)))
            continue
        n_ticks = max(1, int(round(action.duration_s / tick_s)))
        for _ in range(n_ticks):
            if action.kind is ActionKind.PAUSE:
                steps.append(("pause", focus, float(dt_ms)))
                continue
            words = int(round(action.velocity_wps * tick_s))
            focus += words
            steps.append(("read", focus, float(dt_ms)))
    return steps


def _next_shots_after(shots: list[SimShot], after_word: int, limit: int) -> list[SimShot]:
    out: list[SimShot] = []
    for shot in shots:
        if shot.word_index_start > after_word:
            out.append(shot)
            if len(out) >= limit:
                break
    return out


def simulate_policy(
    trace: ReadingTrace,
    shots: list[SimShot],
    *,
    adaptive: bool,
    settings: Settings | None = None,
    tick_s: float = DEFAULT_TICK_S,
    providers: list[ProviderState] | None = None,
    regime_config: RegimeConfig | None = None,
    adaptive_config: AdaptiveConfig | None = None,
    max_parallel: int | None = None,
) -> PolicyMetrics:
    """Replay ``trace`` through one policy arm and score it (§4.5–§4.10).

    The buffer physics are identical for both arms; only the *policy* differs:

    * **baseline** (``adaptive=False``): fixed §4.5 watermarks, promote in reading
      order up to ``H``/``C`` one provider at a time (the de-facto today behaviour),
      no regime awareness, no prefetch.
    * **adaptive** (``adaptive=True``): regime-sized watermarks
      (:func:`size_watermarks`), provider concurrency-aware fan-out
      (:func:`plan_promotions`), and forward page-need prediction
      (:func:`predict_pages_needed`) to choose candidates.

    Returns :class:`PolicyMetrics`. Promotions are tracked as would-be
    video-seconds; nothing is enqueued or reserved — provably zero real spend.
    """
    settings = settings or get_settings()
    base_wm = Watermarks(
        low_s=settings.watermark_low_s,
        high_s=settings.watermark_high_s,
        commit_horizon_s=settings.commit_horizon_s,
    )
    # Default provider pool: enough committed slots + a render latency under the
    # commit horizon so a *steady* reader's buffer can build a healthy lead — the
    # §4.10 sustainable regime. Pass a leaner / multi-provider pool to study the
    # render-bound cases where the concurrency-aware policy earns its keep.
    providers = providers or [
        ProviderState(name="wan", free_committed=6, latency_s=6.0),
    ]
    rc = regime_config or RegimeConfig()

    model = VelocityRegimeModel.fresh(velocity_wps=trace.nominal_velocity_wps)
    metrics = PolicyMetrics(label=f"{'adaptive' if adaptive else 'baseline'}:{trace.label}")

    # Render slots: one slot per declared committed slot per provider. The
    # *baseline* arm drains through one provider only (serial, the de-facto §4.9
    # single-backend today); the *adaptive* arm fans across every provider's slots.
    usable_providers = providers if adaptive else providers[:1]
    slots: list[_Slot] = [
        _Slot(provider=p.name, latency_s=p.latency_s)
        for p in usable_providers
        if p.healthy
        for _ in range(max(1, p.free_committed))
    ]
    if not slots:  # never leave a fully-saturated snapshot with zero capacity
        slots = [_Slot(provider=providers[0].name, latency_s=providers[0].latency_s)]

    steps = _expand_trace(trace, tick_s)
    clock_ms = 0
    prev_focus = trace.focus_word
    focus = trace.focus_word

    # A promoted shot lives in ``pending`` (queued/rendering) until ``lands_at_ms``,
    # then moves to ``ready`` (its clip exists). Two distinct buffer measures:
    #   * promoted-ahead (pending + ready) gates the §4.9 fill so we promote toward
    #     ``H`` exactly once and never re-promote a shot already in flight;
    #   * ready-ahead (landed clips only) is the *playable* buffer the §13 underrun
    #     / stall metric scores — a shot the reader reaches that is promoted but not
    #     yet rendered is a real playback stall (the clip isn't there yet).
    # This is the lever the provider/concurrency policy moves: faster rendering
    # (more providers, lower latency) lands clips sooner, so ready-ahead stays full.
    pending: dict[str, _Pending] = {}
    ready: dict[str, SimShot] = {}
    promoted_ids: set[str] = set()
    reached_words: set[int] = set()

    def land_due() -> None:
        landed = [sid for sid, p in pending.items() if p.lands_at_ms <= clock_ms]
        for sid in landed:
            ready[sid] = pending.pop(sid).shot

    def promoted_ahead() -> float:
        ahead = sum(
            p.shot.est_duration_s for p in pending.values() if p.shot.word_index_start > focus
        )
        ahead += sum(s.est_duration_s for s in ready.values() if s.word_index_start > focus)
        return round(ahead, 6)

    def ready_ahead() -> float:
        return round(
            sum(s.est_duration_s for s in ready.values() if s.word_index_start > focus), 6
        )

    def free_committed_snapshot() -> list[ProviderState]:
        """A capacity snapshot for the planner: free slots = slots idle *now*."""
        free_by_provider: dict[str, int] = {}
        lat_by_provider: dict[str, float] = {}
        for slot in slots:
            lat_by_provider[slot.provider] = slot.latency_s
            if slot.free_at_ms <= clock_ms:
                free_by_provider[slot.provider] = free_by_provider.get(slot.provider, 0) + 1
        return [
            ProviderState(
                name=name,
                free_committed=free_by_provider.get(name, 0),
                latency_s=lat_by_provider[name],
            )
            for name in lat_by_provider
            if free_by_provider.get(name, 0) > 0
        ]

    def assign_to_slot(shot: SimShot) -> None:
        """Place ``shot`` on the soonest-free slot; it lands when that slot frees.

        The slot serialises: if every slot is busy, the shot queues behind the
        soonest-free one and lands at that slot's free time plus a render. This is
        where provider speed matters — a faster provider's slots free sooner, so
        fanning out across providers lands the buffer's video earlier.
        """
        slot = min(slots, key=lambda s: s.free_at_ms)
        start_ms = max(clock_ms, slot.free_at_ms)
        lands = start_ms + int(slot.latency_s * 1000)
        slot.free_at_ms = lands
        pending[shot.shot_id] = _Pending(shot=shot, lands_at_ms=lands)
        promoted_ids.add(shot.shot_id)
        metrics.promotions += 1
        metrics.cost_video_s += shot.est_duration_s

    for kind, new_focus, dt_ms in steps:
        clock_ms += int(tick_s * 1000)
        # Land any clips whose render finished by now (pending → ready).
        land_due()

        if kind == "seek":
            # §4.8: re-seed. Cancel far pending/ready shots; the video-seconds spent
            # on a cancelled-but-never-reached shot are *wasted renders* (the classic
            # §4.8 waste — a far seek strands the buffer the reader will never see).
            words_delta = new_focus - focus
            focus = new_focus

            def _keep_near(start: int, *, _focus: int = focus) -> bool:
                return abs(eta_seconds(start, _focus, DEFAULT_VELOCITY_WPS)) <= 120.0

            kept: dict[str, _Pending] = {}
            for sid, p in pending.items():
                if _keep_near(p.shot.word_index_start):
                    kept[sid] = p
                elif p.shot.word_index_start not in reached_words:
                    metrics.wasted_video_s += p.shot.est_duration_s
            pending = kept
            ready = {sid: s for sid, s in ready.items() if _keep_near(s.word_index_start)}
            model.observe(words_advanced=words_delta, dt_ms=dt_ms, config=rc)
            prev_focus = focus
            _record_tick(metrics, ready_ahead(), base_wm, tick_s)
            continue

        if kind == "pause":
            # An idle pause (§4.7) freezes the buffer: in-flight clips still land,
            # but the loop promotes nothing into the void.
            model.observe(words_advanced=0, dt_ms=dt_ms, config=rc)
            _record_tick(metrics, ready_ahead(), base_wm, tick_s)
            continue

        # READ tick.
        prev_focus = focus
        focus = new_focus
        words = focus - prev_focus
        reached_words.add(focus)
        for shot in shots:
            if prev_focus < shot.word_index_start <= focus:
                reached_words.add(shot.word_index_start)
        model.observe(words_advanced=words, dt_ms=dt_ms, config=rc)

        # --- choose watermarks for this arm ------------------------------- #
        if adaptive:
            wm, _verdict = size_watermarks(
                base_wm, model, regime_config=rc, adaptive_config=adaptive_config
            )
        else:
            wm = base_wm

        promoted = promoted_ahead()
        metrics.peak_committed_s = max(metrics.peak_committed_s, promoted)

        # --- fill loop: promote toward H within the commit horizon -------- #
        if promoted < wm.high_s:
            candidates = _select_candidates(
                shots=shots,
                focus=focus,
                velocity=model.base.predict_velocity().mean_wps,
                wm=wm,
                pending=pending,
                promoted_ids=promoted_ids,
                headroom_s=wm.high_s - promoted,
                adaptive=adaptive,
                model=model,
                rc=rc,
            )
            # Promotion fills the buffer toward ``H`` (§4.9 promotes toward H; the
            # render slots throttle *concurrency*, not buffer depth — queued shots
            # still count as committed-ahead). The adaptive arm orders the release
            # with the provider-aware planner (soonest-landing across all free
            # slots; an explicit ``max_parallel`` cap, when set, limits this tick's
            # fan-out), the baseline releases in plain reading order.
            release_ids = _order_release(
                candidates,
                free_committed_snapshot() if adaptive else [],
                adaptive=adaptive,
                max_parallel=max_parallel,
            )
            for shot_id in release_ids:
                if shot_id in promoted_ids:
                    continue
                released = _shot_by_id(shots, shot_id)
                if released is not None:
                    assign_to_slot(released)

        _record_tick(metrics, ready_ahead(), base_wm, tick_s)

    # --- end-of-trace waste: shots promoted *beyond* the natural look-ahead --- #
    # A shot still ahead of the final playhead is only "wasted" if it sits beyond
    # the buffer a continuing reader would consume next — i.e. further than ``H``
    # reading-seconds ahead at the trace's end velocity. Shots inside that window
    # are the legitimate committed buffer (the reader simply stopped reading), not
    # over-promotion, so counting them would penalise a deeper-but-correct buffer.
    final_focus = focus
    end_v = max(0.1, model.base.predict_velocity().mean_wps)
    lookahead_words = base_wm.high_s * end_v
    for shot in shots:
        if (
            shot.shot_id in promoted_ids
            and shot.word_index_start > final_focus + lookahead_words
            and shot.word_index_start not in reached_words
        ):
            metrics.wasted_video_s += shot.est_duration_s

    metrics.duration_s = clock_ms / 1000.0
    return metrics


def _select_candidates(
    *,
    shots: list[SimShot],
    focus: int,
    velocity: float,
    wm: Watermarks,
    pending: dict[str, _Pending],
    promoted_ids: set[str],
    headroom_s: float,
    adaptive: bool,
    model: VelocityRegimeModel,
    rc: RegimeConfig,
) -> list[PromotionCandidate]:
    """Pick promotable candidates for this tick under the active policy.

    Both arms only consider shots inside the commit horizon not already promoted,
    and stop once the chosen video would refill the headroom to ``H`` (no
    over-fill). The adaptive arm routes the choice through
    :func:`predict_pages_needed` (regime-aware — a skim/seek/re-read yields nothing,
    so it never spends on pages the reader won't linearly consume) and then orders
    the fan-out with the provider-aware planner; the baseline takes the next
    uncommitted shots in plain reading order.
    """
    busy = promoted_ids | set(pending.keys())
    upcoming = [s for s in _next_shots_after(shots, focus, limit=64) if s.shot_id not in busy]

    if adaptive:
        verdict = model.classify(config=rc)
        needs = predict_pages_needed(
            [
                _UpcomingShot(
                    shot_id=s.shot_id,
                    word_index_start=s.word_index_start,
                    est_duration_s=s.est_duration_s,
                )
                for s in upcoming
            ],
            focus_word=focus,
            verdict=verdict,
            commit_horizon_s=wm.commit_horizon_s,
        )
        out: list[PromotionCandidate] = []
        budget = headroom_s
        for n in needs:
            if budget <= 0:
                break
            out.append(
                PromotionCandidate(
                    shot_id=n.shot_id, est_duration_s=n.est_duration_s, eta_s=n.eta_s
                )
            )
            budget -= n.est_duration_s
        return out

    # Baseline: reading-order, ETA < C, stop at headroom.
    out2: list[PromotionCandidate] = []
    budget = headroom_s
    v = max(0.1, velocity)
    for s in upcoming:
        if budget <= 0:
            break
        eta = (s.word_index_start - focus) / v
        if eta >= wm.commit_horizon_s:
            break
        out2.append(
            PromotionCandidate(shot_id=s.shot_id, est_duration_s=s.est_duration_s, eta_s=eta)
        )
        budget -= s.est_duration_s
    return out2


def _order_release(
    candidates: list[PromotionCandidate],
    snapshot: list[ProviderState],
    *,
    adaptive: bool,
    max_parallel: int | None,
) -> list[str]:
    """Order this tick's promotions (and apply any hard fan-out cap) (§4.9).

    Baseline: release every candidate in reading order (the de-facto §4.9 fill).
    Adaptive: run the provider-aware planner over the free-slot snapshot to put the
    soonest-landing shots first; an explicit ``max_parallel`` caps the tick's
    fan-out (the planner's ``deferred`` shots are held — re-offered next tick). When
    no slot is free this tick the planner defers everything, but with no cap the
    candidates are still released to *queue* (they count toward the buffer and wait
    for a slot), matching "promote toward H, slots throttle concurrency".
    """
    if not adaptive:
        return [c.shot_id for c in candidates]

    if snapshot:
        plan = plan_promotions(candidates, snapshot, max_parallel=max_parallel)
        ordered = [a.shot_id for a in plan.for_lane(Lane.COMMITTED)]
        if max_parallel is not None and max_parallel > 0:
            # Hard cap: only the planned fan-out is released this tick.
            return ordered
        # No hard cap: release the planned ones first, then queue the rest.
        deferred = [c.shot_id for c in plan.deferred]
        return ordered + deferred
    # No free slot this tick and no hard cap ⇒ queue everything (it still counts
    # toward the buffer and lands when a slot frees). A hard cap of 0/≤0 never
    # reaches here (treated as "no cap"); a positive cap with no free slots holds.
    if max_parallel is not None and max_parallel > 0:
        return []
    return [c.shot_id for c in candidates]


def _shot_by_id(shots: list[SimShot], shot_id: str) -> SimShot | None:
    for s in shots:
        if s.shot_id == shot_id:
            return s
    return None


def _record_tick(
    metrics: PolicyMetrics, ahead: float, base_wm: Watermarks, tick_s: float
) -> None:
    """Fold one tick's buffer occupancy into the underrun/stall accounting (§13).

    Underrun is always scored against the **fixed §4.5 ``L``** (``base_wm.low_s``),
    not the per-arm watermark, so both arms are judged by the same quality bar —
    the adaptive arm doesn't get to lower its own pass mark.
    """
    metrics.ticks += 1
    metrics.total_time_s += tick_s
    if ahead < base_wm.low_s:
        metrics.samples_below_low += 1
        metrics.time_below_low_s += tick_s
    # A hard stall is the *onset* of an empty buffer (count once per transition).
    if ahead <= 0.0:
        if not metrics._in_stall:
            metrics.stalls += 1
            metrics._in_stall = True
    else:
        metrics._in_stall = False


def compare_policies(
    trace: ReadingTrace,
    shots: list[SimShot] | None = None,
    *,
    settings: Settings | None = None,
    providers: list[ProviderState] | None = None,
    regime_config: RegimeConfig | None = None,
    adaptive_config: AdaptiveConfig | None = None,
    tick_s: float = DEFAULT_TICK_S,
    max_parallel: int | None = None,
) -> Comparison:
    """Run baseline and adaptive over the same trace and return the head-to-head.

    The proof unit: identical reader motion, identical buffer physics, identical
    provider pool — only the policy differs. Use the returned :class:`Comparison`'s
    ``underrun_improvement`` / ``waste_improvement`` / ``adaptive_no_worse_underrun``
    to assert the adaptive policy is a strict-or-equal win.
    """
    shots = shots or build_sim_shots(600)
    baseline = simulate_policy(
        trace,
        shots,
        adaptive=False,
        settings=settings,
        tick_s=tick_s,
        providers=providers,
        regime_config=regime_config,
        adaptive_config=adaptive_config,
        max_parallel=max_parallel,
    )
    adaptive = simulate_policy(
        trace,
        shots,
        adaptive=True,
        settings=settings,
        tick_s=tick_s,
        providers=providers,
        regime_config=regime_config,
        adaptive_config=adaptive_config,
        max_parallel=max_parallel,
    )
    return Comparison(label=trace.label, baseline=baseline, adaptive=adaptive)


def standard_scenarios() -> list[ReadingTrace]:
    """The canonical reader archetypes the comparison is proven across (§4.11).

    Reuses the existing :class:`~app.scheduler.simulation.ReaderProfile` generators
    so the v2 comparison and the real harness share one definition of "a steady
    reader", "a skimmer", etc. — deterministic, seeded, reproducible.
    """
    return [
        ReaderProfile.steady(velocity_wps=4.0, duration_s=240.0),
        ReaderProfile.steady(velocity_wps=8.0, duration_s=180.0),
        ReaderProfile.variable(base_wps=5.0, jitter=0.6, segments=20, seed=7),
        ReaderProfile.skimmer(velocity_wps=16.0, duration_s=120.0),
        ReaderProfile.thinker(velocity_wps=3.0, read_s=30.0, pause_s=25.0, cycles=5),
        ReaderProfile.seeker(velocity_wps=4.0, read_s=40.0, jumps=(4000, 200, 6000)),
    ]


__all__ = [
    "DEFAULT_SHOT_DURATION_S",
    "DEFAULT_SHOT_WORDS",
    "DEFAULT_TICK_S",
    "SIM_IDLE_PAUSE_MS",
    "Comparison",
    "PolicyMetrics",
    "SimShot",
    "build_sim_shots",
    "compare_policies",
    "simulate_policy",
    "standard_scenarios",
]
