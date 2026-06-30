"""Concrete prompt dialects — one per video model family.

Each module renders the canonical :class:`~app.video.prompts.canonical.ShotDescription`
into that model's best prompt:

* :mod:`.wan` — Wan 2.x (DashScope). Bit-faithful to
  :func:`app.agents.generator.compose_wan_prompt` so the current render path is
  unchanged. The reference dialect.
* :mod:`.runway` — Runway Gen-3 (free-text, structured camera, no negative channel).
* :mod:`.pika` — Pika (very short prompt + ``-camera``/``-neg`` parameters).
* :mod:`.kling` — Kling (short prompt + dedicated negative prompt).
* :mod:`.luma` — Luma Dream Machine (natural-language motion, no negative channel).
* :mod:`.veo` — Google Veo (long, paragraph-style cinematic prose + negatives).
* :mod:`.sora` — OpenAI Sora (long descriptive prose, no negative channel).
* :mod:`.generic` — an open/neutral dialect for any other model.

Importing this package is side-effect free; the registry imports each concrete
dialect explicitly.
"""

from __future__ import annotations

__all__: list[str] = []
