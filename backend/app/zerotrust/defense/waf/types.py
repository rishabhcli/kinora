"""Request model and verdict types for the WAF-style policy engine.

The WAF screens an inbound HTTP request *before* it reaches the application. Its
input is a normalized :class:`HttpRequest` (method, path, query, headers, body,
client ip); its output is a :class:`Verdict` — an action plus the rules that
fired and a confidence. These types are framework-free so a thin adapter can
build an :class:`HttpRequest` from Starlette/ASGI without this package importing
the web layer.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote_plus, urlsplit


class Action(enum.StrEnum):
    """What the WAF decides to do with a request, in increasing severity."""

    ALLOW = "allow"
    #: Serve a challenge (JS/CAPTCHA/step-up) — suspected bot, not proven malicious.
    CHALLENGE = "challenge"
    #: Soft-limit: let through but signal downstream rate limiting.
    THROTTLE = "throttle"
    #: Hard block this request.
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return _ACTION_RANK[self]


_ACTION_RANK = {
    Action.ALLOW: 0,
    Action.CHALLENGE: 1,
    Action.THROTTLE: 2,
    Action.BLOCK: 3,
}


def max_action(a: Action, b: Action) -> Action:
    """The more severe of two actions."""
    return a if a.rank >= b.rank else b


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """A normalized inbound HTTP request the WAF screens.

    ``headers`` keys are lower-cased on construction so lookups are
    case-insensitive (HTTP header names are case-insensitive). Helpers expose the
    URL-decoded query/path so signatures match the *decoded* payload an attacker
    actually delivers (defeating simple percent-encoding evasion).
    """

    method: str = "GET"
    path: str = "/"
    query: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""
    source_ip: str = "0.0.0.0"
    ts: float = 0.0

    def __post_init__(self) -> None:
        lowered = {str(k).lower(): str(v) for k, v in self.headers.items()}
        object.__setattr__(self, "headers", lowered)

    @classmethod
    def from_url(
        cls,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: str = "",
        source_ip: str = "0.0.0.0",
        ts: float = 0.0,
    ) -> HttpRequest:
        parts = urlsplit(url)
        return cls(
            method=method.upper(),
            path=parts.path or "/",
            query=parts.query,
            headers=headers or {},
            body=body,
            source_ip=source_ip,
            ts=ts,
        )

    @property
    def user_agent(self) -> str:
        return self.headers.get("user-agent", "")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def decoded_path(self) -> str:
        return unquote_plus(self.path)

    @property
    def decoded_query(self) -> str:
        return unquote_plus(self.query)

    def query_params(self) -> list[tuple[str, str]]:
        return parse_qsl(self.query, keep_blank_values=True)

    @property
    def inspectable(self) -> str:
        """The decoded path + query + body — the surface signatures scan."""
        return f"{self.decoded_path}?{self.decoded_query}\n{unquote_plus(self.body)}"


@dataclass(frozen=True, slots=True)
class RuleHit:
    """One rule that matched a request."""

    rule_id: str
    category: str
    severity: int  # 0..4 (mirrors Severity ranks)
    action: Action
    message: str
    matched: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "severity": self.severity,
            "action": str(self.action),
            "message": self.message,
            "matched": self.matched,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """The WAF's decision about one request."""

    action: Action
    score: float
    hits: tuple[RuleHit, ...] = ()
    bot_score: float = 0.0
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.action is Action.BLOCK

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW

    def as_dict(self) -> dict[str, object]:
        return {
            "action": str(self.action),
            "score": round(self.score, 4),
            "bot_score": round(self.bot_score, 4),
            "reason": self.reason,
            "hits": [h.as_dict() for h in self.hits],
        }


__all__ = [
    "Action",
    "HttpRequest",
    "RuleHit",
    "Verdict",
    "max_action",
]
