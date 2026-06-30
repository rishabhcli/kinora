"""Deterministic fakes for the shadow-harness tests + their own sanity tests.

These fakes implement the injectable seams (provider, scorer) with hand-controlled
behaviour so every shadow test is deterministic and touches no network. Other
shadow test modules import these fakes.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.video.shadow.seams import FailureKind, QualityScorer, RenderOutcome, ShotSpec


class ScriptedProvider:
    """A provider whose every render is scripted per ``shot_id``.

    ``outcomes`` maps a ``shot_id`` to the exact :class:`RenderOutcome` to return; a
    ``default`` covers unscripted shots. ``raise_on`` ids raise instead (to exercise
    the runner's exception isolation). Records the order of rendered shot ids.
    """

    def __init__(
        self,
        model_id: str,
        *,
        outcomes: Mapping[str, RenderOutcome] | None = None,
        default: RenderOutcome | None = None,
        raise_on: frozenset[str] = frozenset(),
    ) -> None:
        self._model_id = model_id
        self._outcomes = dict(outcomes or {})
        self._default = default
        self._raise_on = raise_on
        self.rendered: list[str] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def render(self, spec: ShotSpec) -> RenderOutcome:
        self.rendered.append(spec.shot_id)
        if spec.shot_id in self._raise_on:
            raise RuntimeError(f"scripted boom for {spec.shot_id}")
        if spec.shot_id in self._outcomes:
            return self._outcomes[spec.shot_id]
        if self._default is not None:
            return self._default.model_copy(update={"model": self._model_id})
        return RenderOutcome(
            model=self._model_id,
            succeeded=True,
            clip_ref=f"{self._model_id}:{spec.shot_id}",
            video_seconds=spec.expected_video_seconds,
            latency_ms=1000.0,
        )


class MapScorer:
    """A scorer that returns a fixed quality per model id (no scoring logic)."""

    def __init__(
        self, by_model: Mapping[str, float], *, raise_for: frozenset[str] = frozenset()
    ) -> None:
        self._by_model = dict(by_model)
        self._raise_for = raise_for
        self.scored: list[tuple[str, str]] = []

    async def score(self, spec: ShotSpec, outcome: RenderOutcome) -> float:
        self.scored.append((spec.shot_id, outcome.model))
        if outcome.model in self._raise_for:
            raise RuntimeError(f"scorer boom for {outcome.model}")
        return self._by_model[outcome.model]


def make_outcome(
    model: str,
    *,
    quality: float | None = None,
    video_seconds: float = 5.0,
    latency_ms: float = 1000.0,
    succeeded: bool = True,
    failure: FailureKind = FailureKind.NONE,
) -> RenderOutcome:
    """Convenience builder for a :class:`RenderOutcome` in tests."""
    return RenderOutcome(
        model=model,
        succeeded=succeeded,
        failure=failure,
        clip_ref=f"{model}:ref" if succeeded else None,
        quality=quality,
        video_seconds=video_seconds if succeeded else 0.0,
        latency_ms=latency_ms,
    )


# --------------------------------------------------------------------------- #
# The fakes are themselves verified so a broken fake can't mask a real bug.
# --------------------------------------------------------------------------- #


def test_scripted_provider_satisfies_protocol() -> None:
    from app.video.shadow.seams import VideoRenderProvider

    assert isinstance(ScriptedProvider("m"), VideoRenderProvider)


def test_map_scorer_satisfies_protocol() -> None:
    assert isinstance(MapScorer({"m": 0.5}), QualityScorer)


async def test_scripted_provider_default_and_record() -> None:
    p = ScriptedProvider("cand")
    out = await p.render(ShotSpec(shot_id="s1", duration_s=4.0))
    assert out.model == "cand"
    assert out.video_seconds == 4.0
    assert p.rendered == ["s1"]
