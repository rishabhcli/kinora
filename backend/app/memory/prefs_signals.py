"""Director-note → preference-signal inference + plain-language priors (§8.6).

The Director never edits a "pacing prior" by hand — they leave a note ("slower",
"warmer", "pull back wider"). This module is the deterministic, unit-testable
bridge that the cross-session preference loop turns on:

* :func:`infer_signals` reads a free-text note and yields the ``(axis, direction)``
  signals it implies — "slow it down" → ``("pacing", -1)``, "warmer" →
  ``("palette", +1)``. :func:`infer_signals_from_changes` does the same for a
  canon-edit payload (a re-coloured coat shifts the palette prior).
* :func:`camera_overrides` / :func:`prompt_hints` turn the *accumulated* priors
  back into concrete shot defaults the Cinematographer applies on the next
  session, so the system directs in the reader's taste without being asked.
* :func:`describe` renders a prior as the plain-language label the "Your
  directing style" panel shows ("You prefer slower shots", "Warmer palette
  bias +0.3").

Each signal nudges a single signed ``bias`` per axis by :data:`SIGNAL_STEP`
(±0.3) and accumulates an evidence ``weight``: opposing notes cancel, repeated
notes reinforce. A prior only becomes a *default the agent applies* once its
magnitude clears :data:`APPLY_THRESHOLD` — one stray note never reprograms the
film, a repeated pattern does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid a runtime import cycle with prefs_service
    from app.memory.prefs_service import PreferencePrior, PreferencePriors

#: How far one Director signal moves an axis's signed bias.
SIGNAL_STEP = 0.3
#: Bias is clamped to ``[-BIAS_CLAMP, +BIAS_CLAMP]`` so it can't run away.
BIAS_CLAMP = 1.5
#: A prior only becomes an applied *default* once ``abs(bias) >= APPLY_THRESHOLD``.
APPLY_THRESHOLD = 0.5
#: Half-life (days) for the recency decay of an un-reinforced prior (§8.5 ethos):
#: a taste you stop expressing slowly fades back toward neutral rather than
#: ruling the film forever. Long, so an active session is read at ~full strength.
DECAY_HALF_LIFE_DAYS = 45.0
_SECONDS_PER_DAY = 86_400.0

_WORD_RE = re.compile(r"[a-z]+")


def decay_factor(age_seconds: float, *, half_life_days: float = DECAY_HALF_LIFE_DAYS) -> float:
    """Exponential recency weight in ``(0, 1]`` for a signal ``age_seconds`` old.

    ``1.0`` for a fresh signal, ``0.5`` after one half-life, etc. Negative ages
    (clock skew) clamp to ``1.0``. This is the "timely forgetting" of §8.5 applied
    to preferences: read-time decay, so nothing is destroyed — a stale prior just
    stops dominating until it is expressed again.
    """
    if age_seconds <= 0 or half_life_days <= 0:
        return 1.0
    return float(0.5 ** (age_seconds / (half_life_days * _SECONDS_PER_DAY)))


@dataclass(frozen=True, slots=True)
class _Axis:
    """One learnable axis of directing style (pacing / palette / composition)."""

    kind: str
    #: The ``camera`` field this axis defaults, or ``None`` (palette → prompt).
    camera_field: str | None
    #: Categorical value applied at a strong negative / positive bias.
    low_value: str | None
    high_value: str | None
    #: Note phrases that push the bias negative / positive. Single words match a
    #: whole token; multi-word phrases match as a substring.
    low_phrases: tuple[str, ...]
    high_phrases: tuple[str, ...]
    #: The noun phrase the panel reads at a negative / positive bias, e.g.
    #: "slower, lingering shots" → "You prefer slower, lingering shots".
    low_concept: str
    high_concept: str
    #: Headline shown for a palette-style axis ("Warmer palette bias"); the signed
    #: magnitude is appended ("… +0.3").
    low_headline: str = ""
    high_headline: str = ""
    #: Prompt fragments appended for a palette-style axis (else ``None``).
    prompt_low: str | None = None
    prompt_high: str | None = None
    #: When true the label carries the signed magnitude ("… bias +0.3").
    numeric_label: bool = False
    #: Compact directive handed to the model as a default.
    low_directive: str = ""
    high_directive: str = ""


_AXES: tuple[_Axis, ...] = (
    _Axis(
        kind="pacing",
        camera_field="speed",
        low_value="slow",
        high_value="fast",
        low_phrases=(
            "slower",
            "slow down",
            "slow it down",
            "too fast",
            "too quick",
            "linger",
            "let it breathe",
            "hold the shot",
            "more time",
            "draw it out",
            "drawn out",
            "unhurried",
        ),
        high_phrases=(
            "faster",
            "speed up",
            "speed it up",
            "too slow",
            "snappier",
            "quicker",
            "pick up the pace",
            "more energy",
            "cut faster",
            "punchy",
        ),
        low_concept="slower, lingering shots",
        high_concept="faster, snappier pacing",
        low_directive="slower camera moves",
        high_directive="faster camera moves",
    ),
    _Axis(
        kind="palette",
        camera_field=None,
        low_value="cool",
        high_value="warm",
        low_phrases=(
            "cooler",
            "colder",
            "cool palette",
            "cold tones",
            "bluer",
            "more blue",
            "desaturate",
            "desaturated",
            "muted",
        ),
        high_phrases=(
            "warmer",
            "warm palette",
            "warm tones",
            "warmer tones",
            "golden",
            "golden hour",
            "more saturated",
            "richer color",
            "richer colour",
        ),
        low_concept="a cooler, muted palette",
        high_concept="a warmer palette",
        low_headline="Cooler palette bias",
        high_headline="Warmer palette bias",
        prompt_low="a cooler, more muted color palette",
        prompt_high="a warmer color palette",
        numeric_label=True,
        low_directive="a cooler palette",
        high_directive="a warmer palette",
    ),
    _Axis(
        kind="composition",
        camera_field="shot_size",
        low_value="close",
        high_value="wide",
        low_phrases=(
            "closer",
            "close up",
            "close-up",
            "tighter",
            "tighten",
            "more intimate",
            "push in closer",
        ),
        high_phrases=(
            "wider",
            "wide shot",
            "pull back",
            "pull out",
            "zoom out",
            "establishing",
            "more of the scene",
            "step back",
        ),
        low_concept="tighter, closer framing",
        high_concept="wider, establishing framing",
        low_directive="tighter framing",
        high_directive="wider framing",
    ),
    _Axis(
        kind="lighting",
        camera_field=None,
        low_value="dark",
        high_value="bright",
        low_phrases=(
            "darker",
            "dimmer",
            "low key",
            "low-key",
            "moodier",
            "shadowy",
            "more shadow",
            "gloomier",
        ),
        high_phrases=(
            "brighter",
            "lighter",
            "high key",
            "high-key",
            "more light",
            "sunnier",
            "well lit",
            "luminous",
        ),
        low_concept="darker, moodier lighting",
        high_concept="brighter, high-key lighting",
        prompt_low="darker, low-key, moody lighting",
        prompt_high="brighter, high-key lighting",
        low_directive="darker lighting",
        high_directive="brighter lighting",
    ),
    _Axis(
        kind="energy",
        camera_field=None,
        low_value="calm",
        high_value="dramatic",
        low_phrases=(
            "calmer",
            "quieter",
            "gentler",
            "subdued",
            "understated",
            "less dramatic",
            "more restrained",
        ),
        high_phrases=(
            "more dramatic",
            "more intense",
            "bolder",
            "epic",
            "heightened",
            "high stakes",
            "more cinematic",
        ),
        low_concept="a calmer, understated mood",
        high_concept="a bolder, more dramatic mood",
        prompt_low="a calm, understated, restrained mood",
        prompt_high="a heightened, dramatic, cinematic mood",
        low_directive="a calmer mood",
        high_directive="a more dramatic mood",
    ),
)

_AXES_BY_KIND: dict[str, _Axis] = {axis.kind: axis for axis in _AXES}

#: The directing-style axes, in display order — the panel renders these.
AXIS_KINDS: tuple[str, ...] = tuple(axis.kind for axis in _AXES)


# --------------------------------------------------------------------------- #
# Inference: free text -> (axis, direction) signals
# --------------------------------------------------------------------------- #


def _matches(text_lower: str, tokens: frozenset[str], phrase: str) -> bool:
    """Match a single token against word boundaries; a phrase as a substring."""
    if " " in phrase or "-" in phrase:
        return phrase in text_lower
    return phrase in tokens


def infer_signals(note: str) -> list[tuple[str, int]]:
    """Infer the ``(kind, direction)`` signals a free-text note implies.

    Direction is ``-1`` (toward ``low_value``) or ``+1`` (toward ``high_value``).
    An axis that is pushed both ways at once (e.g. "warmer but cooler shadows")
    is ambiguous and yields no signal — only a clear nudge teaches a prior.
    """
    text_lower = note.lower()
    tokens = frozenset(_WORD_RE.findall(text_lower))
    signals: list[tuple[str, int]] = []
    for axis in _AXES:
        low = any(_matches(text_lower, tokens, p) for p in axis.low_phrases)
        high = any(_matches(text_lower, tokens, p) for p in axis.high_phrases)
        if low and not high:
            signals.append((axis.kind, -1))
        elif high and not low:
            signals.append((axis.kind, +1))
    return signals


def infer_signals_from_changes(changes: dict[str, Any]) -> list[tuple[str, int]]:
    """Infer signals from a canon-edit payload (§8.6: a re-coloured/re-framed edit).

    The edit's text-ish values are flattened into one blob and run through the
    same note inference, so "make her coat a warmer red" shifts the palette prior
    exactly as a Director note would.
    """
    return infer_signals(_flatten_text(changes))


def _flatten_text(value: object) -> str:
    """Collect the string leaves of a (possibly nested) JSON-ish value."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_text(v) for v in value)
    return ""


# --------------------------------------------------------------------------- #
# Bias arithmetic (accumulation across signals)
# --------------------------------------------------------------------------- #


def merge_bias(old_bias: float, direction: int, step: float = SIGNAL_STEP) -> float:
    """Nudge ``old_bias`` by one signal's worth, clamped to ``±BIAS_CLAMP``."""
    nudged = old_bias + (step if direction >= 0 else -step)
    return max(-BIAS_CLAMP, min(BIAS_CLAMP, round(nudged, 4)))


def bias_of(prior: PreferencePrior | None) -> float:
    """The signed bias stored on a prior (0.0 when absent or non-numeric)."""
    if prior is None:
        return 0.0
    raw = prior.value.get("bias") if isinstance(prior.value, dict) else None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _prior(priors: PreferencePriors | None, kind: str) -> PreferencePrior | None:
    if priors is None:
        return None
    return priors.priors.get(kind)


# --------------------------------------------------------------------------- #
# Priors -> shot defaults (consumed by the Cinematographer)
# --------------------------------------------------------------------------- #


def categorical(kind: str, bias: float) -> str | None:
    """The applied categorical value for an axis at ``bias`` (``None`` if weak)."""
    axis = _AXES_BY_KIND.get(kind)
    if axis is None:
        return None
    if bias <= -APPLY_THRESHOLD:
        return axis.low_value
    if bias >= APPLY_THRESHOLD:
        return axis.high_value
    return None


def camera_overrides(
    priors: PreferencePriors | None, *, skip: frozenset[str] = frozenset()
) -> dict[str, str]:
    """Camera-field defaults implied by the priors (``speed``, ``shot_size``).

    Axes in ``skip`` (those the *current* notes already speak to) are left to the
    in-session direction — a learned default never overrides an explicit ask.
    """
    out: dict[str, str] = {}
    for axis in _AXES:
        if axis.camera_field is None or axis.kind in skip:
            continue
        value = categorical(axis.kind, bias_of(_prior(priors, axis.kind)))
        if value is not None:
            out[axis.camera_field] = value
    return out


def prompt_hints(
    priors: PreferencePriors | None, *, skip: frozenset[str] = frozenset()
) -> list[str]:
    """Prompt fragments implied by non-camera priors (the palette default)."""
    hints: list[str] = []
    for axis in _AXES:
        if axis.prompt_low is None or axis.kind in skip:
            continue
        bias = bias_of(_prior(priors, axis.kind))
        if bias <= -APPLY_THRESHOLD and axis.prompt_low:
            hints.append(axis.prompt_low)
        elif bias >= APPLY_THRESHOLD and axis.prompt_high:
            hints.append(axis.prompt_high)
    return hints


def preferences_payload(priors: PreferencePriors | None) -> dict[str, str]:
    """Compact ``{kind: directive}`` of the *applied* priors, for the LLM prior."""
    out: dict[str, str] = {}
    for axis in _AXES:
        bias = bias_of(_prior(priors, axis.kind))
        if bias <= -APPLY_THRESHOLD and axis.low_directive:
            out[axis.kind] = axis.low_directive
        elif bias >= APPLY_THRESHOLD and axis.high_directive:
            out[axis.kind] = axis.high_directive
    return out


#: Non-camera axes whose applied value also drives a visible ffmpeg grade on the
#: off-gate degradation lane (so palette/lighting are *seen*, not just prompted).
_GRADE_KINDS: tuple[str, ...] = ("palette", "lighting")


def grade_for(priors: PreferencePriors | None) -> dict[str, str]:
    """Applied categoricals for the gradeable axes (``palette`` / ``lighting``).

    e.g. ``{"palette": "warm", "lighting": "dark"}`` — the degradation lane turns
    these into a real colour/brightness grade, so the learned look is on screen
    even with ``KINORA_LIVE_VIDEO`` off.
    """
    out: dict[str, str] = {}
    for kind in _GRADE_KINDS:
        value = categorical(kind, bias_of(_prior(priors, kind)))
        if value is not None:
            out[kind] = value
    return out


# --------------------------------------------------------------------------- #
# Priors -> plain language (consumed by the Settings panel)
# --------------------------------------------------------------------------- #


def describe(prior: PreferencePrior) -> tuple[str, str]:
    """Render a prior as a ``(label, detail)`` pair for the directing-style panel.

    A strong prior reads as a statement ("You prefer slower, lingering shots"); a
    weak one reads as a lean ("Leaning toward wider, establishing framing"). A
    palette prior always leads with its signed magnitude ("Warmer palette bias
    +0.3") — the number the goal asks the panel to show.
    """
    axis = _AXES_BY_KIND.get(prior.kind)
    bias = bias_of(prior)
    edits = int(round(prior.weight))
    evidence = f"learned from {edits} director {'edit' if edits == 1 else 'edits'}"
    applied = abs(bias) >= APPLY_THRESHOLD
    if axis is None or bias == 0.0:
        label = "No preference learned yet" if bias == 0.0 else prior.kind
        return label, evidence
    if axis.numeric_label:
        headline = axis.high_headline if bias > 0 else axis.low_headline
        label = f"{headline} {bias:+.1f}"
    else:
        concept = axis.high_concept if bias > 0 else axis.low_concept
        label = f"You prefer {concept}" if applied else f"Leaning toward {concept}"
    detail = evidence if applied else f"{evidence} · not yet applied"
    return label, detail


def is_applied(prior: PreferencePrior) -> bool:
    """Whether this prior is strong enough to be applied as a default."""
    return abs(bias_of(prior)) >= APPLY_THRESHOLD


def applied_value(prior: PreferencePrior) -> str | None:
    """The categorical default this prior applies, if any (``slow`` / ``wide`` …)."""
    return categorical(prior.kind, bias_of(prior))


__all__ = [
    "APPLY_THRESHOLD",
    "AXIS_KINDS",
    "BIAS_CLAMP",
    "DECAY_HALF_LIFE_DAYS",
    "SIGNAL_STEP",
    "applied_value",
    "bias_of",
    "camera_overrides",
    "categorical",
    "decay_factor",
    "describe",
    "grade_for",
    "infer_signals",
    "infer_signals_from_changes",
    "is_applied",
    "merge_bias",
    "preferences_payload",
    "prompt_hints",
]
