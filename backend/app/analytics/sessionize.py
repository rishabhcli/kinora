"""Gap-based sessionization — stitch an event stream into reading sessions.

A *session* is a maximal run of one user's events with no inactivity gap longer
than ``gap`` (default 30 minutes). This is the classic web-analytics definition,
specialised here to reading: a session is also split when the active book
changes, because Kinora sessions are per-book reading sittings.

The output (:class:`ReadingSession`) carries the engagement signal the product
cares about (kinora.md §5.3): wall-clock duration, distinct pages seen, deepest
page reached, words read, derived pages/min & words/min, the completion ratio
against the book's page count, the drop-off page (where the sitting ended), and
the viewer/director split. These feed the rollups and the query API.

Pure: deterministic given the event list and a ``gap``. No I/O.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.analytics.events import EventName, ReadMode, TrackedEvent

#: Default inactivity gap that splits two sessions.
DEFAULT_GAP = timedelta(minutes=30)


@dataclass(frozen=True)
class ReadingSession:
    """One derived reading sitting and its engagement metrics."""

    session_id: str
    anon_user_id: str | None
    book_id: str | None
    started_at: datetime
    ended_at: datetime
    event_count: int
    pages_seen: int
    deepest_page: int | None
    words_read: int
    completion_ratio: float | None
    dropoff_page: int | None
    director_event_count: int
    stall_count: int

    @property
    def duration_s(self) -> float:
        """Wall-clock seconds from first to last event in the session."""
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    @property
    def pages_per_min(self) -> float | None:
        """Distinct pages seen per minute of the session (``None`` if 0-duration)."""
        minutes = self.duration_s / 60.0
        if minutes <= 0 or self.pages_seen == 0:
            return None
        return self.pages_seen / minutes

    @property
    def words_per_min(self) -> float | None:
        """Words read per minute (``None`` if 0-duration / unknown words)."""
        minutes = self.duration_s / 60.0
        if minutes <= 0 or self.words_read == 0:
            return None
        return self.words_read / minutes

    @property
    def is_director_session(self) -> bool:
        """True if the reader used Director mode at all during the sitting."""
        return self.director_event_count > 0


def _make_session_id(
    anon_user_id: str | None, book_id: str | None, index: int, started_at: datetime
) -> str:
    """A stable, deterministic session id (used as the persisted ``session_key``)."""
    user = anon_user_id or "anon"
    book = book_id or "nobook"
    return f"rs_{user}_{book}_{int(started_at.timestamp())}_{index}"


@dataclass
class _Accumulator:
    """Mutable per-session scratch used while folding the event stream."""

    anon_user_id: str | None
    book_id: str | None
    started_at: datetime
    ended_at: datetime
    events: int = 0
    pages: set[int] = field(default_factory=set)
    deepest_page: int | None = None
    last_page: int | None = None
    words_by_page: dict[int, int] = field(default_factory=dict)
    director_events: int = 0
    stalls: int = 0
    page_count_hint: int | None = None

    def add(self, event: TrackedEvent) -> None:
        self.events += 1
        self.ended_at = max(self.ended_at, event.occurred_at)
        if event.mode is ReadMode.DIRECTOR or event.name in _DIRECTOR_EVENTS:
            self.director_events += 1
        if event.name is EventName.BUFFER_STALL:
            self.stalls += 1
        page = event.prop_int("page")
        if page is not None and page >= 0:
            self.pages.add(page)
            self.last_page = page
            self.deepest_page = page if self.deepest_page is None else max(self.deepest_page, page)
            word_count = event.prop_int("word_index")
            if word_count is not None:
                # ``word_index`` is the focus word position; treat the max seen on
                # a page as "words reached" on that page (monotone within a page).
                prev = self.words_by_page.get(page, 0)
                self.words_by_page[page] = max(prev, max(0, word_count))
        hint = event.prop_int("page_count")
        if hint is not None and hint > 0:
            self.page_count_hint = hint

    def finish(self, index: int) -> ReadingSession:
        pages_seen = len(self.pages)
        words_read = sum(self.words_by_page.values())
        completion: float | None = None
        if self.page_count_hint and self.deepest_page is not None:
            completion = min(1.0, (self.deepest_page + 1) / self.page_count_hint)
        return ReadingSession(
            session_id=_make_session_id(
                self.anon_user_id, self.book_id, index, self.started_at
            ),
            anon_user_id=self.anon_user_id,
            book_id=self.book_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            event_count=self.events,
            pages_seen=pages_seen,
            deepest_page=self.deepest_page,
            words_read=words_read,
            completion_ratio=completion,
            dropoff_page=self.last_page,
            director_event_count=self.director_events,
            stall_count=self.stalls,
        )


_DIRECTOR_EVENTS: frozenset[EventName] = frozenset(
    {
        EventName.DIRECTOR_COMMENT,
        EventName.DIRECTOR_REGEN,
        EventName.CANON_EDITED,
    }
)


def sessionize(
    events: list[TrackedEvent],
    *,
    gap: timedelta = DEFAULT_GAP,
) -> list[ReadingSession]:
    """Split ``events`` into per-user, per-book reading sessions (gap-based).

    Events are grouped by ``anon_user_id`` (None users grouped together), sorted
    by ``occurred_at``, then split whenever the inactivity gap is exceeded *or*
    the active ``book_id`` changes. Returns sessions ordered by start time.
    """
    by_user: dict[str | None, list[TrackedEvent]] = defaultdict(list)
    for event in events:
        by_user[event.anon_user_id].append(event)

    sessions: list[ReadingSession] = []
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.occurred_at, e.event_id))
        acc: _Accumulator | None = None
        index = 0
        for event in user_events:
            new_session = (
                acc is None
                or event.occurred_at - acc.ended_at > gap
                or event.book_id != acc.book_id
            )
            if new_session:
                if acc is not None:
                    sessions.append(acc.finish(index))
                    index += 1
                acc = _Accumulator(
                    anon_user_id=user,
                    book_id=event.book_id,
                    started_at=event.occurred_at,
                    ended_at=event.occurred_at,
                )
            assert acc is not None
            acc.add(event)
        if acc is not None:
            sessions.append(acc.finish(index))

    sessions.sort(key=lambda s: (s.started_at, s.session_id))
    return sessions


__all__ = ["DEFAULT_GAP", "ReadingSession", "sessionize"]
