"""The single-agent baseline arm (§13 control) — driven with mocked providers.

Confirms the control arm's defining properties: one memoryless LLM call per shot
(``qwen3.7-max``), pure frame-chaining (each keyframe conditioned ONLY on the
previous frame, never on canon references), zero video-seconds, and a
``SequenceRun`` result object comparable to the crew arm's.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.eval.baseline import BaselineArm
from app.eval.harness import DemoSequence, DemoShot, SequenceRun
from tests.conftest import FakeEmbedder


class FakeChat:
    """Records each single-agent design call and returns a canned prompt."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], str]] = []

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
    ) -> Any:
        self.calls.append((messages, model))
        return {"prompt": "a painterly forest clearing at dawn"}


class FakeImage:
    """Records frame-chaining inputs; returns deterministic per-(prompt,seed) bytes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        *,
        n: int = 1,
        reference_images: list[bytes | str] | None = None,
        seed: int | None = None,
    ) -> list[bytes]:
        self.calls.append({"prompt": prompt, "reference_images": reference_images, "seed": seed})
        return [f"keyframe:{prompt}:{seed}".encode()]


def _sequence() -> DemoSequence:
    shots = [
        DemoShot(shot_id=f"shot_{i}", scene_id="scene_1", seed=100 + i,
                 prompt=f"beat {i}", character_keys=["char_a"])
        for i in range(3)
    ]
    return DemoSequence(book_id="book_demo", shots=shots, locked_refs={"char_a": b"ref-a"})


async def test_baseline_runs_and_returns_comparable_result() -> None:
    chat, image = FakeChat(), FakeImage()
    arm = BaselineArm(chat=chat, image=image, embedder=FakeEmbedder(), settings=get_settings())

    run = await arm.run_sequence(_sequence(), run_index=0)

    assert isinstance(run, SequenceRun)
    assert run.arm == "baseline"
    assert [o.shot_id for o in run.outcomes] == ["shot_0", "shot_1", "shot_2"]
    for outcome in run.outcomes:
        assert outcome.style_embedding  # a real style embedding was measured
        assert "char_a" in outcome.character_crops  # crop embedding per character


async def test_baseline_is_single_agent_qwen_max() -> None:
    chat, image = FakeChat(), FakeImage()
    arm = BaselineArm(chat=chat, image=image, embedder=FakeEmbedder(), settings=get_settings())
    await arm.run_sequence(_sequence(), run_index=0)

    # Exactly one LLM call per shot — the single-agent control (§13).
    assert len(chat.calls) == 3
    assert all(model == get_settings().chat_model_max for _msgs, model in chat.calls)
    assert get_settings().chat_model_max == "qwen3.7-max"


async def test_baseline_is_pure_frame_chaining_no_canon_refs() -> None:
    chat, image = FakeChat(), FakeImage()
    arm = BaselineArm(chat=chat, image=image, embedder=FakeEmbedder(), settings=get_settings())
    await arm.run_sequence(_sequence(), run_index=0)

    # The first shot has no previous frame; each later shot is conditioned ONLY on
    # the previous shot's frame (frame-chaining), and NEVER on the locked ref.
    assert image.calls[0]["reference_images"] is None
    for prev_call, call in zip(image.calls, image.calls[1:], strict=False):
        refs = call["reference_images"]
        assert refs is not None and len(refs) == 1
        assert b"ref-a" not in refs  # canon locked refs are never used (no memory)
        assert refs[0] == f"keyframe:{prev_call['prompt']}:{prev_call['seed']}".encode()


async def test_baseline_falls_back_to_fixed_prompt_on_llm_error() -> None:
    class BoomChat(FakeChat):
        async def chat_json(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("LLM down")

    image = FakeImage()
    arm = BaselineArm(chat=BoomChat(), image=image, embedder=FakeEmbedder())
    run = await arm.run_sequence(_sequence(), run_index=0)
    # Still produced every shot from the fixed per-shot prompt (control never blocks).
    assert len(run.outcomes) == 3
    assert image.calls[0]["prompt"] == "beat 0"
