"""Canon vault export — the human-inspectable Obsidian-style bible (kinora.md §8.1).

The canon graph is backed by a graph + vector index for millisecond agent
retrieval, but the design also wants a **markdown vault a reader can open and
review**. This service renders the whole canon of a book — every entity (with its
version history) and every continuity fact (with its beat interval, retired ones
included for the audit trail) — to markdown and writes it to object storage
under ``canon/<book_id>/``.
"""

from __future__ import annotations

import anyio
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.book import Book
from app.db.models.continuity import ContinuityState
from app.db.models.entity import Entity
from app.memory.interfaces import BlobStore
from app.storage.object_store import Keys


class VaultExport(BaseModel):
    """The result of a vault export: the written object keys and their markdown."""

    book_id: str
    index_key: str
    keys: list[str] = Field(default_factory=list)
    #: object key -> markdown content (so callers/tests can inspect what was written).
    files: dict[str, str] = Field(default_factory=dict)


def _cell(text: str | None) -> str:
    """Make a string safe for a markdown table cell."""
    return (text or "").replace("\n", " ").replace("|", r"\|")


class CanonVault:
    """Render a book's canon graph to a markdown vault and persist it."""

    def __init__(self, session: AsyncSession, *, blob_store: BlobStore) -> None:
        self.session = session
        self._store = blob_store

    async def export(self, book_id: str) -> VaultExport:
        """Build and write the markdown vault for ``book_id`` to object storage."""
        book = await self.session.get(Book, book_id)
        title = book.title if book is not None else book_id

        entities = list(
            (
                await self.session.execute(
                    select(Entity)
                    .where(Entity.book_id == book_id)
                    .order_by(Entity.type, Entity.entity_key, Entity.version)
                )
            )
            .scalars()
            .all()
        )
        states = list(
            (
                await self.session.execute(
                    select(ContinuityState)
                    .where(ContinuityState.book_id == book_id)
                    .order_by(ContinuityState.valid_from_beat, ContinuityState.version)
                )
            )
            .scalars()
            .all()
        )

        grouped: dict[str, list[Entity]] = {}
        for entity in entities:
            grouped.setdefault(entity.entity_key, []).append(entity)

        files: dict[str, str] = {}
        index_key = Keys.canon(book_id, "index.md")
        files[index_key] = self._render_index(title, book_id, grouped, states)
        for entity_key, versions in grouped.items():
            current = versions[-1]
            note_key = Keys.canon(book_id, f"{current.type.value}/{entity_key}.md")
            files[note_key] = self._render_entity(entity_key, versions)
        continuity_key = Keys.canon(book_id, "continuity.md")
        files[continuity_key] = self._render_continuity(states)

        for key, content in files.items():
            await anyio.to_thread.run_sync(
                self._store.put_bytes, key, content.encode("utf-8"), "text/markdown"
            )

        return VaultExport(book_id=book_id, index_key=index_key, keys=list(files), files=files)

    def _render_index(
        self,
        title: str,
        book_id: str,
        grouped: dict[str, list[Entity]],
        states: list[ContinuityState],
    ) -> str:
        lines = [f"# Canon — {title}", "", f"`book_id: {book_id}`", ""]
        by_type: dict[str, list[Entity]] = {}
        for versions in grouped.values():
            current = versions[-1]
            by_type.setdefault(current.type.value, []).append(current)
        for kind in sorted(by_type):
            lines.append(f"## {kind.capitalize()}s")
            lines.append("")
            for current in sorted(by_type[kind], key=lambda e: e.entity_key):
                alias = f" ({', '.join(current.aliases)})" if current.aliases else ""
                lines.append(
                    f"- [[{current.entity_key}]] — **{current.name}**{alias} "
                    f"· v{current.version}"
                )
            lines.append("")
        lines.append(f"## Continuity facts ({len(states)})")
        lines.append("")
        lines.append("See [[continuity]].")
        lines.append("")
        return "\n".join(lines)

    def _render_entity(self, entity_key: str, versions: list[Entity]) -> str:
        current = versions[-1]
        lines = [f"# {current.name}", "", f"- **entity_key:** `{entity_key}`"]
        lines.append(f"- **type:** {current.type.value}")
        if current.aliases:
            lines.append(f"- **aliases:** {', '.join(current.aliases)}")
        if current.description:
            lines.append(f"- **description:** {current.description}")
        if current.appearance:
            desc = current.appearance.get("description")
            if desc:
                lines.append(f"- **appearance:** {desc}")
            locked = current.appearance.get("locked")
            if locked is not None:
                lines.append(f"- **reference locked:** {bool(locked)}")
        if current.voice:
            voice_id = current.voice.get("cosyvoice_voice_id")
            if voice_id:
                lines.append(f"- **voice:** `{voice_id}`")
        if current.style_tokens:
            tokens = ", ".join(f"{k}={v}" for k, v in current.style_tokens.items())
            lines.append(f"- **style tokens:** {tokens}")
        lines.append("")
        lines.append("## Version history")
        lines.append("")
        lines.append("| version | valid_from_beat | valid_to_beat | name |")
        lines.append("|---|---|---|---|")
        for version in versions:
            valid_to = "—" if version.valid_to_beat is None else str(version.valid_to_beat)
            lines.append(
                f"| {version.version} | {version.valid_from_beat} | {valid_to} "
                f"| {_cell(version.name)} |"
            )
        lines.append("")
        return "\n".join(lines)

    def _render_continuity(self, states: list[ContinuityState]) -> str:
        lines = ["# Continuity facts", ""]
        lines.append("| subject | predicate | object | from | to | status |")
        lines.append("|---|---|---|---|---|---|")
        for state in states:
            retired = state.valid_to_beat is not None
            valid_to = "—" if state.valid_to_beat is None else str(state.valid_to_beat)
            status = "retired" if retired else "active"
            lines.append(
                f"| {_cell(state.subject_entity_key)} | {_cell(state.predicate)} "
                f"| {_cell(state.object_value)} | {state.valid_from_beat} | {valid_to} "
                f"| {status} |"
            )
        lines.append("")
        return "\n".join(lines)


__all__ = ["CanonVault", "VaultExport"]
