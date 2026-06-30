"""Redaction-aware projection — PII is scrubbed, the hash chain survives.

The conflict a naive audit log hits: you must hash an entry to make it
tamper-evident, but you must also be able to *erase* personal data later (GDPR
Art. 17) — and a normal hash over the plaintext means erasing the plaintext
destroys the ability to re-verify the chain.

Kinora's resolution: **commit to a hash of the value, never the plaintext.** At
append time every sensitive field is replaced by a deterministic *commitment*::

    {"__redacted__": <reason>, "digest": sha256(salt || canonical_json(value))}

The hash chain is then computed over this *redacted core*, so:

* the plaintext PII never enters the stored row or the hash input;
* the entry_hash is stable forever — re-verification re-hashes the same redacted
  core and matches;
* a later "right to erasure" need not touch the audit row at all (the PII was
  never stored), yet an auditor holding the original value can still *prove* it
  matches the commitment by re-deriving the digest.

When a field is **not** known-sensitive it is stored verbatim (the before/after
canon snapshots are not PII and must stay legible for provenance). The salt is
process config (defaults to a derived secret) so commitments are unforgeable
without it but reproducible by the operator who holds it.

A small built-in :class:`Redactor` recognises common PII keys (email, password,
token, ip, name, …) and email-shaped values; call sites can pass extra keys or a
fully custom policy. Pure module (no DB, no clock).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.audit.chain import canonical_json

#: The marker key identifying a redacted node in a stored payload.
REDACTED_KEY = "__redacted__"

#: Substrings that mark a *key* as carrying PII / a secret. Mirrors the logging
#: scrubber's vocabulary so the two stay consistent. ``token`` is matched exactly
#: (below) so operational ids like ``provider_task_id`` are never scrubbed.
_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "bearer",
    "email",
    "phone",
    "ssn",
    "credit_card",
    "ip_address",
    "first_name",
    "last_name",
    "full_name",
)

#: Email-shaped values are scrubbed even under a non-sensitive key (a free-text
#: ``reason`` that happens to contain an address).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered == "token":
        return True
    return any(marker in lowered for marker in _SENSITIVE_KEY_SUBSTRINGS)


@dataclass(frozen=True)
class Redactor:
    """A reusable, deterministic PII redactor.

    ``salt`` keys the commitment digest (so it is unforgeable without it but
    reproducible by the operator). ``extra_keys`` adds call-site-specific
    sensitive key substrings; ``scrub_email_values`` masks email-shaped strings
    found under *any* key.
    """

    salt: str = ""
    extra_keys: frozenset[str] = field(default_factory=frozenset)
    scrub_email_values: bool = True

    def _key_is_sensitive(self, key: str) -> bool:
        if _is_sensitive_key(key):
            return True
        lowered = key.lower()
        return any(marker in lowered for marker in self.extra_keys)

    def commit(self, value: Any, *, reason: str = "pii") -> dict[str, str]:
        """Return the redaction commitment for ``value`` (no plaintext retained)."""
        digest = hashlib.sha256((self.salt + canonical_json(value)).encode("utf-8")).hexdigest()
        return {REDACTED_KEY: reason, "digest": digest}

    def verify(self, value: Any, commitment: dict[str, Any]) -> bool:
        """True iff ``value`` matches a previously-issued ``commitment``.

        Lets an auditor who holds the original PII *prove* an audit entry is about
        that subject without the entry ever having stored the plaintext.
        """
        expected = self.commit(value, reason=str(commitment.get(REDACTED_KEY, "pii")))
        return expected["digest"] == commitment.get("digest")

    def redact(self, value: Any, *, _key: str | None = None) -> Any:
        """Recursively redact a value, returning a hash-safe, PII-free copy.

        Dict values under a sensitive key become a commitment; nested dicts /
        lists recurse; email-shaped strings are masked element-wise. Anything not
        recognised as sensitive is returned unchanged (so provenance snapshots
        stay legible).

        Idempotent: an already-redacted node (a commitment produced by a previous
        pass) is returned verbatim, so re-running ``redact`` over a stored,
        already-scrubbed payload never double-commits (which would change the
        bytes and break the hash chain — see :meth:`AuditService.forget_subject`).
        """
        if is_redacted(value):
            return value
        if _key is not None and self._key_is_sensitive(_key):
            return self.commit(value)
        if isinstance(value, dict):
            return {k: self.redact(v, _key=k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str) and self.scrub_email_values and _EMAIL_RE.search(value):
            # Mask each address but keep surrounding text legible.
            def _mask(match: re.Match[str]) -> str:
                return self.commit(match.group(0))["digest"][:12] + "@redacted"

            return _EMAIL_RE.sub(_mask, value)
        return value

    def redact_core(self, core: dict[str, Any]) -> dict[str, Any]:
        """Redact the hashable fields of an entry core (before/after/payload/reason).

        Returns a new dict; the structural fields (seq, action, ids, timestamps)
        are never touched — only the free-form, possibly-PII-bearing fields.
        """
        redacted = dict(core)
        for fieldname in ("before", "after", "payload"):
            if redacted.get(fieldname) is not None:
                redacted[fieldname] = self.redact(redacted[fieldname])
        reason = redacted.get("reason")
        if isinstance(reason, str):
            redacted["reason"] = self.redact(reason)
        return redacted


def is_redacted(node: Any) -> bool:
    """True when ``node`` is a redaction commitment produced by :class:`Redactor`."""
    return isinstance(node, dict) and REDACTED_KEY in node and "digest" in node


def contains_pii_plaintext(value: Any, redactor: Redactor) -> bool:
    """Best-effort check that ``value`` still holds un-redacted PII.

    Used by tests / a self-audit to confirm the store never persisted plaintext
    under a sensitive key. Already-redacted commitments are treated as clean.
    """
    if is_redacted(value):
        return False
    if isinstance(value, dict):
        for key, sub in value.items():
            if redactor._key_is_sensitive(key) and not is_redacted(sub):
                return True
            if contains_pii_plaintext(sub, redactor):
                return True
        return False
    if isinstance(value, list):
        return any(contains_pii_plaintext(item, redactor) for item in value)
    if isinstance(value, str) and redactor.scrub_email_values:
        return bool(_EMAIL_RE.search(value))
    return False


def default_keys(extra: Iterable[str] | None = None) -> frozenset[str]:
    """Build an ``extra_keys`` frozenset (helper for call sites)."""
    return frozenset(k.lower() for k in (extra or ()))


__all__ = [
    "REDACTED_KEY",
    "Redactor",
    "contains_pii_plaintext",
    "default_keys",
    "is_redacted",
]
