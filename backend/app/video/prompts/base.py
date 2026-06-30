"""The :class:`PromptDialect` plugin interface + the :class:`RenderedPrompt` result.

A dialect is the pure translation from one canonical :class:`ShotDescription` into
*one* model's best prompt string (+ optional native negative prompt), within that
model's length budget and using that model's camera/film-grammar vocabulary and
quality tokens.

The contract is small on purpose:

* :attr:`PromptDialect.name` — the registry key ("wan", "runway", …).
* :attr:`PromptDialect.spec` — declarative capabilities (budget, negative-prompt
  support, weighting syntax, structured-vs-free-text) so callers/tests can reason
  about a dialect without rendering.
* :meth:`PromptDialect.render` — the one function that matters; returns a
  :class:`RenderedPrompt`.

A concrete dialect overrides :meth:`_compose_clauses` (priority-ordered positive
clauses) and :meth:`_negative_terms`; the base :meth:`render` applies the shared
length-aware fit + negative-prompt placement so every dialect inherits the budget
guarantee (non-empty, within budget) for free.

Pure / deterministic; no I/O, no provider imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field

from .canonical import ShotDescription
from .compress import fit_clauses

#: A generous default char budget for a model that does not publish a hard cap.
DEFAULT_PROMPT_BUDGET = 2000


class NegativeStyle(BaseModel):
    """How a model accepts "things to avoid"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: True when the model has a dedicated negative-prompt input.
    supported: bool = False
    #: Char budget for the negative prompt (only meaningful when ``supported``).
    budget: int = 512


class DialectSpec(BaseModel):
    """A dialect's declared capabilities — inspectable without rendering.

    Lets the registry, the router, and tests reason about a model's prompt
    contract (how long, whether it has a negative channel, whether it wants
    structured fields or free prose, whether it supports ``(token:weight)``
    weighting) without invoking :meth:`PromptDialect.render`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The registry key / model family ("wan", "runway", …).
    name: str
    #: A human label for the activity feed ("Runway Gen-3 Alpha").
    label: str = ""
    #: Max characters for the positive prompt.
    prompt_budget: int = DEFAULT_PROMPT_BUDGET
    #: The negative-prompt contract.
    negative: NegativeStyle = Field(default_factory=NegativeStyle)
    #: True when the model reads structured/keyed fields rather than free prose.
    structured: bool = False
    #: True when the model supports inline ``(token:weight)`` emphasis weighting.
    supports_weighting: bool = False
    #: Model-version ids this dialect targets (informational).
    model_ids: tuple[str, ...] = ()


class RenderedPrompt(BaseModel):
    """The output of a dialect: the model-ready prompt (+ optional negative).

    ``negative_prompt`` is ``None`` for a model with no negative channel — such a
    dialect folds an avoid-clause into ``prompt`` instead, so a caller can pass
    :class:`RenderedPrompt` straight through without branching on the model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dialect: str
    prompt: str
    negative_prompt: str | None = None

    @property
    def is_empty(self) -> bool:
        """True when the positive prompt is blank (only for an empty description)."""
        return not self.prompt.strip()


class PromptDialect(ABC):
    """The plugin interface: translate a canonical shot into one model's prompt.

    Subclasses declare :attr:`spec` and implement :meth:`_compose_clauses` (the
    positive prompt, as priority-ordered clauses) and :meth:`_negative_terms`. The
    base :meth:`render` handles the shared work — length-aware fitting against
    ``spec.prompt_budget`` and routing the negatives into the native channel or a
    folded text clause — so behaviour is uniform across dialects.
    """

    #: Set by subclasses (a frozen :class:`DialectSpec`).
    spec: DialectSpec

    @property
    def name(self) -> str:
        """The registry key (``spec.name``)."""
        return self.spec.name

    # -- the one method callers use ----------------------------------------- #

    def render(self, shot: ShotDescription, *, budget: int | None = None) -> RenderedPrompt:
        """Translate ``shot`` into this model's prompt within ``budget`` characters.

        ``budget`` overrides ``spec.prompt_budget`` when given (e.g. a caller with
        a tighter cap). The positive prompt is fit with the length-aware
        compressor; negatives go to the native channel if supported, else a short
        avoid-clause is appended to the positive prompt (still inside the budget).
        """
        limit = budget if budget is not None else self.spec.prompt_budget
        clauses = list(self._compose_clauses(shot))
        negatives = self._negative_terms(shot)

        if self.spec.negative.supported:
            negative_prompt = self._format_negative(negatives) if negatives else None
            prompt = fit_clauses(clauses, limit)
        else:
            # No native negative channel: fold a compact avoid-clause in last
            # (lowest priority, so it is dropped first under a tight budget).
            negative_prompt = None
            if negatives:
                clauses = [*clauses, self._fold_negative_clause(negatives)]
            prompt = fit_clauses(clauses, limit)
        return RenderedPrompt(
            dialect=self.name,
            prompt=prompt,
            negative_prompt=negative_prompt,
        )

    # -- subclass extension points ------------------------------------------ #

    @abstractmethod
    def _compose_clauses(self, shot: ShotDescription) -> list[str]:
        """Return the positive prompt as priority-ordered clauses (most important first)."""

    @abstractmethod
    def _negative_terms(self, shot: ShotDescription) -> list[str]:
        """Return the de-duplicated negative terms for this model (may be empty)."""

    # -- shared helpers a subclass may reuse or override -------------------- #

    def _format_negative(self, terms: list[str]) -> str:
        """Join negative ``terms`` for the native channel, within its budget.

        Default is a comma-joined list (the DashScope/Wan convention). Dialects
        whose negative field wants different framing override this.
        """
        from .compress import join_within

        return join_within(terms, self.spec.negative.budget, separator=", ")

    def _fold_negative_clause(self, terms: list[str]) -> str:
        """Phrase negatives as an in-prompt avoid-clause for a model without a channel.

        Capped at a handful of terms so it never dominates a short prompt.
        """
        head = ", ".join(terms[:6])
        return f"avoid {head}" if head else ""


__all__ = [
    "DEFAULT_PROMPT_BUDGET",
    "DialectSpec",
    "NegativeStyle",
    "PromptDialect",
    "RenderedPrompt",
]
