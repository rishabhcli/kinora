"""In-memory fakes for the flow service ports — zero credits, fully deterministic.

These implement :class:`~app.distributed.sagas.flows.ingest.IngestPorts` and
:class:`~app.distributed.sagas.flows.render.RenderPorts` against plain in-memory
state, recording every call so a test can assert exactly what ran (and, after a
compensation, what was undone). They spend no credits and never touch a real
provider — the production adapters live elsewhere and are wired in via the
orchestrator's ``resources`` bag; these fakes are the test/harness substitute.

Each fake also supports **fault injection**: a set of step names to fail on, with a
counter so a test can make a step fail the first N times (transient) or always
(terminal). This is how the flow tests exercise retry, compensation, and the
degrade ladder deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.distributed.sagas.flows.render import Conflict


@dataclass
class FakeIngestServices:
    """A fake :class:`IngestPorts` recording calls + supporting fault injection."""

    calls: list[str] = field(default_factory=list)
    # step-name -> remaining times to fail (>=10_000 means "always")
    fail: dict[str, int] = field(default_factory=dict)
    staged: set[str] = field(default_factory=set)
    pages: dict[str, int] = field(default_factory=dict)
    canon: dict[str, int] = field(default_factory=dict)
    locked: dict[str, list[str]] = field(default_factory=dict)
    ready: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    page_count: int = 12

    def _maybe_fail(self, key: str) -> None:
        n = self.fail.get(key, 0)
        if n > 0:
            self.fail[key] = n - 1 if n < 10_000 else n
            raise RuntimeError(f"injected fault: {key}")

    async def stage_source(self, book_id: str, source_uri: str) -> str:
        self.calls.append("stage_source")
        self._maybe_fail("stage_source")
        key = f"oss://staged/{book_id}.pdf"
        self.staged.add(key)
        return key

    async def delete_source(self, object_key: str) -> None:
        self.calls.append("delete_source")
        self.staged.discard(object_key)

    async def extract_pages(self, book_id: str, object_key: str) -> int:
        self.calls.append("extract_pages")
        self._maybe_fail("extract_pages")
        self.pages[book_id] = self.page_count
        return self.page_count

    async def drop_pages(self, book_id: str) -> None:
        self.calls.append("drop_pages")
        self.pages.pop(book_id, None)

    async def build_canon(self, book_id: str, page_count: int) -> int:
        self.calls.append("build_canon")
        self._maybe_fail("build_canon")
        version = self.canon.get(book_id, 0) + 1
        self.canon[book_id] = version
        return version

    async def delete_canon(self, book_id: str, version: int) -> None:
        self.calls.append("delete_canon")
        if self.canon.get(book_id) == version:
            self.canon.pop(book_id, None)

    async def lock_identity(self, book_id: str, canon_version: int) -> list[str]:
        self.calls.append("lock_identity")
        self._maybe_fail("lock_identity")
        refs = [f"{book_id}:char_{i}@v{canon_version}" for i in range(2)]
        self.locked[book_id] = refs
        return refs

    async def unlock_identity(self, book_id: str, reference_ids: list[str]) -> None:
        self.calls.append("unlock_identity")
        self.locked.pop(book_id, None)

    async def mark_ready(self, book_id: str) -> None:
        self.calls.append("mark_ready")
        self._maybe_fail("mark_ready")
        self.ready.add(book_id)
        self.failed.discard(book_id)

    async def mark_failed(self, book_id: str) -> None:
        self.calls.append("mark_failed")
        self.ready.discard(book_id)
        self.failed.add(book_id)


@dataclass
class FakeRenderServices:
    """A fake :class:`RenderPorts` recording calls + supporting fault injection."""

    calls: list[str] = field(default_factory=list)
    fail: dict[str, int] = field(default_factory=dict)
    # shot_hash -> cached clip id (a pre-seeded cache hit)
    cache: dict[str, str] = field(default_factory=dict)
    reservations: dict[str, float] = field(default_factory=dict)
    released: set[str] = field(default_factory=set)
    rendered: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    accepted: set[str] = field(default_factory=set)
    unaccepted: set[str] = field(default_factory=set)
    evolved: list[str] = field(default_factory=list)
    director_present: bool = False
    # A conflict to raise on the first N QA calls (then pass). None = always pass.
    conflict: Conflict | None = None
    conflict_times: int = 0
    _qa_calls: int = 0
    _reservation_seq: int = 0
    _clip_seq: int = 0

    def _maybe_fail(self, key: str) -> None:
        n = self.fail.get(key, 0)
        if n > 0:
            self.fail[key] = n - 1 if n < 10_000 else n
            raise RuntimeError(f"injected fault: {key}")

    async def cache_lookup(self, shot_hash: str) -> str | None:
        self.calls.append("cache_lookup")
        return self.cache.get(shot_hash)

    async def reserve_budget(self, shot_id: str, seconds: float) -> str:
        self.calls.append("reserve_budget")
        self._maybe_fail("reserve_budget")
        self._reservation_seq += 1
        rid = f"res_{self._reservation_seq}"
        self.reservations[rid] = seconds
        return rid

    async def release_budget(self, reservation_id: str) -> None:
        self.calls.append("release_budget")
        self.reservations.pop(reservation_id, None)
        self.released.add(reservation_id)

    async def render_clip(self, shot_id: str, *, degraded: bool) -> str:
        self.calls.append("render_clip")
        self._maybe_fail("render_clip")
        self._clip_seq += 1
        clip_id = f"clip_{shot_id}_{self._clip_seq}{'_kb' if degraded else ''}"
        self.rendered.append(clip_id)
        return clip_id

    async def discard_clip(self, clip_id: str) -> None:
        self.calls.append("discard_clip")
        self.discarded.append(clip_id)

    async def qa_clip(self, shot_id: str, clip_id: str) -> Conflict | None:
        self.calls.append("qa_clip")
        self._qa_calls += 1
        if self.conflict is not None and self._qa_calls <= self.conflict_times:
            return self.conflict
        return None

    async def is_director_present(self, shot_id: str) -> bool:
        return self.director_present

    async def evolve_canon(self, shot_id: str, conflict: Conflict) -> None:
        self.calls.append("evolve_canon")
        self.evolved.append(conflict.conflict_id)

    async def accept_shot(self, shot_id: str, clip_id: str) -> None:
        self.calls.append("accept_shot")
        self._maybe_fail("accept_shot")
        self.accepted.add(clip_id)

    async def unaccept_shot(self, shot_id: str, clip_id: str) -> None:
        self.calls.append("unaccept_shot")
        self.accepted.discard(clip_id)
        self.unaccepted.add(clip_id)


__all__ = ["FakeIngestServices", "FakeRenderServices"]
