"""Prompt-injection + jailbreak defense — input sanitization.

Kinora feeds *book text* (and director comments) into the crew's prompts. Book
text is attacker-controllable in the general case (a malicious PDF, or simply a
novel that quotes an "ignore your instructions" line) and a director comment is
free-form user input. This module is the **input** half of the guardrail layer:
it scans untrusted text for injection / jailbreak patterns, scores the risk, and
returns a sanitized rendition (delimited + suspicious spans optionally redacted)
that an agent can safely embed as data rather than instructions.

Design notes:

* **Detection is layered.** Regex *signatures* catch the well-known phrasings
  ("ignore previous instructions", "you are now DAN", system-prompt exfiltration,
  fake role headers, tool-call smuggling). A handful of *heuristics* catch the
  shape of an attack without an exact phrase (an imperative addressed at "you",
  base64-looking blobs, an unusual density of instruction verbs).
* **Scoring is additive + capped** at 1.0, with named contributions so a reviewer
  can see *why* a span scored high. The categories map to MITRE-style buckets.
* **Sanitization is non-destructive by default** (it wraps untrusted text in a
  data fence and neutralizes role headers); ``redact=True`` masks the matched
  injection spans.

Pure module — no model calls, no app imports beyond the package error type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class InjectionCategory(StrEnum):
    """Buckets a matched signal falls into."""

    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_HIJACK = "role_hijack"
    SYSTEM_EXFIL = "system_exfiltration"
    JAILBREAK_PERSONA = "jailbreak_persona"
    TOOL_SMUGGLING = "tool_smuggling"
    ENCODING_OBFUSCATION = "encoding_obfuscation"
    DELIMITER_INJECTION = "delimiter_injection"


@dataclass(frozen=True, slots=True)
class Signature:
    """A weighted regex signature for one injection pattern."""

    name: str
    category: InjectionCategory
    pattern: re.Pattern[str]
    weight: float


def _sig(name: str, category: InjectionCategory, pattern: str, weight: float) -> Signature:
    return Signature(name, category, re.compile(pattern, re.IGNORECASE), weight)


#: The signature table. Weights are tuned so a single strong match (≥ 0.5) trips
#: the default block threshold while a lone weak heuristic does not.
SIGNATURES: tuple[Signature, ...] = (
    _sig(
        "ignore_previous",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|"
        r"all)\b.{0,20}\b(instructions?|prompts?|rules?|directions?)\b",
        0.6,
    ),
    _sig(
        "new_instructions",
        InjectionCategory.INSTRUCTION_OVERRIDE,
        r"\b(new|updated|real|actual)\b\s+(instructions?|task|rules?)\s*:",
        0.45,
    ),
    _sig(
        "do_anything_now",
        InjectionCategory.JAILBREAK_PERSONA,
        r"\b(do anything now|DAN mode|developer mode|jailbreak|unfiltered|no restrictions?)\b",
        0.6,
    ),
    _sig(
        "you_are_now",
        InjectionCategory.JAILBREAK_PERSONA,
        r"\byou are (now|no longer)\b.{0,40}\b(an?|the)\b",
        0.4,
    ),
    _sig(
        "reveal_system",
        InjectionCategory.SYSTEM_EXFIL,
        r"\b(reveal|print|repeat|show|output|disclose|leak)\b.{0,30}\b(system|developer|"
        r"initial)\b.{0,15}\b(prompt|instructions?|message)\b",
        0.65,
    ),
    _sig(
        "role_header",
        InjectionCategory.ROLE_HIJACK,
        r"^\s*(system|assistant|developer)\s*[:>\]]",
        0.5,
    ),
    _sig(
        "chatml_tags",
        InjectionCategory.DELIMITER_INJECTION,
        r"<\|?(im_start|im_end|system|endoftext)\|?>",
        0.55,
    ),
    _sig(
        "tool_call_smuggle",
        InjectionCategory.TOOL_SMUGGLING,
        r"\b(call|invoke|execute|run)\b.{0,20}\b(tool|function|skill|shot\.render|canon\."
        r"\w+)\b",
        0.4,
    ),
    _sig(
        "pretend_ignore_safety",
        InjectionCategory.JAILBREAK_PERSONA,
        r"\b(pretend|imagine|hypothetically)\b.{0,40}\b(no|without|ignore)\b.{0,20}"
        r"\b(rules?|guidelines?|safety|filter)\b",
        0.45,
    ),
)

#: Verbs whose dense co-occurrence (addressed at "you") suggests a covert command.
_IMPERATIVE_VERBS = frozenset(
    {
        "ignore",
        "stop",
        "disregard",
        "reveal",
        "print",
        "output",
        "repeat",
        "execute",
        "run",
        "obey",
        "comply",
        "respond",
        "answer",
        "do",
    }
)

_BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
_YOU_RE = re.compile(r"\byou\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InjectionMatch:
    """One detected signal with its location in the scanned text."""

    name: str
    category: InjectionCategory
    weight: float
    span: tuple[int, int]
    excerpt: str


@dataclass(frozen=True, slots=True)
class InjectionScan:
    """The result of scanning a piece of untrusted text."""

    score: float
    matches: tuple[InjectionMatch, ...]
    categories: tuple[InjectionCategory, ...]
    is_suspicious: bool

    @property
    def top_category(self) -> InjectionCategory | None:
        if not self.matches:
            return None
        return max(self.matches, key=lambda m: m.weight).category


@dataclass
class InjectionScanner:
    """Scans untrusted text for prompt-injection / jailbreak signals."""

    #: Score at/above which text is flagged suspicious.
    threshold: float = 0.5
    signatures: tuple[Signature, ...] = field(default=SIGNATURES)
    #: Heuristic weight applied when the imperative-verb density is high.
    heuristic_weight: float = 0.3

    def scan(self, text: str) -> InjectionScan:
        matches: list[InjectionMatch] = []
        for sig in self.signatures:
            for m in sig.pattern.finditer(text):
                excerpt = text[m.start() : min(m.end(), m.start() + 80)]
                matches.append(
                    InjectionMatch(
                        name=sig.name,
                        category=sig.category,
                        weight=sig.weight,
                        span=(m.start(), m.end()),
                        excerpt=excerpt.strip(),
                    )
                )
        matches.extend(self._heuristics(text))
        score = min(1.0, sum(m.weight for m in matches))
        categories = tuple(dict.fromkeys(m.category for m in matches))
        return InjectionScan(
            score=round(score, 4),
            matches=tuple(matches),
            categories=categories,
            is_suspicious=score >= self.threshold,
        )

    def _heuristics(self, text: str) -> list[InjectionMatch]:
        out: list[InjectionMatch] = []
        # 1) A long base64-ish blob is a classic obfuscated-payload tell.
        for m in _BASE64_RE.finditer(text):
            out.append(
                InjectionMatch(
                    name="base64_blob",
                    category=InjectionCategory.ENCODING_OBFUSCATION,
                    weight=0.25,
                    span=(m.start(), m.end()),
                    excerpt=text[m.start() : m.start() + 40],
                )
            )
        # 2) High density of imperative verbs aimed at "you" within a short window.
        if _YOU_RE.search(text):
            words = re.findall(r"[a-z']+", text.lower())
            if words:
                density = sum(1 for w in words if w in _IMPERATIVE_VERBS) / len(words)
                if density >= 0.12 and len(words) >= 6:
                    out.append(
                        InjectionMatch(
                            name="imperative_density",
                            category=InjectionCategory.INSTRUCTION_OVERRIDE,
                            weight=self.heuristic_weight,
                            span=(0, len(text)),
                            excerpt=text[:60],
                        )
                    )
        return out


# --------------------------------------------------------------------------- #
# Sanitization
# --------------------------------------------------------------------------- #

#: A fence that marks text as DATA, not instructions, when embedded in a prompt.
DATA_FENCE_OPEN = "<<<UNTRUSTED_DATA"
DATA_FENCE_CLOSE = "UNTRUSTED_DATA>>>"

_ROLE_HEADER_RE = re.compile(r"(?im)^\s*(system|assistant|developer)\s*([:>\]])")
_CHATML_RE = re.compile(r"<\|?(?:im_start|im_end|system|endoftext)\|?>", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SanitizedText:
    """Sanitized text plus the scan that justified the treatment."""

    text: str
    scan: InjectionScan
    redacted: bool
    fenced: bool


def neutralize_delimiters(text: str) -> str:
    """Defang role headers and ChatML control tokens so they read as plain data."""
    text = _ROLE_HEADER_RE.sub(r"[\1]\2", text)  # `system:` -> `[system]:`
    text = _CHATML_RE.sub("[redacted-control-token]", text)
    return text


def fence(text: str) -> str:
    """Wrap text in the untrusted-data fence (the agent treats the body as data)."""
    return f"{DATA_FENCE_OPEN}\n{text}\n{DATA_FENCE_CLOSE}"


def sanitize(
    text: str,
    *,
    scanner: InjectionScanner | None = None,
    redact: bool = False,
    add_fence: bool = True,
) -> SanitizedText:
    """Scan + neutralize untrusted text for safe embedding in a prompt.

    Always neutralizes role headers / control tokens. ``redact`` masks the matched
    injection spans (most aggressive). ``add_fence`` wraps the result in the
    untrusted-data fence so the model treats it as data. Returns the scan so the
    caller can still block on a high score.
    """
    scanner = scanner or InjectionScanner()
    scan = scanner.scan(text)
    body = neutralize_delimiters(text)
    if redact and scan.matches:
        # Redact in descending start order so earlier spans keep their offsets.
        for m in sorted(scan.matches, key=lambda x: x.span[0], reverse=True):
            start, end = m.span
            body = body[:start] + "[redacted-injection]" + body[end:]
    if add_fence:
        body = fence(body)
    return SanitizedText(
        text=body, scan=scan, redacted=redact and bool(scan.matches), fenced=add_fence
    )


__all__ = [
    "DATA_FENCE_CLOSE",
    "DATA_FENCE_OPEN",
    "SIGNATURES",
    "InjectionCategory",
    "InjectionMatch",
    "InjectionScan",
    "InjectionScanner",
    "SanitizedText",
    "Signature",
    "fence",
    "neutralize_delimiters",
    "sanitize",
]
