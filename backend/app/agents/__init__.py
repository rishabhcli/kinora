"""The crew — six agents over the shared canon, behind typed contracts (§7).

Each agent is a thin, stateless service whose only shared dependency is the MCP
canon memory; every one sits behind a JSON request/response schema
(:mod:`app.agents.contracts`) and a versioned system prompt
(:mod:`app.agents.prompts`). The creative judgement lives in the models; the
*policy* lives in pure, deterministic functions so it is unit-testable without a
network:

* the Cinematographer's §9.3 render-mode tree — :func:`decide_render_mode`;
* the Critic's §9.5 thresholds + repair routing — :func:`decide_qa`;
* the Showrunner's §7.2 conflict-arbitration policy — :func:`decide_arbitration`;
* the Continuity Supervisor's §7.2 conflict construction — :func:`build_conflict`.

The Generator is the one non-LLM member: the real Wan + CosyVoice render bridge.

The Showrunner additionally owns **series-scale showrunning** (§7) — cross-book
canon, multi-volume arc tracking, pacing curves, episode/act structure, recaps,
motif planning, and a richer weighed arbitration. That policy lives, in the same
pure-function spirit, in the :mod:`app.agents.series` package, re-exported here.
"""

from __future__ import annotations

from . import series
from .adapter import Adapter
from .base import BaseAgent
from .cinematographer import Cinematographer, RenderModeInputs, decide_render_mode
from .continuity import Continuity, build_conflict
from .critic import Critic, QAThresholds, decide_qa
from .generator import Generator, GeneratorOutput, build_wan_spec, wan_mode_for
from .prompts import VersionedPrompt
from .showrunner import Showrunner, decide_arbitration

__all__ = [
    "Adapter",
    "BaseAgent",
    "Cinematographer",
    "Continuity",
    "Critic",
    "Generator",
    "GeneratorOutput",
    "QAThresholds",
    "RenderModeInputs",
    "Showrunner",
    "VersionedPrompt",
    "build_conflict",
    "build_wan_spec",
    "decide_arbitration",
    "decide_qa",
    "decide_render_mode",
    "series",
    "wan_mode_for",
]
