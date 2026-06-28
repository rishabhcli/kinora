"""Per-agent and per-shot cost attribution (kinora.md §11.1, §12.5).

The existing :class:`~app.optim.cost_meter.CostMeter` already rolls USD + physical
units up per *model* / *operation* / *book* / *session* from the ``Usage`` stream.
This module adds the two breakdowns FinOps needs that the meter doesn't:

* **per-agent** — which of the six agents (§7) drove the spend. Agents are mapped
  from the coarse ``Usage.operation`` label + the model id (a Critic is the VL
  call; the Cinematographer/Adapter are the high-volume chat calls; the Generator
  owns video + tts + keyframe image-gen). The mapping is deterministic and pure.
* **per-shot** — the cost of producing one shot, aggregated from a list of the
  ``Usage`` events recorded while rendering it (the render pipeline already
  threads a per-shot context) plus the video-seconds the budget ledger charged.

Both produce :class:`CostRollup`-shaped dictionaries so the API surface is
uniform with ``/optim/cost``. Pure + total: unpriced models cost zero, an unknown
operation attributes to the ``unknown`` agent, nothing raises.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from app.optim.cost_meter import PRICING, Price, cost_of
from app.providers.types import Usage

_ZERO = Decimal("0")


class Agent(enum.StrEnum):
    """The crew roles spend is attributed to (kinora.md §7)."""

    SHOWRUNNER = "showrunner"
    ADAPTER = "adapter"
    CINEMATOGRAPHER = "cinematographer"
    CONTINUITY = "continuity"
    GENERATOR = "generator"
    CRITIC = "critic"
    UNKNOWN = "unknown"


def attribute_agent(usage: Usage) -> Agent:
    """Map one :class:`Usage` to the crew role that drove it (deterministic).

    Operation is the primary signal (it is set by the provider layer):

    * ``video`` / ``image`` / ``tts`` -> the **Generator** owns asset production
      (the render pipeline and the keyframe lane);
    * ``vl`` -> the **Critic** (the only VL caller is the §9.5 QA scorer);
    * ``embedding`` -> **Continuity** (canon embedding / similarity, §8);
    * ``chat`` -> disambiguated by model: the orchestration model (``-max``) is the
      **Showrunner** (planning/arbitration, §7.2); otherwise the high-volume
      **Adapter** (shot planning is the dominant chat consumer, §9.1).

    An unrecognized operation attributes to :attr:`Agent.UNKNOWN`.
    """
    op = usage.operation.lower()
    if op in {"video", "image", "tts", "asr"}:
        return Agent.GENERATOR
    if op == "vl":
        return Agent.CRITIC
    if op == "embedding":
        return Agent.CONTINUITY
    if op == "chat":
        if "max" in usage.model.lower():
            return Agent.SHOWRUNNER
        return Agent.ADAPTER
    return Agent.UNKNOWN


@dataclass
class AttributedRollup:
    """A cost rollup tagged with what it is attributed to (agent or shot)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    cost_usd: Decimal = field(default_factory=lambda: _ZERO)

    def add(self, usage: Usage, cost: Decimal) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.images += usage.images
        self.audio_seconds += usage.audio_seconds
        self.video_seconds += usage.video_seconds
        self.cost_usd += cost

    def as_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "images": self.images,
            "audio_seconds": round(self.audio_seconds, 3),
            "video_seconds": round(self.video_seconds, 3),
            "cost_usd": str(self.cost_usd),
        }


def attribute_by_agent(
    usages: Iterable[Usage], pricing: Mapping[str, Price] = PRICING
) -> dict[str, dict[str, object]]:
    """Roll a stream of :class:`Usage` up per crew agent (USD + physical units)."""
    rollups: dict[Agent, AttributedRollup] = {}
    for usage in usages:
        agent = attribute_agent(usage)
        rollups.setdefault(agent, AttributedRollup()).add(usage, cost_of(usage, pricing))
    return {agent.value: rollup.as_dict() for agent, rollup in rollups.items()}


@dataclass(frozen=True, slots=True)
class ShotCost:
    """The full cost of producing one shot (the §12.5 per-shot telemetry line).

    ``video_seconds`` is what the *budget ledger* charged (the scarce currency);
    the model usages add the chat/vl/tts/image cost around it. ``cost_usd`` is the
    USD valuation across all of it (video included, via the price table).
    """

    shot_id: str
    cost_usd: Decimal
    video_seconds: float
    input_tokens: int
    output_tokens: int
    images: int
    audio_seconds: float
    calls: int
    by_agent: dict[str, dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "shot_id": self.shot_id,
            "cost_usd": str(self.cost_usd),
            "video_seconds": round(self.video_seconds, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "images": self.images,
            "audio_seconds": round(self.audio_seconds, 3),
            "calls": self.calls,
            "by_agent": self.by_agent,
        }


def attribute_shot(
    shot_id: str,
    usages: Iterable[Usage],
    *,
    charged_video_seconds: float | None = None,
    pricing: Mapping[str, Price] = PRICING,
) -> ShotCost:
    """Aggregate the cost of one shot from its :class:`Usage` events.

    ``charged_video_seconds`` overrides the video-seconds total with the authority
    of the budget ledger (the actual seconds ``commit`` charged), which is the
    number FinOps reconciles against — falling back to the sum of the usages'
    ``video_seconds`` when not supplied.
    """
    usages = list(usages)
    by_agent = attribute_by_agent(usages, pricing)
    total = AttributedRollup()
    for usage in usages:
        total.add(usage, cost_of(usage, pricing))
    video = total.video_seconds if charged_video_seconds is None else charged_video_seconds
    return ShotCost(
        shot_id=shot_id,
        cost_usd=total.cost_usd,
        video_seconds=video,
        input_tokens=total.input_tokens,
        output_tokens=total.output_tokens,
        images=total.images,
        audio_seconds=total.audio_seconds,
        calls=total.calls,
        by_agent=by_agent,
    )


class ShotCostRecorder:
    """Collect per-shot :class:`Usage` so a shot's cost can be attributed later.

    A thin, thread-unsafe-by-design helper for the render pipeline / tests: call
    :meth:`record` for every provider ``Usage`` produced while building a shot,
    then :meth:`finalize` once the budget ledger has charged its actual seconds.
    The render pipeline already threads a per-shot context; this is the place to
    hang that context's usages without touching the global cost meter.
    """

    def __init__(self, shot_id: str) -> None:
        self.shot_id = shot_id
        self._usages: list[Usage] = []

    def record(self, usage: Usage) -> None:
        self._usages.append(usage)

    @property
    def usages(self) -> list[Usage]:
        return list(self._usages)

    def finalize(
        self,
        *,
        charged_video_seconds: float | None = None,
        pricing: Mapping[str, Price] = PRICING,
    ) -> ShotCost:
        return attribute_shot(
            self.shot_id,
            self._usages,
            charged_video_seconds=charged_video_seconds,
            pricing=pricing,
        )


__all__ = [
    "Agent",
    "AttributedRollup",
    "ShotCost",
    "ShotCostRecorder",
    "attribute_agent",
    "attribute_by_agent",
    "attribute_shot",
]
