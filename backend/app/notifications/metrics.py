"""Prometheus counters for the notifications platform (kinora.md §12.5).

Kept inside the notifications package (not the shared ``app.observability.metrics``
module owned by another domain) so this is purely additive. The counters register
on the *default* Prometheus registry, which ``app.observability.metrics`` also
scrapes via ``generate_latest`` at ``/metrics`` — so these show up alongside the
render metrics without editing the shared module.

All helpers are defensive: a duplicate registration (e.g. a test re-importing the
module) is swallowed so importing this never raises.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised implicitly by import
    from prometheus_client import Counter

    _ENABLED = True
except Exception:  # pragma: no cover - prometheus is a hard dep, but stay safe
    _ENABLED = False


def _counter(name: str, doc: str, labels: list[str]) -> Any:
    if not _ENABLED:
        return None
    try:
        return Counter(name, doc, labels)
    except ValueError:
        # Already registered (re-import in tests) — fetch the existing collector.
        from prometheus_client import REGISTRY

        return REGISTRY._names_to_collectors.get(name)  # noqa: SLF001


_dispatched = _counter(
    "kinora_notifications_dispatched_total",
    "Notifications dispatched, by channel + outcome.",
    ["channel", "outcome"],
)
_webhook = _counter(
    "kinora_notification_webhooks_total",
    "Webhook delivery attempts, by result.",
    ["result"],
)
_deadletters = _counter(
    "kinora_notification_deadletters_total",
    "Notifications dead-lettered, by channel.",
    ["channel"],
)


def inc_dispatched(channel: str, outcome: str) -> None:
    """Count one dispatch outcome (delivered / deferred / retry / deadlettered …)."""
    if _dispatched is not None:
        _dispatched.labels(channel=channel, outcome=outcome).inc()


def inc_webhook(result: str) -> None:
    """Count one webhook delivery attempt result."""
    if _webhook is not None:
        _webhook.labels(result=result).inc()


def inc_deadletter(channel: str) -> None:
    """Count one dead-lettered notification."""
    if _deadletters is not None:
        _deadletters.labels(channel=channel).inc()


__all__ = ["inc_deadletter", "inc_dispatched", "inc_webhook"]
