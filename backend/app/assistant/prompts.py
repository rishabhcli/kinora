"""Prompt construction — grounded, citation-forced, spoiler-safe (kinora.md §8).

The assistant's whole value is that it is *grounded*: every claim must trace to a
retrieved span, and it must never reveal the future. This module turns an
assembled context block + the reader's question into the chat messages that
enforce that contract:

* a **system prompt** that fixes the rules — answer ONLY from the numbered
  context, cite every sentence with ``[n]`` markers, refuse when the context is
  insufficient, and never use outside knowledge of the book;
* a **per-intent user prompt** that frames the task (who-is / explain / recap /
  state / general) and embeds the context block and the question;
* an optional **conversation preamble** (prior turns) so follow-ups have memory.

It also defines the **JSON answer contract** the synthesizer asks for, so the
model returns the prose plus a structured citation list we can validate — with a
plain-text fallback the guard can still parse from inline ``[n]`` markers.

Pure string assembly: no provider, no DB. The synthesizer (``synth.py``) feeds
these messages to the chat seam.
"""

from __future__ import annotations

from app.assistant.context import AssembledContext
from app.assistant.types import AssistantIntent, ConversationTurn

#: The sentinel an answer uses when the context can't support a grounded reply.
REFUSAL_SENTINEL = "I can't answer that from what you've read so far."

_SYSTEM_PROMPT = f"""\
You are Kinora's reading companion. You answer a reader's questions about the \
book they are CURRENTLY reading, grounded strictly in the numbered CONTEXT \
passages provided.

Hard rules:
1. Use ONLY the numbered CONTEXT. Do not use any outside knowledge of this book, \
its author, adaptations, or how the story ends.
2. Cite your sources: end every factual sentence with one or more markers like \
[1] or [2][3] pointing to the CONTEXT lines you used.
3. The CONTEXT contains ONLY what the reader has already read. Never hint at, \
foreshadow, or reveal anything that happens later in the book.
4. If the CONTEXT does not contain enough to answer, say exactly: \
"{REFUSAL_SENTINEL}" — do not guess.
5. Be concise and concrete. Quote sparingly. Do not invent names, events, or \
details that are not in the CONTEXT.
"""

#: Per-intent framing prepended to the user turn (keeps the model on task).
_INTENT_FRAMING: dict[AssistantIntent, str] = {
    AssistantIntent.WHO_IS: (
        "The reader wants to know about a character/place/thing. Describe it using "
        "only the CONTEXT, focusing on identity, role, and appearance so far."
    ),
    AssistantIntent.EXPLAIN: (
        "The reader wants this passage or moment explained. Clarify what is "
        "happening and why, grounded only in the CONTEXT, without speculating "
        "about what it will lead to."
    ),
    AssistantIntent.RECAP: (
        "The reader wants a recap of what has happened so far. Synthesize the "
        "CONTEXT into a brief chronological summary up to their current position. "
        "Do not include anything beyond it."
    ),
    AssistantIntent.STATE: (
        "The reader is asking about the current situation. Answer with the state "
        "as of where they are now, using only the CONTEXT."
    ),
    AssistantIntent.GENERAL: (
        "Answer the reader's question using only the CONTEXT."
    ),
}

#: The JSON shape the synthesizer requests (prose + structured citations).
JSON_ANSWER_INSTRUCTION = (
    'Reply with ONLY a JSON object: {"answer": "<prose with inline [n] markers>", '
    '"citations": [<integer markers you used>], "refused": <true|false>}. '
    "Set refused=true and leave citations empty if the CONTEXT is insufficient."
)


def build_system_prompt() -> str:
    """The fixed grounding/spoiler/citation contract (the system message)."""
    return _SYSTEM_PROMPT


def build_user_prompt(
    question: str,
    context: AssembledContext,
    *,
    intent: AssistantIntent = AssistantIntent.GENERAL,
    require_json: bool = True,
) -> str:
    """Assemble the user turn: framing + numbered context + question (+ JSON ask)."""
    framing = _INTENT_FRAMING.get(intent, _INTENT_FRAMING[AssistantIntent.GENERAL])
    context_block = context.block or "(no relevant passages were found)"
    parts = [
        framing,
        "",
        "CONTEXT:",
        context_block,
        "",
        f"QUESTION: {question.strip()}",
    ]
    if require_json:
        parts += ["", JSON_ANSWER_INSTRUCTION]
    return "\n".join(parts)


def build_messages(
    question: str,
    context: AssembledContext,
    *,
    intent: AssistantIntent = AssistantIntent.GENERAL,
    history: list[ConversationTurn] | None = None,
    require_json: bool = True,
) -> list[dict[str, str]]:
    """Build the full chat message list (system + history + user)."""
    messages: list[dict[str, str]] = [{"role": "system", "content": build_system_prompt()}]
    for turn in history or []:
        role = "assistant" if turn.role == "assistant" else "user"
        messages.append({"role": role, "content": turn.content})
    messages.append(
        {
            "role": "user",
            "content": build_user_prompt(
                question, context, intent=intent, require_json=require_json
            ),
        }
    )
    return messages


def is_refusal(text: str) -> bool:
    """True when the model's reply is the refusal sentinel (grounding failure)."""
    return REFUSAL_SENTINEL.lower() in text.strip().lower()


__all__ = [
    "JSON_ANSWER_INSTRUCTION",
    "REFUSAL_SENTINEL",
    "build_messages",
    "build_system_prompt",
    "build_user_prompt",
    "is_refusal",
]
