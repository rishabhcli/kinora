"""Alert deduplication, rollup and rate-suppression.

A detector under attack can emit the same finding thousands of times a second.
Forwarding each one buries the signal and floods the sink. The :class:`Deduper`
collapses alerts sharing a ``dedup_key`` within a cooldown window into a single
rolled-up alert: the first one passes through immediately; subsequent ones only
bump ``count``/``last_seen``/``score`` and re-emit on a throttled cadence (or
when severity escalates), so the operator sees "23,901 attempts over 5 min from
1.2.3.4", not 23,901 rows.

The deduper is pure and clock-driven, so suppression behaviour is deterministic
in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .types import Alert, Severity


@dataclass(slots=True)
class _DedupState:
    count: int
    first_seen: float
    last_emitted: float
    last_severity: Severity
    peak_score: float


@dataclass(slots=True)
class DedupConfig:
    """Tuning for :class:`Deduper`."""

    cooldown: float = 30.0
    """Seconds between re-emissions of an active, unchanged dedup key."""
    escalate_on_severity: bool = True
    """Re-emit immediately when a key's severity rises, ignoring cooldown."""
    reset_after: float = 300.0
    """Idle seconds after which a key is treated as a fresh incident again."""

    def __post_init__(self) -> None:
        if self.cooldown < 0:
            raise ValueError("cooldown must be non-negative")


class Deduper:
    """Collapse a storm of identical alerts into rolled-up emissions.

    :meth:`admit` takes a raw detector alert and returns either ``None`` (the
    alert is suppressed for now) or the alert to forward — annotated with the
    accumulated ``count`` and time span so the sink stores the rollup.
    """

    __slots__ = ("config", "_state")

    def __init__(self, config: DedupConfig | None = None) -> None:
        self.config = config or DedupConfig()
        self._state: dict[str, _DedupState] = {}

    def admit(self, alert: Alert, *, now: float) -> Alert | None:
        key = alert.dedup_key
        st = self._state.get(key)

        # New or long-idle key: a fresh incident — always emit.
        if st is None or now - st.last_emitted >= self.config.reset_after:
            self._state[key] = _DedupState(
                count=1,
                first_seen=alert.first_at,
                last_emitted=now,
                last_severity=alert.severity,
                peak_score=alert.score,
            )
            return replace(alert, count=1)

        st.count += 1
        st.peak_score = max(st.peak_score, alert.score)
        escalated = self.config.escalate_on_severity and alert.severity > st.last_severity
        due = now - st.last_emitted >= self.config.cooldown

        if escalated or due:
            st.last_emitted = now
            st.last_severity = alert.severity
            return replace(
                alert,
                count=st.count,
                score=st.peak_score,
                severity=Severity.for_score(st.peak_score),
                first_seen=st.first_seen,
                last_seen=alert.ts,
            )
        # Suppressed: still tracking the rollup, just not forwarding yet.
        st.last_severity = max(st.last_severity, alert.severity)
        return None

    def flush(self, *, now: float) -> list[Alert]:
        """No-op hook for symmetry; rollups are emitted on the cooldown cadence.

        Returns an empty list — present so an engine can call ``flush`` on a
        timer without special-casing the deduper.
        """
        _ = now
        return []

    def active_keys(self) -> int:
        return len(self._state)


__all__ = ["DedupConfig", "Deduper"]
