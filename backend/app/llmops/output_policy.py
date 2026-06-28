"""Output policy checks — the *output* half of the guardrail layer.

After an agent produces text, the output policy decides whether it is safe to
return. The checks Kinora cares about:

* **secret / credential leakage** — an API key, bearer token, private key, or the
  DashScope/OpenAI key shape slipped into the output;
* **PII leakage** — email / phone / credit-card-shaped strings the model echoed
  from input it should have treated as data;
* **system-prompt leakage** — the output reproduces a chunk of a known system
  prompt (passed in as ``protected_texts``), i.e. a successful exfiltration;
* **refusal-bypass / unsafe content** — the output contains a small banned-phrase
  set (a jailbreak success tell), configurable per deployment;
* **format / schema violations** — for the crew's JSON-strict agents (§10), the
  output must parse as JSON when ``expect_json`` is set.

Each check yields zero or more :class:`PolicyViolation`s with a severity; the
aggregate :class:`OutputReport` carries the worst severity and whether the output
should be blocked. Pure module — no model calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class Severity(IntEnum):
    """Ordered severity so ``max`` gives the worst violation."""

    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class ViolationKind(StrEnum):
    SECRET_LEAK = "secret_leak"
    PII_LEAK = "pii_leak"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"
    UNSAFE_CONTENT = "unsafe_content"
    FORMAT_VIOLATION = "format_violation"


@dataclass(frozen=True, slots=True)
class PolicyViolation:
    kind: ViolationKind
    severity: Severity
    detail: str
    excerpt: str = ""


# --- detectors --------------------------------------------------------------- #

_SECRET_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("dashscope_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}\b")
_CC_RE = re.compile(r"\b(?:\d[ \-]?){13,16}\b")

#: A conservative banned-phrase seed for the "refusal bypass succeeded" check.
DEFAULT_BANNED_PHRASES: tuple[str, ...] = (
    "here is how to make a bomb",
    "step-by-step guide to synthesize",
    "i will ignore my safety guidelines",
    "as dan, i can",
)


def _luhn_ok(digits: str) -> bool:
    """Luhn check so a 16-digit ISBN/phone doesn't masquerade as a credit card."""
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 16:
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        checksum += n
    return checksum % 10 == 0


def detect_secrets(text: str) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    for name, rx in _SECRET_RES:
        m = rx.search(text)
        if m:
            out.append(
                PolicyViolation(
                    kind=ViolationKind.SECRET_LEAK,
                    severity=Severity.CRITICAL,
                    detail=f"possible {name} in output",
                    excerpt=text[m.start() : m.start() + 12] + "…",
                )
            )
    return out


def detect_pii(text: str) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    if m := _EMAIL_RE.search(text):
        out.append(
            PolicyViolation(ViolationKind.PII_LEAK, Severity.MEDIUM, "email address", m.group(0))
        )
    if m := _PHONE_RE.search(text):
        out.append(
            PolicyViolation(ViolationKind.PII_LEAK, Severity.LOW, "phone-shaped number", m.group(0))
        )
    for m in _CC_RE.finditer(text):
        if _luhn_ok(m.group(0)):
            out.append(
                PolicyViolation(
                    ViolationKind.PII_LEAK, Severity.HIGH, "credit-card-shaped number", "[card]"
                )
            )
            break
    return out


def detect_system_prompt_leak(text: str, protected_texts: list[str]) -> list[PolicyViolation]:
    """Flag the output if it reproduces a long shingle of a protected system prompt."""
    out: list[PolicyViolation] = []
    haystack = text.lower()
    for protected in protected_texts:
        # Compare on a sliding window of meaningful length; a 60-char run is a leak.
        norm = " ".join(protected.lower().split())
        window = 60
        for i in range(0, max(0, len(norm) - window), window // 2):
            shingle = norm[i : i + window]
            if shingle and shingle in haystack:
                out.append(
                    PolicyViolation(
                        ViolationKind.SYSTEM_PROMPT_LEAK,
                        Severity.HIGH,
                        "output reproduces protected system-prompt text",
                        shingle[:40] + "…",
                    )
                )
                break
    return out


def detect_unsafe(text: str, banned: tuple[str, ...]) -> list[PolicyViolation]:
    low = text.lower()
    return [
        PolicyViolation(
            ViolationKind.UNSAFE_CONTENT, Severity.HIGH, f"banned phrase: {phrase!r}", phrase
        )
        for phrase in banned
        if phrase in low
    ]


def detect_format(text: str, *, expect_json: bool) -> list[PolicyViolation]:
    if not expect_json:
        return []
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        candidate = candidate.removesuffix("```").strip()
    try:
        json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return [
            PolicyViolation(
                ViolationKind.FORMAT_VIOLATION,
                Severity.MEDIUM,
                "output is not valid JSON but JSON was required (§10 contract)",
            )
        ]
    return []


@dataclass(frozen=True, slots=True)
class OutputReport:
    violations: tuple[PolicyViolation, ...]
    blocked: bool
    max_severity: Severity

    @property
    def ok(self) -> bool:
        return not self.violations

    def kinds(self) -> tuple[ViolationKind, ...]:
        return tuple(dict.fromkeys(v.kind for v in self.violations))


@dataclass
class OutputPolicy:
    """Composes the output detectors into one report with a block threshold."""

    #: Block when any violation reaches this severity.
    block_at: Severity = Severity.HIGH
    expect_json: bool = False
    banned_phrases: tuple[str, ...] = field(default=DEFAULT_BANNED_PHRASES)
    check_pii: bool = True

    def check(self, text: str, *, protected_texts: list[str] | None = None) -> OutputReport:
        violations: list[PolicyViolation] = []
        violations.extend(detect_secrets(text))
        if self.check_pii:
            violations.extend(detect_pii(text))
        if protected_texts:
            violations.extend(detect_system_prompt_leak(text, protected_texts))
        violations.extend(detect_unsafe(text, self.banned_phrases))
        violations.extend(detect_format(text, expect_json=self.expect_json))
        max_sev = max((v.severity for v in violations), default=Severity.INFO)
        return OutputReport(
            violations=tuple(violations),
            blocked=max_sev >= self.block_at,
            max_severity=max_sev,
        )


__all__ = [
    "DEFAULT_BANNED_PHRASES",
    "OutputPolicy",
    "OutputReport",
    "PolicyViolation",
    "Severity",
    "ViolationKind",
    "detect_format",
    "detect_pii",
    "detect_secrets",
    "detect_system_prompt_leak",
    "detect_unsafe",
]
