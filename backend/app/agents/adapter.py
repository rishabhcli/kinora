"""Adapter / Screenwriter — page → beats → shot list (§4.2, §9.1, §10).

Reads page text (and detected illustrations) into narrative beats, then
decomposes beats into ~5-second shots with their source spans and cost
estimates. It is the :class:`app.memory.interfaces.ShotPlanner` the memory layer
declares as a seam: :meth:`plan_scene` reads a scene's beats and returns the
render-queue shot specs.

``analyze_page`` is the only LLM call here (page → beats, with the §10 "never
invent a character" guardrail). ``plan_shots`` is deterministic, so the
beat→shot decomposition is unit-testable without a network; ``plan_scene`` wraps
it over the persisted beats (read via ``BeatRepo``).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.repositories.beat import BeatRepo
from app.db.session import get_session
from app.memory.interfaces import ShotPlanner
from app.memory.interfaces import ShotSpec as RenderShotSpec
from app.providers import Providers

from .base import BaseAgent
from .contracts import (
    AnalyzePageRequest,
    AnalyzePageResponse,
    Beat,
    EstCost,
    ShotListItem,
    SourceSpan,
)
from .prompts import ADAPTER

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
BeatsLoader = Callable[[str], Awaitable[Sequence[Any]]]

#: A beat splits into shots of roughly this many narration words each (~5s screen
#: time), matching the §4.1 reading/screen-time asymmetry.
WORDS_PER_SHOT = 60
SHOT_SECONDS = 5.0
#: Token-cost estimate per shot: a fixed planning/prompt floor plus per-word.
BASE_SHOT_TOKENS = 1500
TOKENS_PER_WORD = 6


class Adapter(BaseAgent):
    """Turns pages into beats and beats into a shot list (implements ShotPlanner)."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        session_factory: SessionFactory = get_session,
        beats_loader: BeatsLoader | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="adapter",
            model=settings.chat_model_adapter,
            prompt=ADAPTER,
            skills=skills,  # type: ignore[arg-type]
        )
        self._sf = session_factory
        self._beats_loader = beats_loader

    # -- page → beats (§9.1) ------------------------------------------------- #

    async def analyze_page(
        self,
        page_text: str,
        *,
        page: int = 1,
        scene_id: str | None = None,
        beat_index_start: int = 0,
        detected_illustrations: list[str] | None = None,
        known_entities: set[str] | None = None,
        max_tokens: int | None = None,
    ) -> list[Beat]:
        """Segment a page into beats; assign canonical ids and resolve entities.

        When ``known_entities`` is given, any entity the model named that is not
        in the canon is moved to ``unresolved_entities`` — a deterministic
        enforcement of the §10 "never invent a character" guardrail. ``max_tokens``
        bounds the (potentially large) multi-beat JSON generation.
        """
        request = AnalyzePageRequest(
            page=page,
            page_text=page_text,
            scene_id=scene_id,
            beat_index_start=beat_index_start,
            detected_illustrations=detected_illustrations or [],
        )
        response = await self.run_json(
            request, AnalyzePageResponse, temperature=0.2, max_tokens=max_tokens
        )
        beats: list[Beat] = []
        for offset, raw in enumerate(response.beats):
            index = beat_index_start + offset
            entities, unresolved = self._resolve_entities(raw, known_entities)
            span = raw.source_span.model_copy(update={"page": raw.source_span.page or page})
            beats.append(
                raw.model_copy(
                    update={
                        "beat_id": f"beat_{index:04d}",
                        "beat_index": index,
                        "scene_id": raw.scene_id or scene_id,
                        "entities": entities,
                        "unresolved_entities": unresolved,
                        "source_span": span,
                    }
                )
            )
        return beats

    @staticmethod
    def _resolve_entities(
        beat: Beat, known_entities: set[str] | None
    ) -> tuple[list[str], list[str]]:
        if known_entities is None:
            return list(beat.entities), list(beat.unresolved_entities)
        kept = [e for e in beat.entities if e in known_entities]
        unresolved = list(beat.unresolved_entities)
        for entity in beat.entities:
            if entity not in known_entities and entity not in unresolved:
                unresolved.append(entity)
        return kept, unresolved

    # -- beats → shots (deterministic, §4.2) --------------------------------- #

    def plan_shots(self, beats: Sequence[Beat]) -> list[ShotListItem]:
        """Split each beat into ~5s shots with source spans and cost estimates."""
        items: list[ShotListItem] = []
        for beat in beats:
            for shot_index, span in enumerate(self._split_span(beat.source_span)):
                words = max(span.word_range[1] - span.word_range[0], 0) or WORDS_PER_SHOT
                items.append(
                    ShotListItem(
                        shot_id=f"{beat.beat_id or 'beat'}_shot_{shot_index:02d}",
                        beat_id=beat.beat_id,
                        scene_id=beat.scene_id,
                        source_span=span,
                        est_duration_s=SHOT_SECONDS,
                        est_cost=EstCost(
                            video_seconds=SHOT_SECONDS,
                            tokens=BASE_SHOT_TOKENS + words * TOKENS_PER_WORD,
                        ),
                    )
                )
        return items

    @staticmethod
    def _split_span(span: SourceSpan) -> list[SourceSpan]:
        start, end = span.word_range
        words = max(end - start, 0)
        count = max(1, math.ceil(words / WORDS_PER_SHOT)) if words else 1
        if count == 1:
            return [span]
        step = words / count
        spans: list[SourceSpan] = []
        for i in range(count):
            lo = start + round(i * step)
            hi = end if i == count - 1 else start + round((i + 1) * step)
            spans.append(span.model_copy(update={"word_range": (lo, hi)}))
        return spans

    # -- ShotPlanner protocol (§ interfaces) --------------------------------- #

    async def plan_scene(self, scene_id: str) -> list[RenderShotSpec]:
        """Read a scene's beats and return the render-queue shot specs.

        Persistence of beats/shots is the ingest phase's job; this returns typed
        objects only. The Cinematographer fills render_mode/prompt/refs later.
        """
        rows = await self._load_scene_beats(scene_id)
        beats = [self._beat_from_row(row) for row in rows]
        book_id = str(getattr(rows[0], "book_id", "")) if rows else ""
        return [self._render_spec(item, book_id) for item in self.plan_shots(beats)]

    async def _load_scene_beats(self, scene_id: str) -> Sequence[Any]:
        if self._beats_loader is not None:
            return await self._beats_loader(scene_id)
        async with self._sf() as session:
            return await BeatRepo(session).list_by_scene(scene_id)

    @staticmethod
    def _beat_from_row(row: Any) -> Beat:
        raw_span = getattr(row, "source_span", None) or {}
        span = SourceSpan.model_validate(raw_span) if raw_span else SourceSpan()
        return Beat(
            beat_id=str(getattr(row, "id", "")),
            scene_id=getattr(row, "scene_id", None),
            beat_index=int(getattr(row, "beat_index", 0) or 0),
            summary=str(getattr(row, "summary", "")),
            entities=list(getattr(row, "entities", None) or []),
            described_visuals=getattr(row, "described_visuals", None),
            mood=getattr(row, "mood", None),
            source_span=span,
        )

    @staticmethod
    def _render_spec(item: ShotListItem, book_id: str) -> RenderShotSpec:
        return RenderShotSpec(
            book_id=book_id,
            beat_id=item.beat_id,
            scene_id=item.scene_id,
            shot_id=item.shot_id,
            target_duration_s=item.est_duration_s,
        )


def _planner_conformance(adapter: Adapter) -> ShotPlanner:
    """Static guarantee (checked by ``mypy app``) that Adapter is a ShotPlanner."""
    return adapter


__all__ = [
    "BASE_SHOT_TOKENS",
    "SHOT_SECONDS",
    "TOKENS_PER_WORD",
    "WORDS_PER_SHOT",
    "Adapter",
]
