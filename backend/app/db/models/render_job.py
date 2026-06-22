"""``render_jobs`` — priority-queue render jobs (kinora.md §12.1).

Tracks each enqueued render: its lane/priority, queue state machine, the
``shot_hash`` idempotency key, the cooperative ``cancel_token``, attempt count
(retry cap), the provider task id for polling, and the video-seconds reserved
against the budget.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, StrIdMixin, TimestampMixin
from app.db.models.enums import RenderJobStatus, RenderPriority, str_enum


class RenderJob(StrIdMixin, TimestampMixin, Base):
    """A unit of render work tracked through the queue lifecycle."""

    __tablename__ = "render_jobs"
    __table_args__ = ()

    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=True
    )
    shot_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    priority: Mapped[RenderPriority] = mapped_column(
        str_enum(RenderPriority, "render_priority"), nullable=False
    )
    status: Mapped[RenderJobStatus] = mapped_column(
        str_enum(RenderJobStatus, "render_job_status"),
        default=RenderJobStatus.QUEUED,
        nullable=False,
        index=True,
    )
    cancel_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_task_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserved_video_s: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
