"""Literary-device detection → visual intent (metaphor/simile/symbol, §10).

Prose carries meaning in figures the camera cannot shoot literally. "Her grief
was a stone in her chest" is not a request to render a rock; it asks for the
*felt weight* — a sinking, a pressure, a heaviness in the frame. This module
detects the common figures deterministically and translates each into a concrete
``visual_intent`` the Cinematographer can stage, WITHOUT inventing entities the
canon doesn't know (the §10 guardrail): the visual intent is an instruction
about mood/motion/composition, never a new character or prop named as canon.

Detected kinds: simile ("like/as a …"), metaphor (copular "X was a Y"),
personification (inanimate subject + animate verb), symbolism (recurring loaded
nouns), and concrete sensory imagery. Heuristic + lexical; an LLM pass can add
nuance, but the deterministic detector gives the pipeline a real signal offline.
"""

from __future__ import annotations

import re

from app.agents.contracts import LiteraryDevice

from .text_utils import split_sentences, words

# "<tenor> like/as (a|an|the)? <vehicle>" — simile.
_SIMILE_RE = re.compile(
    r"\b([\w\s']{2,40}?)\s+(?:like|as)\s+(?:a|an|the)?\s*([\w][\w\s']{1,40})",
    re.IGNORECASE,
)
# "<tenor> (was|were|is|are) (a|an|the) <vehicle>" — copular metaphor.
_METAPHOR_RE = re.compile(
    r"\b([A-Za-z][\w\s']{1,30}?)\s+(?:was|were|is|are)\s+(?:a|an|the)\s+([\w][\w\s']{1,30})",
    re.IGNORECASE,
)

#: Inanimate-ish subjects whose taking an animate verb signals personification.
_INANIMATE = frozenset({
    "wind", "sun", "moon", "sea", "ocean", "river", "rain", "storm", "night",
    "darkness", "shadow", "shadows", "city", "forest", "fire", "light", "silence",
    "fog", "mist", "mountain", "mountains", "sky", "door", "house", "fear", "hope",
    "death", "time", "the wind", "the sun", "the sea", "the night", "the city",
})
_ANIMATE_VERBS = frozenset({
    "whispered", "screamed", "wept", "laughed", "danced", "crept", "reached",
    "embraced", "clutched", "watched", "waited", "sang", "sighed", "groaned",
    "breathed", "smiled", "wandered", "stared", "called", "beckoned", "devoured",
    "swallowed", "clawed", "fingered", "kissed", "mourned", "remembered",
})

#: Loaded nouns commonly carrying symbolic weight in narrative.
_SYMBOLS = {
    "rose": "a charged close-up on the rose — love/mortality, shallow focus",
    "raven": "a dark omen — the raven held in silhouette against pale sky",
    "crow": "an omen of death — a crow watching from a bare branch",
    "mirror": "a mirror motif — identity/duality, a reflected double in frame",
    "candle": "a guttering candle — fragile hope against encroaching dark",
    "river": "a river of passing time — slow downstream drift",
    "mask": "a mask motif — concealment, a face half-hidden",
    "cross": "a cross motif — sacrifice/faith, backlit",
    "snake": "a serpent motif — temptation/danger coiling in frame",
    "moon": "the moon as fate — cold light, the figure small beneath it",
    "wall": "a wall as division — the subject pressed against / divided by it",
    "key": "a key motif — access/secrecy, turned slowly in the hand",
    "blood": "blood as guilt/violence — a held, saturated close-up",
    "white": "whiteness as innocence/void — overexposed, near colourless frame",
}

#: Concrete sensory words → keep the literal image vivid (imagery, not figure).
_SENSORY = frozenset({
    "glittered", "gleamed", "shimmered", "burned", "froze", "trembled", "echoed",
    "reeked", "fragrant", "crimson", "golden", "icy", "scorching", "velvet",
    "jagged", "thunderous", "deafening", "silken", "bitter", "sweet", "acrid",
})

_STOP_TENOR = frozenset({"it", "he", "she", "they", "that", "this", "there", "here"})


def detect_devices(text: str, *, max_devices: int = 4) -> list[LiteraryDevice]:
    """Detect figures of speech in ``text`` and translate each to visual intent.

    Returns at most ``max_devices`` devices in reading order, de-duplicated by
    source phrase. Pure/deterministic — no entities are invented; a device's
    ``visual_intent`` is a staging instruction, not a new canon object.
    """
    devices: list[LiteraryDevice] = []
    seen: set[str] = set()

    for sent in split_sentences(text):
        for dev in _scan_sentence(sent.text):
            key = dev.text.strip().lower()
            if key and key not in seen:
                seen.add(key)
                devices.append(dev)
            if len(devices) >= max_devices:
                return devices
    return devices


def _scan_sentence(sentence: str) -> list[LiteraryDevice]:
    found: list[LiteraryDevice] = []

    m = _SIMILE_RE.search(sentence)
    if m:
        tenor = _clean(m.group(1))
        vehicle = _clean(m.group(2))
        if tenor and vehicle and tenor.lower() not in _STOP_TENOR:
            found.append(
                LiteraryDevice(
                    kind="simile",
                    text=m.group(0).strip(),
                    tenor=tenor,
                    vehicle=vehicle,
                    visual_intent=(
                        f"evoke '{tenor}' through the imagery of {vehicle} — "
                        f"let the framing/motion carry the comparison"
                    ),
                )
            )

    m = _METAPHOR_RE.search(sentence)
    if m:
        tenor = _clean(m.group(1))
        vehicle = _clean(m.group(2))
        distinct = tenor.lower() != vehicle.lower()
        if tenor and vehicle and tenor.lower() not in _STOP_TENOR and distinct:
            found.append(
                LiteraryDevice(
                    kind="metaphor",
                    text=m.group(0).strip(),
                    tenor=tenor,
                    vehicle=vehicle,
                    visual_intent=(
                        f"stage '{tenor}' as if it were {vehicle} — borrow the "
                        f"weight/texture/motion of {vehicle} for the mood"
                    ),
                )
            )

    pers = _personification(sentence)
    if pers is not None:
        found.append(pers)

    found.extend(_symbols(sentence))
    return found


def _personification(sentence: str) -> LiteraryDevice | None:
    toks = sentence.split()
    low = [t.strip(",.;:!?\"'").lower() for t in toks]
    for i in range(len(low) - 1):
        subj = low[i]
        verb = low[i + 1]
        two = f"the {subj}" if subj else subj
        if (subj in _INANIMATE or two in _INANIMATE) and verb in _ANIMATE_VERBS:
            return LiteraryDevice(
                kind="personification",
                text=f"{subj} {verb}",
                tenor=subj,
                vehicle="a living agent",
                visual_intent=(
                    f"give the {subj} agency in frame — motion/behaviour that "
                    f"reads as if it '{verb}', not a static element"
                ),
            )
    return None


def _symbols(sentence: str) -> list[LiteraryDevice]:
    toks = set(words(sentence))
    out: list[LiteraryDevice] = []
    for sym, intent in _SYMBOLS.items():
        if sym in toks:
            out.append(
                LiteraryDevice(
                    kind="symbol", text=sym, tenor=sym, vehicle="", visual_intent=intent
                )
            )
    return out


def _clean(fragment: str) -> str:
    return " ".join(fragment.split()).strip(" ,;:")


def visual_intent_summary(devices: list[LiteraryDevice]) -> str:
    """One-line concatenation of device visual intents (for prompt conditioning)."""
    intents = [d.visual_intent for d in devices if d.visual_intent]
    return "; ".join(intents)


__all__ = ["detect_devices", "visual_intent_summary"]
