"""LLM-ops persistence: prompt registry, changelog, run traces, eval reports.

The :mod:`app.llmops` platform keeps its working state in-memory (so every pure
component is testable with no infra); these tables are the durable backing for
the bits that must survive a restart:

* ``llmops_prompt_versions`` — every registered version of a prompt key (semver,
  the originating ``key@vN`` agent tag, the full system text, a sha256 content
  address, lifecycle status). One row per ``(key, version)``.
* ``llmops_changelog`` — the append-only audit of registry mutations
  (seed / register / promote / rollback / archive).
* ``llmops_runs`` — structured run traces (prompt + inputs + outputs + tokens +
  cost + latency + guardrail decision), with loose ``book_id`` / ``session_id``
  attribution (no FK, exactly like the cost meter's loose attribution).
* ``llmops_eval_reports`` — cached eval / A-B / regression reports as JSONB,
  keyed by an opaque report id, so the expensive harness can be run out-of-band
  and the API can serve the cached result (the §13 "guard expensive runs" policy
  the ``/eval/report`` endpoint already follows).

All additive: no FK references INTO existing tables, so the migration is purely
``create_table`` + indexes and trivially reversible.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin


class LLMOpsPromptVersion(StrIdMixin, CreatedAtMixin, Base):
    """One registered version of a prompt key (the registry's durable record)."""

    __tablename__ = "llmops_prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_key", "version", name="uq_llmops_prompt_key_version"),
        Index("ix_llmops_prompt_key_status", "prompt_key", "status"),
    )

    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Canonical semantic version string (``MAJOR.MINOR.PATCH``).
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The agents' originating ``key@vN`` tag (or synthetic for operator versions).
    prompt_tag: Mapped[str] = mapped_column(String(64), nullable=False)
    system: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: ``draft`` | ``active`` | ``archived``.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class LLMOpsChangelog(StrIdMixin, CreatedAtMixin, Base):
    """An append-only changelog entry for a registry mutation."""

    __tablename__ = "llmops_changelog"
    __table_args__ = (Index("ix_llmops_changelog_key", "prompt_key", "created_at"),)

    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``seed`` | ``register`` | ``promote`` | ``rollback`` | ``archive``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(128), nullable=False, default="operator")


class LLMOpsRun(StrIdMixin, CreatedAtMixin, Base):
    """A structured run trace (prompt + inputs + outputs + tokens + cost)."""

    __tablename__ = "llmops_runs"
    __table_args__ = (
        Index("ix_llmops_runs_key_version", "prompt_key", "prompt_version"),
        Index("ix_llmops_runs_book", "book_id"),
        Index("ix_llmops_runs_session", "session_id"),
        Index("ix_llmops_runs_model_created", "model", "created_at"),
    )

    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: USD cost; NUMERIC keeps Decimal exact (no binary-float drift).
    cost_usd: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    guardrail_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Loose attribution (no FK; same pattern as the cost meter).
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LLMOpsEvalReport(StrIdMixin, CreatedAtMixin, Base):
    """A cached eval / A-B / regression report (JSONB body)."""

    __tablename__ = "llmops_eval_reports"
    __table_args__ = (Index("ix_llmops_eval_kind_key", "kind", "prompt_key"),)

    #: ``eval`` | ``ab`` | ``regression``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


__all__ = [
    "LLMOpsChangelog",
    "LLMOpsEvalReport",
    "LLMOpsPromptVersion",
    "LLMOpsRun",
]
