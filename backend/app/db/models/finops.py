"""``cost_ledger`` — the append-only, auditable USD cost ledger (kinora.md §11.1, §12.5).

Distinct from ``budget_ledger`` (which meters the scarce, hard-capped
*video-seconds*). This ledger records the **money** valuation of every unit of
spend — tokens, images, audio, *and* video — so FinOps can attribute USD per
tenant / book / session / scene / shot / agent and reconcile that valuation
against both the physical video-seconds ledger and the live cost meter.

Like the budget ledger, every row is immutable: a render's spend is *appended*,
never updated. Cost is stored in **micro-USD as an integer** (``cost_micros``) to
avoid binary-float drift in the database — Decimal USD is reconstructed as
``cost_micros / 1_000_000``. Physical units are carried alongside for the §12.5
per-shot telemetry and for reconciliation against the video budget.
"""

from __future__ import annotations

import enum

from sqlalchemy import BigInteger, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin
from app.db.models.enums import str_enum


class CostKind(enum.StrEnum):
    """The coarse kind of spend a cost-ledger row records (mirrors ``Usage.operation``)."""

    CHAT = "chat"
    VL = "vl"
    IMAGE = "image"
    TTS = "tts"
    ASR = "asr"
    EMBEDDING = "embedding"
    VIDEO = "video"
    OTHER = "other"

    @classmethod
    def from_operation(cls, operation: str) -> CostKind:
        """Map a provider ``Usage.operation`` label onto a ledger kind."""
        try:
            return cls(operation.lower())
        except ValueError:
            return cls.OTHER


class CostLedger(StrIdMixin, CreatedAtMixin, Base):
    """One immutable USD-valued spend movement (append-only).

    All scope columns are nullable plain strings (not FKs): a cost may be
    attributed at several granularities at once, a render can be costed before the
    scene/shot row is persisted, and the ledger must survive a book/session/user
    deletion for clean lifetime accounting (§11.1 — the budget is lifetime-scoped).
    """

    __tablename__ = "cost_ledger"
    __table_args__ = (
        Index("ix_cost_ledger_scope", "tenant_id", "book_id", "session_id"),
        Index("ix_cost_ledger_shot", "shot_id"),
        Index("ix_cost_ledger_kind", "kind"),
        Index("ix_cost_ledger_agent", "agent"),
    )

    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: The crew role spend is attributed to (app.finops.attribution.Agent value).
    agent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The model id the spend was billed against.
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[CostKind] = mapped_column(str_enum(CostKind, "cost_kind"), nullable=False)

    #: USD cost in micro-dollars (integer; reconstruct USD as / 1_000_000).
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    #: Physical units (for §12.5 telemetry + reconciliation against the budget).
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    images: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    video_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
