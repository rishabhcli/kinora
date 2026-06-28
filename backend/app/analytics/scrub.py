"""PII-safe event scrubbing — the boundary that keeps PII out of storage.

Every raw event passes through :func:`scrub_event` before it is persisted or
analysed. The scrubber:

* **Pseudonymises identifiers.** ``user_ref`` is turned into an opaque,
  *deterministic* ``anon_user_id`` via a salted hash (HMAC-SHA256, hex-truncated).
  Deterministic so the same user maps to the same anon id across events (retention
  and funnels need a stable per-user key) — but the salt makes it impossible to
  reverse to the original identifier without the secret.
* **Redacts free text.** String prop values are run through redactors that mask
  emails, bearer tokens, ``sk-`` keys, URLs with query strings, and filesystem
  paths, then truncated to a hard length cap. Director notes, search queries, and
  error messages are exactly the high-risk free-text fields this protects.
* **Allow/deny lists props.** Only known-safe prop keys survive; anything else is
  dropped. Keys whose name *looks* sensitive (``email``, ``token``, ``password``,
  ``name``, ``query`` …) are always dropped even if not on the allow-list, so a
  new producer can't accidentally leak a field by adding it upstream.
* **Clamps clock skew.** A client ``occurred_at`` wildly in the future (a broken
  device clock) is clamped to ``received_at`` so it can't poison time-bucketing.

The salted hash is the only place the configured secret is used; pass it from
``Settings.analytics_salt``. All functions are pure given the salt.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.analytics.events import RawEvent, TrackedEvent

# --------------------------------------------------------------------------- #
# Identifier pseudonymisation
# --------------------------------------------------------------------------- #

#: Length of the hex digest kept for an anon id. 20 hex chars = 80 bits, plenty
#: of collision resistance for a per-deployment user population.
_ANON_ID_HEX_LEN = 20


def anonymize(identifier: str | None, *, salt: str) -> str | None:
    """Return a stable, opaque, salted hash of ``identifier`` (or ``None``).

    Deterministic for a given ``(identifier, salt)`` so the same user always maps
    to the same anon id (required for retention/funnels), while the salt prevents
    reversing the digest back to the original id.
    """
    if identifier is None:
        return None
    ident = identifier.strip()
    if not ident:
        return None
    digest = hmac.new(salt.encode("utf-8"), ident.encode("utf-8"), hashlib.sha256)
    return f"u_{digest.hexdigest()[:_ANON_ID_HEX_LEN]}"


def session_key(session_ref: str | None, *, salt: str) -> str | None:
    """Pseudonymise a client session reference the same way (``s_`` prefix)."""
    anon = anonymize(session_ref, salt=salt)
    if anon is None:
        return None
    return "s_" + anon[2:]


# --------------------------------------------------------------------------- #
# Free-text redaction
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{6,}\b")
# A URL: keep the scheme+host, drop any path/query (which can carry tokens / PII).
_URL_RE = re.compile(r"\bhttps?://[^\s/]+(?:/\S*)?")
# A filesystem-ish path with at least two segments (e.g. /Users/jane/book.pdf).
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)(?:[\w .\-]+[/\\]){1,}[\w .\-]+")

#: Hard cap on any stored free-text value (chars). Long text is engagement noise
#: and a PII risk; cap aggressively.
_MAX_TEXT_LEN = 240

#: Placeholder for a redacted span.
_REDACTED = "[redacted]"


def redact_text(value: str) -> str:
    """Mask emails/tokens/keys/URLs/paths in ``value`` and cap its length."""
    text = _EMAIL_RE.sub(_REDACTED, value)
    text = _BEARER_RE.sub(_REDACTED, text)
    text = _SK_KEY_RE.sub(_REDACTED, text)
    text = _URL_RE.sub(_REDACTED, text)
    text = _PATH_RE.sub(_REDACTED, text)
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN] + "…"
    return text


# --------------------------------------------------------------------------- #
# Prop allow/deny lists
# --------------------------------------------------------------------------- #

#: Keys whose *values* are analytically useful and safe to keep. Anything not in
#: this set is dropped. Keep this list small and reviewed.
ALLOWED_PROP_KEYS: frozenset[str] = frozenset(
    {
        # navigation / reading position
        "page",
        "page_count",
        "from_page",
        "to_page",
        "word_index",
        "focus_word",
        "velocity_wps",
        "scroll_direction",
        "dwell_ms",
        "duration_ms",
        # video stage
        "shot_id",
        "scene_id",
        "clip_seconds",
        "stall_ms",
        "stage",  # render ladder stage label (e.g. "video", "keyframe")
        "buffer_seconds_ahead",
        # director
        "mode",
        "from_mode",
        "to_mode",
        "agent",  # routed agent (cinematographer/continuity)
        "aspect",  # pacing/look/room/...
        "entity_type",
        # library / acquisition (categorical only)
        "source",  # "upload" | "public_domain" | "demo"
        "genre",
        "result_count",  # search results count (a number, not the query!)
        "import_status",
        # generic engagement (categorical / numeric)
        "feature",
        "error_code",
        "platform",  # "macos" | "windows" | "web"
        "ok",
    }
)

#: Substrings that, if present in a prop *key*, force a drop regardless of the
#: allow-list — a belt-and-suspenders guard against a producer adding a leaky
#: field with an innocuous-looking name elsewhere in the dict.
_DENY_KEY_SUBSTRINGS: tuple[str, ...] = (
    "email",
    "token",
    "password",
    "secret",
    "apikey",
    "api_key",
    "name",  # user_name, file_name, full_name, ...
    "query",  # raw search query text
    "note",  # raw director note text
    "text",
    "title",  # book titles can be identifying / copyrighted
    "url",
    "path",
    "ip",
    "address",
    "phone",
)

#: Maximum number of props kept on a single event (cardinality guard).
_MAX_PROPS = 24


def _key_is_denied(key: str) -> bool:
    low = key.lower()
    return any(token in low for token in _DENY_KEY_SUBSTRINGS)


def _scrub_value(value: Any) -> Any:
    """Scrub a single prop value (strings redacted; scalars kept; else dropped)."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    # Nested structures are not analytics-friendly and a PII risk: drop them.
    return None


def scrub_props(props: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict containing only allow-listed, redacted, bounded props."""
    out: dict[str, Any] = {}
    for key, value in props.items():
        if len(out) >= _MAX_PROPS:
            break
        if not isinstance(key, str):
            continue
        if _key_is_denied(key):
            continue
        if key not in ALLOWED_PROP_KEYS:
            continue
        scrubbed = _scrub_value(value)
        if scrubbed is None:
            continue
        out[key] = scrubbed
    return out


# --------------------------------------------------------------------------- #
# Clock-skew clamp
# --------------------------------------------------------------------------- #

#: How far in the future a client ``occurred_at`` may be before we clamp it to
#: ``received_at`` (tolerates small clock differences, rejects broken clocks).
_MAX_FUTURE_SKEW = timedelta(minutes=5)


def _clamp_occurred_at(occurred_at: datetime, received_at: datetime) -> datetime:
    if occurred_at > received_at + _MAX_FUTURE_SKEW:
        return received_at
    return occurred_at


# --------------------------------------------------------------------------- #
# The one entry point
# --------------------------------------------------------------------------- #


def scrub_event(
    raw: RawEvent,
    *,
    salt: str,
    received_at: datetime | None = None,
) -> TrackedEvent:
    """Convert a validated :class:`RawEvent` into a stored :class:`TrackedEvent`.

    Pseudonymises identifiers, scrubs props, clamps clock skew, and stamps
    ``received_at``. Pure given ``salt`` (and the optional ``received_at`` clock).
    """
    received = received_at or datetime.now(UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    return TrackedEvent(
        event_id=raw.event_id,
        name=raw.event_name,
        occurred_at=_clamp_occurred_at(raw.occurred_at, received),
        received_at=received,
        anon_user_id=anonymize(raw.user_ref, salt=salt),
        book_id=raw.book_id,
        session_key=session_key(raw.session_ref, salt=salt),
        mode=raw.mode,
        props=scrub_props(raw.props),
    )


__all__ = [
    "ALLOWED_PROP_KEYS",
    "anonymize",
    "redact_text",
    "scrub_event",
    "scrub_props",
    "session_key",
]
