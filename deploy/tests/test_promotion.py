"""Tests for artifact promotion across environments (gap rule, soak, idempotency)."""

from __future__ import annotations

import pytest

from deploy.orchestrator.fakes import VirtualClock, make_artifact
from deploy.orchestrator.models import Environment
from deploy.orchestrator.promotion import PromotionPipeline, PromotionRejectedError


def _pipeline(**kw: object) -> PromotionPipeline:
    return PromotionPipeline(now=VirtualClock(), **kw)  # type: ignore[arg-type]


def test_promote_into_dev_has_no_lower_gate() -> None:
    pipe = _pipeline()
    art = make_artifact()
    pipe.check_promotable(art, Environment.DEV)  # no raise


def test_staging_requires_dev_success() -> None:
    pipe = _pipeline()
    art = make_artifact()
    with pytest.raises(PromotionRejectedError) as exc:
        pipe.check_promotable(art, Environment.STAGING)
    assert "dev" in str(exc.value)
    # After dev succeeds, staging is promotable.
    pipe.mark_succeeded(art, Environment.DEV)
    pipe.check_promotable(art, Environment.STAGING)


def test_prod_requires_staging_success_not_just_dev() -> None:
    pipe = _pipeline()
    art = make_artifact()
    pipe.mark_succeeded(art, Environment.DEV)
    with pytest.raises(PromotionRejectedError):
        pipe.check_promotable(art, Environment.PROD)
    pipe.mark_succeeded(art, Environment.STAGING)
    pipe.check_promotable(art, Environment.PROD)


def test_idempotent_when_already_live() -> None:
    pipe = _pipeline()
    art = make_artifact()
    pipe.mark_succeeded(art, Environment.DEV)
    pipe.mark_succeeded(art, Environment.STAGING)
    assert pipe.is_idempotent(art, Environment.STAGING) is True
    # check_promotable should not raise even though it's "already live".
    pipe.check_promotable(art, Environment.STAGING)


def test_soak_rule_blocks_then_allows_after_time() -> None:
    clock = VirtualClock()
    pipe = PromotionPipeline(now=clock, min_soak_s=60.0)
    art = make_artifact()
    pipe.mark_succeeded(art, Environment.DEV)  # healthy_since = 0
    with pytest.raises(PromotionRejectedError) as exc:
        pipe.check_promotable(art, Environment.STAGING)
    assert "soak" in str(exc.value)
    clock.advance(120.0)
    pipe.check_promotable(art, Environment.STAGING)  # now soaked enough


def test_rollback_reverts_live_and_clears_soak() -> None:
    clock = VirtualClock()
    pipe = PromotionPipeline(now=clock)
    prev = make_artifact(digest_body="b" * 64)
    new = make_artifact(digest_body="c" * 64)
    pipe.mark_succeeded(prev, Environment.STAGING)
    assert pipe.live_digest(Environment.STAGING) == prev.digest

    # Pretend new went live then rolls back to prev.
    pipe.mark_rolled_back(new, Environment.STAGING, to=prev.digest)
    assert pipe.live_digest(Environment.STAGING) == prev.digest
    assert not pipe.has_succeeded(Environment.STAGING, new.digest)


def test_disable_lower_env_gate() -> None:
    pipe = _pipeline(require_lower_env=False)
    art = make_artifact()
    # Straight to prod allowed when the gap rule is disabled.
    pipe.check_promotable(art, Environment.PROD)


def test_record_snapshot() -> None:
    pipe = _pipeline()
    art = make_artifact()
    pipe.mark_succeeded(art, Environment.DEV)
    rec = pipe.record(Environment.DEV)
    assert rec.live_digest == art.digest
    assert art.digest in rec.succeeded_digests
