"""Showrunner / Orchestrator — production planning + conflict arbitration (§7, §7.2).

The expensive model (``qwen3.7-max``), called sparingly. Two jobs:

* :meth:`plan_production` — decompose a book summary into a high-level scene plan;
* :meth:`arbitrate` — resolve a :class:`ConflictObject` under the FIXED §7.2
  policy and emit a :class:`DecisionRecord`.

The policy itself is the pure, deterministic :func:`decide_arbitration` — it
takes the conflict, whether the source text supports the change, and whether a
director is present, and returns the chosen option. That separation is what lets
all three branches (evolve / surface / honor) be unit-tested without a network:
the textual-support judgment is injectable.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.providers import Providers

from .base import BaseAgent
from .contracts import (
    ConflictObject,
    ConflictOption,
    DecisionRecord,
    ScenePlan,
    TextualSupport,
)
from .prompts import SHOWRUNNER


def decide_arbitration(
    conflict: ConflictObject,
    *,
    textual_support: bool,
    director_present: bool,
) -> tuple[ConflictOption, bool]:
    """Apply the §7.2 resolution policy. Returns ``(chosen_option, evolved_canon)``.

    Policy (in order):
      1. evolve the canon — only when the conflict offers that option AND the
         source text genuinely supports the change;
      2. surface to the user — when a director is present and the conflict is
         user-facing;
      3. honor the canon — the safe default.
    """
    offers_evolve = any(opt.id is ConflictOption.EVOLVE_CANON for opt in conflict.options)
    if offers_evolve and textual_support:
        return ConflictOption.EVOLVE_CANON, True
    if director_present and conflict.user_facing:
        return ConflictOption.SURFACE_TO_USER, False
    return ConflictOption.HONOR_CANON, False


class Showrunner(BaseAgent):
    """Plans the production and arbitrates conflicts under the fixed policy."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="showrunner",
            model=settings.chat_model_max,
            prompt=SHOWRUNNER,
            skills=skills,  # type: ignore[arg-type]
        )

    async def plan_production(
        self, book_summary: str, *, title: str | None = None, page_count: int | None = None
    ) -> ScenePlan:
        """Decompose a book summary into an ordered, high-level scene plan."""
        payload = {
            "task": "plan_production",
            "title": title,
            "page_count": page_count,
            "book_summary": book_summary,
        }
        return await self.run_json(payload, ScenePlan, temperature=0.3)

    async def judge_textual_support(
        self, conflict: ConflictObject, source_span_text: str
    ) -> TextualSupport:
        """Ask the model whether the source text genuinely supports the change (§7.2)."""
        payload = {
            "task": "judge_textual_support",
            "conflict": conflict.model_dump(mode="json"),
            "source_span_text": source_span_text,
        }
        return await self.run_json(payload, TextualSupport, temperature=0.0)

    async def arbitrate(
        self,
        conflict: ConflictObject,
        source_span_text: str,
        *,
        director_present: bool,
        textual_support: TextualSupport | None = None,
    ) -> DecisionRecord:
        """Resolve ``conflict`` under the §7.2 policy and return a decision record.

        ``textual_support`` may be injected (so the policy branches are testable
        without a network); when omitted, it is judged by a real model call.
        """
        if textual_support is None:
            textual_support = await self.judge_textual_support(conflict, source_span_text)
        chosen, evolved = decide_arbitration(
            conflict,
            textual_support=textual_support.supported,
            director_present=director_present,
        )
        return DecisionRecord(
            conflict_id=conflict.conflict_id,
            chosen_option=chosen,
            reasoning=self._reasoning(chosen, textual_support, director_present),
            evolved_canon=evolved,
        )

    @staticmethod
    def _reasoning(
        chosen: ConflictOption, support: TextualSupport, director_present: bool
    ) -> str:
        detail = f" ({support.reasoning})" if support.reasoning else ""
        if chosen is ConflictOption.EVOLVE_CANON:
            return f"Source text supports the change{detail}; evolving canon and regenerating."
        if chosen is ConflictOption.SURFACE_TO_USER:
            return (
                "No textual support for the change and a director is present on a "
                "user-facing conflict; surfacing for the reader to choose."
            )
        director = "no director present" if not director_present else "not user-facing"
        return f"No textual support for the change ({director}); honouring established canon."


__all__ = ["Showrunner", "decide_arbitration"]
