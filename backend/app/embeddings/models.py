"""Small shared value types for the embeddings subsystem.

Kept dependency-light (stdlib dataclasses + enums) so the vector/index/cache
layers don't pull in pydantic; the :mod:`app.embeddings.config` settings model is
the only pydantic surface here.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class EntityKind(enum.StrEnum):
    """What a namespace's vectors describe (mirrors the canon entity taxonomy)."""

    CHARACTER = "character"
    LOCATION = "location"
    PROP = "prop"
    STYLE = "style"
    SHOT = "shot"
    OTHER = "other"


class Modality(enum.StrEnum):
    """Whether an embedding came from an image or from text."""

    IMAGE = "image"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class ReembedReport:
    """Outcome of a re-embed-on-model-change migration pass.

    ``examined`` is every record considered; ``reembedded`` is how many were in a
    stale space and got recomputed; ``skipped_current`` were already in the target
    space; ``failed`` could not be recomputed (e.g. missing source bytes).
    """

    target_space_key: str
    examined: int = 0
    reembedded: int = 0
    skipped_current: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.reembedded > 0


__all__ = ["EntityKind", "Modality", "ReembedReport"]
