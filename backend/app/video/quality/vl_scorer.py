"""The :class:`VlScorer` seam — the judgments only a vision-language model can make.

Frame statistics (``features.py``) catch *mechanical* defects, but three axes need a
viewer:

* **aesthetic** — is the clip *good-looking* beyond raw sharpness/contrast;
* **prompt_adherence** — does the clip actually depict the shot's spec / prompt;
* **nsfw_flag** — a safety gate independent of quality.

These are hidden behind the async :class:`VlScorer` protocol so the harness stays
infra-free:

* :class:`RealVlScorer` adapts a real ``app.providers.vl.VLProvider`` (a single
  ``analyze_json`` call against ``qwen-vl-max`` over the sampled frames) — the *only*
  place a live model is touched, and it is never constructed in tests;
* :class:`StaticVlScorer` / :class:`ScriptedVlScorer` return canned verdicts so the
  evaluator's perception axes are deterministic with no network.

The real adapter is written but **must not** be exercised under
``KINORA_LIVE_VIDEO`` / any spend in tests; the evaluator always defaults to a fake.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from .scores import clamp01

if TYPE_CHECKING:
    from app.providers.vl import ImageInput, VLProvider


class VlVerdict(BaseModel):
    """The VL model's graded judgments for one clip (all 0..1 except the flag)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    aesthetic: float = 0.5
    prompt_adherence: float = 0.5
    nsfw_flag: bool = False
    reason: str = ""

    def normalized(self) -> VlVerdict:
        """Clamp the graded fields into 0..1 (defensive against a noisy model)."""
        return VlVerdict(
            aesthetic=clamp01(self.aesthetic),
            prompt_adherence=clamp01(self.prompt_adherence),
            nsfw_flag=self.nsfw_flag,
            reason=self.reason,
        )


@runtime_checkable
class VlScorer(Protocol):
    """Seam: score the perceptual / semantic axes for one clip's frames + prompt."""

    async def score(self, frames: Sequence[bytes], prompt: str) -> VlVerdict:
        """Return the VL verdict for the sampled ``frames`` against ``prompt``."""
        ...


_VL_INSTRUCTION = (
    "You are a strict video-quality judge. Given sampled frames of one AI-generated "
    "clip and the shot's intended prompt, return JSON with: aesthetic (0..1, how "
    "good-looking / cinematic), prompt_adherence (0..1, how well the frames depict "
    "the prompt), nsfw_flag (boolean, true only if explicit/unsafe), reason (short)."
)


@dataclass(slots=True)
class RealVlScorer:
    """Adapts a real :class:`~app.providers.vl.VLProvider` (the ONLY live seam).

    One ``analyze_json`` call over the sampled frames. Never constructed in tests; the
    evaluator defaults to a fake. Guard live use behind your own gate — this class
    does not check ``KINORA_LIVE_VIDEO`` itself (it makes no *video* call, only a VL
    chat call), so callers that must not spend should inject a fake instead.
    """

    vl: VLProvider
    model: str | None = None
    max_frames: int = 4
    max_tokens: int = 256

    async def score(self, frames: Sequence[bytes], prompt: str) -> VlVerdict:
        if not frames:
            return VlVerdict(reason="no frames")
        images: list[ImageInput] = list(frames[: self.max_frames])
        full_prompt = f"{_VL_INSTRUCTION}\n\nINTENDED PROMPT:\n{prompt}"
        raw: Any = await self.vl.analyze_json(
            images, full_prompt, model=self.model, max_tokens=self.max_tokens
        )
        data = raw if isinstance(raw, dict) else json.loads(str(raw))
        return VlVerdict(
            aesthetic=float(data.get("aesthetic", 0.5)),
            prompt_adherence=float(data.get("prompt_adherence", 0.5)),
            nsfw_flag=bool(data.get("nsfw_flag", False)),
            reason=str(data.get("reason", "")),
        ).normalized()


@dataclass(frozen=True, slots=True)
class StaticVlScorer:
    """Test seam: always return one canned verdict (ignores frames/prompt)."""

    verdict: VlVerdict

    async def score(self, frames: Sequence[bytes], prompt: str) -> VlVerdict:  # noqa: ARG002
        return self.verdict.normalized()


@dataclass(frozen=True, slots=True)
class ScriptedVlScorer:
    """Test seam: deterministic verdicts keyed by prompt substring (fallback default).

    ``by_keyword`` maps a substring → verdict; the first matching key wins, else
    ``default``. Lets a benchmark test give different providers different VL verdicts
    without any model, fully deterministically.
    """

    by_keyword: dict[str, VlVerdict]
    default: VlVerdict = VlVerdict()

    async def score(self, frames: Sequence[bytes], prompt: str) -> VlVerdict:  # noqa: ARG002
        for key, verdict in self.by_keyword.items():
            if key in prompt:
                return verdict.normalized()
        return self.default.normalized()
