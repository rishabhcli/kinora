"""WAF rule + signature primitives and a default OWASP-flavoured ruleset.

A :class:`Rule` is a compiled predicate over an
:class:`~app.zerotrust.defense.waf.types.HttpRequest` plus the action/severity to
apply when it matches. Two concrete kinds cover most needs:

* :class:`SignatureRule` — a regex over a chosen part of the request (the decoded
  path+query+body by default), the classic WAF signature; and
* :class:`PredicateRule` — an arbitrary callable predicate for structural checks
  that a regex expresses badly (oversized body, too many params, bad method).

:func:`default_ruleset` returns a deterministic, ordered list modelling the core
OWASP CRS categories (SQLi, XSS, path traversal, command injection, scanner UAs,
protocol anomalies). Rules are pure and compiled once; the engine evaluates them
in order. Regex compilation failures raise
:class:`~app.zerotrust.defense.errors.RuleCompileError` at construction so a bad
custom rule is caught at load time, not on the hot path.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import Enum

from ..errors import RuleCompileError
from .types import Action, HttpRequest, RuleHit


class Target(Enum):
    """Which part of the request a signature scans."""

    INSPECTABLE = "inspectable"  # decoded path+query+body
    PATH = "path"
    QUERY = "query"
    BODY = "body"
    USER_AGENT = "user_agent"
    HEADERS = "headers"

    def extract(self, request: HttpRequest) -> str:
        if self is Target.INSPECTABLE:
            return request.inspectable
        if self is Target.PATH:
            return request.decoded_path
        if self is Target.QUERY:
            return request.decoded_query
        if self is Target.BODY:
            return request.body
        if self is Target.USER_AGENT:
            return request.user_agent
        return "\n".join(f"{k}: {v}" for k, v in request.headers.items())


class Rule:
    """Abstract WAF rule: produces a :class:`RuleHit` or ``None``."""

    __slots__ = ("rule_id", "category", "severity", "action", "message", "enabled")

    def __init__(
        self,
        rule_id: str,
        category: str,
        severity: int,
        action: Action,
        message: str,
        *,
        enabled: bool = True,
    ) -> None:
        if not 0 <= severity <= 4:
            raise RuleCompileError(f"rule {rule_id}: severity must be 0..4")
        self.rule_id = rule_id
        self.category = category
        self.severity = severity
        self.action = action
        self.message = message
        self.enabled = enabled

    def evaluate(self, request: HttpRequest) -> RuleHit | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _hit(self, matched: str = "") -> RuleHit:
        return RuleHit(
            rule_id=self.rule_id,
            category=self.category,
            severity=self.severity,
            action=self.action,
            message=self.message,
            matched=matched[:120],
        )


class SignatureRule(Rule):
    """A compiled-regex signature over a chosen request target."""

    __slots__ = ("_pattern", "target")

    def __init__(
        self,
        rule_id: str,
        category: str,
        severity: int,
        action: Action,
        message: str,
        pattern: str,
        *,
        target: Target = Target.INSPECTABLE,
        flags: int = re.IGNORECASE,
        enabled: bool = True,
    ) -> None:
        super().__init__(rule_id, category, severity, action, message, enabled=enabled)
        try:
            self._pattern = re.compile(pattern, flags)
        except re.error as exc:  # bad regex -> caught at load time
            raise RuleCompileError(f"rule {rule_id}: bad pattern {pattern!r}: {exc}") from exc
        self.target = target

    @property
    def pattern(self) -> str:
        return self._pattern.pattern

    def evaluate(self, request: HttpRequest) -> RuleHit | None:
        if not self.enabled:
            return None
        haystack = self.target.extract(request)
        m = self._pattern.search(haystack)
        if m is None:
            return None
        return self._hit(matched=m.group(0))


class PredicateRule(Rule):
    """A structural rule driven by an arbitrary predicate."""

    __slots__ = ("_predicate", "_describe")

    def __init__(
        self,
        rule_id: str,
        category: str,
        severity: int,
        action: Action,
        message: str,
        predicate: Callable[[HttpRequest], bool],
        *,
        describe: Callable[[HttpRequest], str] | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(rule_id, category, severity, action, message, enabled=enabled)
        self._predicate = predicate
        self._describe = describe

    def evaluate(self, request: HttpRequest) -> RuleHit | None:
        if not self.enabled:
            return None
        if not self._predicate(request):
            return None
        matched = self._describe(request) if self._describe else ""
        return self._hit(matched=matched)


# --------------------------------------------------------------------------- #
# Default ruleset (OWASP CRS-flavoured, deliberately compact + deterministic)
# --------------------------------------------------------------------------- #

_SQLI = (
    r"(?:'\s*or\s+'?\d|'\s*or\s+1\s*=\s*1|union\s+select|select\s+.+\s+from\s+|"
    r"insert\s+into\s+|drop\s+table|;\s*--|/\*.*\*/|\bor\b\s+\d+\s*=\s*\d+|sleep\s*\(|"
    r"benchmark\s*\(|information_schema|xp_cmdshell)"
)
_XSS = (
    r"(?:<\s*script\b|javascript:|onerror\s*=|onload\s*=|<\s*img[^>]+src\s*=|"
    r"document\.cookie|<\s*iframe\b|<\s*svg[^>]+onload|alert\s*\()"
)
_TRAVERSAL = r"(?:\.\./|\.\.\\|%2e%2e|/etc/passwd|/proc/self/|c:\\windows|file://|\.\.;/)"
_CMDI = (
    r"(?:;\s*(?:cat|ls|id|whoami|uname|wget|curl|nc|bash|sh)\b|\|\s*(?:cat|nc|sh|bash)\b|"
    r"\$\([^)]+\)|`[^`]+`|&&\s*(?:cat|id|whoami)\b)"
)
_RFI = r"(?:https?://[^\s]+\.(?:php|txt|cgi)\b|php://|data://|expect://|allow_url_include)"
_SCANNER_UA = (
    r"(?:sqlmap|nikto|nmap|masscan|acunetix|nessus|metasploit|dirbuster|gobuster|wpscan|"
    r"havij|fimap|w3af)"
)
_PROTO_ANOMALY = r"(?:%00|\x00|\r\n\r\n|content-length\s*:\s*-)"


def default_ruleset() -> list[Rule]:
    """A fresh, deterministically-ordered default ruleset.

    Returned as a new list each call so a caller can extend/disable rules without
    mutating shared state.
    """
    return [
        SignatureRule(
            "KNR-SQLI-1",
            "sqli",
            4,
            Action.BLOCK,
            "Possible SQL injection",
            _SQLI,
        ),
        SignatureRule(
            "KNR-XSS-1",
            "xss",
            3,
            Action.BLOCK,
            "Possible cross-site scripting",
            _XSS,
        ),
        SignatureRule(
            "KNR-LFI-1",
            "path_traversal",
            4,
            Action.BLOCK,
            "Possible path traversal / local file inclusion",
            _TRAVERSAL,
        ),
        SignatureRule(
            "KNR-CMDI-1",
            "command_injection",
            4,
            Action.BLOCK,
            "Possible OS command injection",
            _CMDI,
        ),
        SignatureRule(
            "KNR-RFI-1",
            "remote_file_inclusion",
            4,
            Action.BLOCK,
            "Possible remote file inclusion",
            _RFI,
        ),
        SignatureRule(
            "KNR-SCAN-1",
            "scanner",
            3,
            Action.BLOCK,
            "Known vulnerability scanner user-agent",
            _SCANNER_UA,
            target=Target.USER_AGENT,
        ),
        SignatureRule(
            "KNR-PROTO-1",
            "protocol_anomaly",
            3,
            Action.BLOCK,
            "HTTP protocol anomaly / null byte",
            _PROTO_ANOMALY,
            target=Target.INSPECTABLE,
        ),
        PredicateRule(
            "KNR-METHOD-1",
            "protocol_anomaly",
            2,
            Action.BLOCK,
            "Disallowed HTTP method",
            lambda r: r.method
            not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"},
            describe=lambda r: r.method,
        ),
        PredicateRule(
            "KNR-SIZE-1",
            "protocol_anomaly",
            1,
            Action.THROTTLE,
            "Oversized request body",
            lambda r: len(r.body) > 1_000_000,
            describe=lambda r: f"{len(r.body)} bytes",
        ),
        PredicateRule(
            "KNR-PARAMS-1",
            "protocol_anomaly",
            1,
            Action.CHALLENGE,
            "Excessive query parameters",
            lambda r: len(r.query_params()) > 64,
            describe=lambda r: f"{len(r.query_params())} params",
        ),
    ]


__all__ = [
    "PredicateRule",
    "Rule",
    "SignatureRule",
    "Target",
    "default_ruleset",
]
