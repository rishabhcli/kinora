"""``FakeWorld`` — an in-memory Kinora assembled through the real seams.

This wires the synthetic book + the local deterministic fakes into the *real*
:class:`~app.render.pipeline.RenderPipeline` and the *real* §7.2
:class:`~app.render.conflict.ConflictResolver`, then layers a small deterministic
reader/buffer model on top of the *real* §4.3/§4.4 scheduler zone math
(:mod:`app.scheduler.zones`). The result is a single object a scenario can drive:
move the reader, render the next shot, watch the buffer stay ahead — all without
a database, Redis, MinIO, DashScope, or ffmpeg-via-network.

What is real here (not faked):
  * the §9.7 per-shot state machine + degradation ladder (RenderPipeline);
  * the §9.5 Critic routing (``decide_qa``) and §7.2 arbitration
    (``decide_arbitration``), driven by the Critic/Showrunner doubles;
  * the §4.3 ETA / §4.4 zone classification (``app.scheduler.zones``);
  * the content-hash cache math (``CacheService``) and the budget ledger shape;
  * the §9.4 sync-segment build (it is produced by the pipeline itself).

What is faked: the heavy model/provider calls and the byte stores — every one
deterministic, none touching the network. ``KINORA_LIVE_VIDEO`` is irrelevant
because the "live" Generator double never spends; the harness models spend with
the in-memory budget ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.contracts import DirectorNote
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.enums import ShotStatus
from app.e2e.clock import VirtualClock
from app.e2e.fakes import (
    FakeBeatRepo,
    FakeBudget,
    FakeCache,
    FakeCanon,
    FakeContinuity,
    FakeCritic,
    FakeDefectRepo,
    FakeDesigner,
    FakeEpisodic,
    FakeEvolver,
    FakeGenerator,
    FakeNarrator,
    FakeObjectStore,
    FakePageRepo,
    FakeShotRepo,
    FakeShowrunner,
    RowBeat,
    RowPage,
    RowShot,
    png_bytes,
)
from app.e2e.synthetic_book import REF_KEY, STYLE_REF_KEY, SyntheticBook, make_synthetic_book
from app.e2e.trace import TraceRecorder
from app.render.conflict import ConflictResolver
from app.render.pipeline import RenderPipeline, RenderResult
from app.scheduler import zones

logger = get_logger("app.e2e.world")

#: A passing QA metric set (the Critic accepts on the first take).
QA_PASS = {"ccs": 0.95, "style": 0.02, "timeline_ok": True, "motion": 0.05}
#: A timeline-failing metric set (drives the §7.2 conflict flow when paired with
#: a contradicting Continuity double).
QA_TIMELINE_FAIL = {"ccs": 0.95, "style": 0.02, "timeline_ok": False, "motion": 0.05}


@dataclass(slots=True)
class WorldConfig:
    """Knobs a scenario flips before assembling a :class:`FakeWorld`."""

    #: ``KINORA_LIVE_VIDEO`` analogue: when False the pipeline always degrades.
    live_video: bool = True
    #: Finite video-second pool the budget ledger enforces.
    budget_s: float = 1_000.0
    #: Remaining-floor below which the budget reads "low" (forces degradation).
    budget_low_floor_s: float = 0.0
    #: Per-call Critic metric sequence (defaults to a single pass).
    critic_metrics: list[dict[str, Any]] = field(default_factory=lambda: [dict(QA_PASS)])
    #: When True the Continuity double raises a §7.2 conflict.
    continuity_contradicts: bool = False
    #: Whether the source span textually supports an evolve (§7.2 honour/evolve).
    showrunner_supported: bool = False
    #: Make the Generator fail its first N renders (provider-failover scenario).
    generator_fail_first: int = 0
    #: Scheduler horizons (seconds) for the zone math (§4.4 defaults are coarse).
    commit_horizon_s: float = 8.0
    spec_horizon_s: float = 30.0
    #: The reader's word velocity (words/second). Default ~4 wps (§4.3).
    velocity_wps: float = 4.0


@dataclass(slots=True)
class ReaderState:
    """The harness's reader model — focus word + velocity (drives ETA math)."""

    focus_word: int = 0
    velocity_wps: float = 4.0
    raw_velocity_wps: float = 4.0
    oscillating: bool = False
    page: int = 1


class FakeWorld:
    """An assembled in-memory Kinora the scenarios drive end-to-end."""

    def __init__(
        self,
        *,
        book: SyntheticBook | None = None,
        config: WorldConfig | None = None,
        clock: VirtualClock | None = None,
        recorder: TraceRecorder | None = None,
    ) -> None:
        self.book = book or make_synthetic_book()
        self.config = config or WorldConfig()
        self.clock = clock or VirtualClock()
        self.recorder = recorder or TraceRecorder()
        self.reader = ReaderState(
            velocity_wps=self.config.velocity_wps, raw_velocity_wps=self.config.velocity_wps
        )

        # -- build the in-memory rows from the synthetic book ---------------- #
        shots = [
            RowShot(
                id=s.shot_id,
                book_id=self.book.book_id,
                beat_id=s.beat_id,
                scene_id=s.scene_id,
                source_span=dict(s.source_span),
                duration_s=s.duration_s,
            )
            for s in self.book.shots
        ]
        beats = [
            RowBeat(
                id=b.beat_id,
                book_id=self.book.book_id,
                scene_id=b.scene_id,
                beat_index=b.beat_index,
                summary=b.summary,
                entities=list(b.entities),
                described_visuals=b.described_visuals,
                mood=b.mood,
                source_span=dict(b.source_span),
            )
            for b in self.book.beats
        ]
        pages = {
            p.page_number: RowPage(
                word_boxes=list(p.word_boxes),
                image_key=f"pages/{self.book.book_id}/{p.page_number}.png",
                text=p.text,
            )
            for p in self.book.pages
        }

        self.shots = FakeShotRepo(shots)
        self.beats = FakeBeatRepo(beats)
        self.pages = FakePageRepo(pages)
        self.defects = FakeDefectRepo()
        self.canon = FakeCanon(self.book.canon_slice)
        self.cache = FakeCache()
        self.budget = FakeBudget(
            live=self.config.live_video,
            budget_s=self.config.budget_s,
            low_floor_s=self.config.budget_low_floor_s,
        )
        self.episodic = FakeEpisodic()
        # Seed the object store with the locked reference + style keyframes the
        # pipeline reads for reference-to-video + the style centroid.
        self.store = FakeObjectStore(
            seed={REF_KEY: png_bytes(640, 360), STYLE_REF_KEY: png_bytes(640, 360)}
        )
        self.designer = FakeDesigner()
        self.generator = FakeGenerator(fail_first=self.config.generator_fail_first)
        self.critic = FakeCritic(self.config.critic_metrics)
        self.narrator = FakeNarrator()
        self.continuity = FakeContinuity(contradicts=self.config.continuity_contradicts)
        self.showrunner = FakeShowrunner(supported=self.config.showrunner_supported)
        self.evolver = FakeEvolver()

        from tests.conftest import FakeEmbedder  # local fake, no infra

        self._embedder = FakeEmbedder()
        self._settings = Settings(dashscope_api_key="test")

        conflict_resolver = ConflictResolver(
            continuity=self.continuity,
            showrunner=self.showrunner,
            canon=self.evolver,
        )

        self.pipeline = RenderPipeline(
            canon=self.canon,
            episodic=self.episodic,
            cache=self.cache,
            budget=self.budget,
            object_store=self.store,
            shots=self.shots,
            beats=self.beats,
            pages=self.pages,
            defects=self.defects,
            designer=self.designer,
            generator=self.generator,
            critic=self.critic,
            narrator=self.narrator,
            conflict_resolver=conflict_resolver,
            embedder=self._embedder,
            settings=self._settings,
        )

    # -- scheduler-style zone math (real ``app.scheduler.zones``) ----------- #

    def zone_for_shot(self, shot_id: str) -> tuple[float, zones.Zone]:
        """``(eta, zone)`` for a shot relative to the reader (real §4.3/§4.4 math)."""
        shot = self.book.shot(shot_id)
        if shot is None:
            raise KeyError(shot_id)
        start = int(shot.source_span["word_range"][0])
        return zones.classify_shot(
            start,
            self.reader.focus_word,
            zones.clamp_velocity(self.reader.velocity_wps),
            commit_horizon_s=self.config.commit_horizon_s,
            spec_horizon_s=self.config.spec_horizon_s,
        )

    def committed_shots(self) -> list[str]:
        """Shot ids inside the commit horizon for the reader's current position."""
        out: list[str] = []
        for shot in self.book.shots:
            _eta, zone = self.zone_for_shot(shot.shot_id)
            if zone is zones.Zone.COMMITTED:
                out.append(shot.shot_id)
        return out

    def stable(self) -> bool:
        """§4.6 stability: False during a rapid skim (uncapped velocity high)."""
        return zones.trajectory_is_stable(self.reader)

    # -- the core render seam ----------------------------------------------- #

    async def render_shot(
        self,
        shot_id: str,
        *,
        session_id: str | None = "sess_e2e",
        director_notes: list[DirectorNote] | None = None,
        director_present: bool = False,
    ) -> RenderResult:
        """Drive one shot through the real pipeline + record the observable result."""
        result = await self.pipeline.render_shot(
            self.book.book_id,
            shot_id,
            session_id=session_id,
            director_notes=director_notes,
            director_present=director_present,
        )
        shot = self.book.shot(shot_id)
        page = self.book.beat_pages.get(shot.beat_id) if shot is not None else None
        status = result.status
        status_value = status.value if isinstance(status, ShotStatus) else str(status)
        seg = result.sync_segment or {}
        self.recorder.record(
            "shot_rendered",
            shot_id=shot_id,
            status=status_value,
            rung=result.rung,
            cache_hit=result.cache_hit,
            video_seconds=result.video_seconds,
            attempts=result.attempts,
            has_conflict=result.conflict is not None,
            has_decision=result.decision is not None,
            page=page,
            sync_words=len(seg.get("words", [])),
        )
        return result


__all__ = ["FakeWorld", "QA_PASS", "QA_TIMELINE_FAIL", "ReaderState", "WorldConfig"]
