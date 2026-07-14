"""Deterministic synthetic attack-trace builders for the defense test suite.

These produce ordered lists of
:class:`~app.zerotrust.defense.types.SecurityEvent` modelling concrete attack
scenarios (and benign baselines) so detector behaviour can be asserted exactly.
Everything is seeded; replaying a builder twice yields identical events.
"""

from __future__ import annotations

import random

from app.zerotrust.defense.types import AuthOutcome, EventKind, SecurityEvent


def benign_logins(
    *,
    start: float,
    user: str = "alice",
    ip: str = "10.0.0.5",
    n: int = 40,
    spacing: float = 120.0,
) -> list[SecurityEvent]:
    """A well-spaced sequence of successful logins from one user/ip (baseline)."""
    out: list[SecurityEvent] = []
    t = start
    for _ in range(n):
        out.append(
            SecurityEvent.auth(
                ts=t,
                source_ip=ip,
                username=user,
                outcome=AuthOutcome.SUCCESS,
                principal=user,
                user_agent="Mozilla/5.0 (Macintosh)",
            )
        )
        t += spacing
    return out


def brute_force(
    *,
    start: float,
    user: str = "victim",
    ip: str = "203.0.113.9",
    n: int = 50,
    spacing: float = 0.4,
) -> list[SecurityEvent]:
    """Many rapid failed logins against one username from one ip."""
    out: list[SecurityEvent] = []
    t = start
    for _ in range(n):
        out.append(
            SecurityEvent.auth(
                ts=t,
                source_ip=ip,
                username=user,
                outcome=AuthOutcome.FAILURE,
                user_agent="python-requests/2.31",
            )
        )
        t += spacing
    return out


def credential_stuffing(
    *,
    start: float,
    ip: str = "198.51.100.7",
    usernames: list[str] | None = None,
    spacing: float = 0.3,
    success_at: int | None = None,
) -> list[SecurityEvent]:
    """One ip trying a *breach list*: many distinct usernames, one attempt each.

    ``success_at`` optionally turns the Nth attempt into a SUCCESS (a hit in the
    stuffing list) so the takeover follow-on can be modelled.
    """
    names = usernames or [f"user{i:04d}" for i in range(60)]
    out: list[SecurityEvent] = []
    t = start
    for i, name in enumerate(names):
        outcome = (
            AuthOutcome.SUCCESS
            if success_at is not None and i == success_at
            else AuthOutcome.FAILURE
        )
        out.append(
            SecurityEvent.auth(
                ts=t,
                source_ip=ip,
                username=name,
                outcome=outcome,
                user_agent="Mozilla/5.0 (Windows NT 10.0)",
            )
        )
        t += spacing
    return out


def scraping_walk(
    *,
    start: float,
    ip: str = "192.0.2.44",
    base_path: str = "/api/books",
    n: int = 120,
    spacing: float = 0.25,
    ua: str = "python-requests/2.31",
) -> list[SecurityEvent]:
    """One client walking many distinct resource paths fast (content scraping)."""
    out: list[SecurityEvent] = []
    t = start
    for i in range(n):
        out.append(
            SecurityEvent.access(
                ts=t,
                source_ip=ip,
                principal=None,
                target=f"{base_path}/{i:05d}",
                action="GET",
                status_code=200,
                bytes_out=8192,
                user_agent=ua,
            )
        )
        t += spacing
    return out


def takeover_session(
    *,
    start: float,
    user: str = "carol",
    home_ip: str = "10.0.0.20",
    home_ua: str = "Mozilla/5.0 (Macintosh)",
    attacker_ip: str = "203.0.113.200",
    attacker_ua: str = "curl/8.4.0",
) -> list[SecurityEvent]:
    """A user's normal logins followed by a sudden new-ip + new-UA success.

    The history establishes the user's "home" fingerprint; the final event is the
    same account succeeding from a never-seen ip/device — the takeover signal.
    """
    out: list[SecurityEvent] = []
    t = start
    for _ in range(12):
        out.append(
            SecurityEvent.auth(
                ts=t,
                source_ip=home_ip,
                username=user,
                outcome=AuthOutcome.SUCCESS,
                principal=user,
                user_agent=home_ua,
            )
        )
        t += 3600.0
    out.append(
        SecurityEvent.auth(
            ts=t,
            source_ip=attacker_ip,
            username=user,
            outcome=AuthOutcome.SUCCESS,
            principal=user,
            user_agent=attacker_ua,
        )
    )
    return out


def noisy_baseline(
    *,
    start: float,
    n: int = 200,
    seed: int = 7,
) -> list[SecurityEvent]:
    """A mixed, randomised but seeded stream of benign traffic from many ips."""
    rng = random.Random(seed)
    out: list[SecurityEvent] = []
    t = start
    users = [f"reader{i}" for i in range(25)]
    for _ in range(n):
        u = rng.choice(users)
        ip = f"10.0.{rng.randrange(0, 4)}.{rng.randrange(2, 250)}"
        if rng.random() < 0.7:
            out.append(
                SecurityEvent.access(
                    ts=t,
                    source_ip=ip,
                    principal=u,
                    target=f"/api/books/{rng.randrange(0, 80)}",
                    status_code=200,
                    bytes_out=rng.randrange(1000, 20000),
                    user_agent="Mozilla/5.0 (Macintosh)",
                )
            )
        else:
            out.append(
                SecurityEvent.auth(
                    ts=t,
                    source_ip=ip,
                    username=u,
                    outcome=AuthOutcome.SUCCESS,
                    principal=u,
                    user_agent="Mozilla/5.0 (Macintosh)",
                )
            )
        t += rng.uniform(0.5, 5.0)
    return out


def merge(*traces: list[SecurityEvent]) -> list[SecurityEvent]:
    """Merge several traces into one stream ordered by timestamp (stable)."""
    combined = [ev for tr in traces for ev in tr]
    return sorted(combined, key=lambda e: e.ts)


_ = EventKind  # re-exported convenience for tests that build raw events
