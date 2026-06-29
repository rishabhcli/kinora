"""Unit tests for the decision cache + decision-log audit (no infra)."""

from __future__ import annotations

from app.platform.authz.audit import (
    CompositeDecisionLog,
    DecisionRecord,
    InMemoryDecisionLog,
    NullDecisionLog,
    summarize,
)
from app.platform.authz.cache import (
    InMemoryDecisionCache,
    NullDecisionCache,
)
from app.platform.authz.model import (
    AuthorizationRequest,
    Decision,
    Effect,
    Reason,
    Resource,
    Subject,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _decision(user: str, action: str, book: str, effect: Effect = Effect.ALLOW) -> Decision:
    req = AuthorizationRequest(
        subject=Subject.user(user),
        action=action,
        resource=Resource.of("book", book),
    )
    return Decision(request=req, effect=effect, reasons=(Reason("rbac", effect, "x"),))


# -- cache -------------------------------------------------------------------- #


def test_cache_hit_miss_and_flag() -> None:
    cache = InMemoryDecisionCache(ttl_s=10)
    d = _decision("alice", "book:read", "1")
    assert cache.get(d.request) is None
    cache.put(d)
    hit = cache.get(d.request)
    assert hit is not None and hit.cached is True
    assert cache.hits == 1 and cache.misses == 1


def test_cache_ttl_expiry() -> None:
    clock = FakeClock()
    cache = InMemoryDecisionCache(ttl_s=5, clock=clock)
    d = _decision("alice", "book:read", "1")
    cache.put(d)
    clock.advance(4.9)
    assert cache.get(d.request) is not None
    clock.advance(0.2)
    assert cache.get(d.request) is None  # expired


def test_cache_invalidate_subject_and_resource() -> None:
    cache = InMemoryDecisionCache(ttl_s=100)
    a = _decision("alice", "book:read", "1")
    b = _decision("alice", "book:read", "2")
    c = _decision("bob", "book:read", "1")
    for d in (a, b, c):
        cache.put(d)
    assert cache.invalidate_subject("user:alice") == 2
    assert cache.get(a.request) is None
    assert cache.get(c.request) is not None  # bob untouched
    assert cache.invalidate_resource("book:1") == 1  # bob's entry on book:1


def test_cache_lru_eviction() -> None:
    cache = InMemoryDecisionCache(ttl_s=100, max_entries=2)
    a = _decision("a", "book:read", "1")
    b = _decision("b", "book:read", "2")
    c = _decision("c", "book:read", "3")
    cache.put(a)
    cache.put(b)
    cache.get(a.request)  # touch a → b is now LRU
    cache.put(c)  # evicts b
    assert cache.get(b.request) is None
    assert cache.get(a.request) is not None
    assert cache.get(c.request) is not None


def test_null_cache_never_caches() -> None:
    cache = NullDecisionCache()
    d = _decision("alice", "book:read", "1")
    cache.put(d)
    assert cache.get(d.request) is None
    assert cache.invalidate_subject("user:alice") == 0


def test_cache_clear() -> None:
    cache = InMemoryDecisionCache()
    cache.put(_decision("a", "book:read", "1"))
    cache.clear()
    assert cache.size == 0


# -- audit log ---------------------------------------------------------------- #


def test_decision_record_from_decision() -> None:
    d = _decision("alice", "book:read", "1", Effect.DENY)
    rec = DecisionRecord.from_decision(d)
    assert rec.subject_ref == "user:alice"
    assert rec.action == "book:read"
    assert rec.resource_ref == "book:1"
    assert rec.effect is Effect.DENY
    assert not rec.allowed
    assert len(rec.digest) == 16


def test_in_memory_log_queries() -> None:
    log = InMemoryDecisionLog()
    log.record(_decision("alice", "book:read", "1", Effect.ALLOW))
    log.record(_decision("alice", "book:edit", "1", Effect.DENY))
    log.record(_decision("bob", "book:read", "2", Effect.ALLOW))
    assert len(log) == 3
    assert len(log.for_subject("user:alice")) == 2
    assert len(log.for_resource("book:1")) == 2
    assert len(log.denials()) == 1


def test_in_memory_log_capacity() -> None:
    log = InMemoryDecisionLog(capacity=2)
    for i in range(5):
        log.record(_decision("u", "book:read", str(i)))
    assert len(log) == 2  # only the most recent kept


def test_composite_and_null_log() -> None:
    a = InMemoryDecisionLog()
    b = InMemoryDecisionLog()
    composite = CompositeDecisionLog([a, b, NullDecisionLog()])
    composite.add(InMemoryDecisionLog())
    composite.record(_decision("alice", "book:read", "1"))
    assert len(a) == 1 and len(b) == 1


def test_summarize_stats() -> None:
    records = [
        DecisionRecord.from_decision(_decision("a", "book:read", "1", Effect.ALLOW)),
        DecisionRecord.from_decision(_decision("a", "book:edit", "1", Effect.DENY)),
        DecisionRecord.from_decision(_decision("b", "book:read", "2", Effect.ALLOW)),
    ]
    stats = summarize(records)
    assert stats.total == 3
    assert stats.allowed == 2 and stats.denied == 1
    assert abs(stats.deny_rate - 1 / 3) < 1e-9
    assert stats.by_action["book:read"] == 2
