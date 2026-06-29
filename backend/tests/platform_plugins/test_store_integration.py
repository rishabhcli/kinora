"""Postgres-backed store + service integration tests (isolated plugins DB).

These exercise the durable persistence + orchestration layers
(:mod:`app.platform.plugins.store` / :mod:`app.platform.plugins.service`)
against a real Postgres. They skip cleanly when
``KINORA_PLUGINS_TEST_DATABASE_URL`` is not set, and use a DEDICATED database
(``plugins_test`` on :5433) so they never touch the live ``kinora`` DB. Each
test starts clean by TRUNCATE-ing only the five plugin tables.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  register tables on Base.metadata
from app.db.base import Base
from app.platform.plugins.broker import HostServices
from app.platform.plugins.capabilities import RiskTier
from app.platform.plugins.errors import LifecycleError, PluginValidationError, SignatureError
from app.platform.plugins.hooks import ExtensionPoint
from app.platform.plugins.lifecycle import PluginState
from app.platform.plugins.service import (
    PluginPlatformConfig,
    PluginService,
    PluginUnitOfWork,
)
from app.platform.plugins.signing import Signer, artifact_digest

_PLUGINS_DB_URL = os.environ.get("KINORA_PLUGINS_TEST_DATABASE_URL") or os.environ.get(
    "KINORA_TEST_DATABASE_URL"
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        _PLUGINS_DB_URL is None,
        reason="KINORA_PLUGINS_TEST_DATABASE_URL not set; skipping plugins DB tests",
    ),
]

_PLUGIN_TABLES = (
    "plugin_audit",
    "plugin_rating",
    "plugin_review",
    "plugin_installation",
    "plugin_registry",
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert _PLUGINS_DB_URL is not None
    engine = create_async_engine(_PLUGINS_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(f"TRUNCATE {', '.join(_PLUGIN_TABLES)} RESTART IDENTITY CASCADE"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


def _make_service(
    maker: async_sessionmaker[AsyncSession],
    *,
    config: PluginPlatformConfig | None = None,
    signer: Signer | None = None,
    services_factory=None,
) -> PluginService:
    @asynccontextmanager
    async def _session():
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def uow() -> PluginUnitOfWork:
        return PluginUnitOfWork(_session)

    return PluginService(
        uow=uow,
        config=config or PluginPlatformConfig(auto_approve_low_risk=True),
        signer=signer,
        host_services_factory=services_factory,
    )


def _manifest(
    pid: str = "com.acme.tone",
    version: str = "1.0.0",
    caps: list[str] | None = None,
    hooks: list[dict] | None = None,
    deps: list[dict] | None = None,
) -> dict:
    return {
        "id": pid,
        "version": version,
        "name": pid,
        "publisher": "acme",
        "capabilities": caps if caps is not None else ["book.read", "log.write"],
        "hooks": hooks
        or [{"id": "h", "point": "ingest.filter", "entrypoint": "run", "priority": 10}],
        "dependencies": deps or [],
    }


_SOURCE_INGEST = (
    "def run(payload, host):\n"
    "    host.log('filtering', n=len(payload.get('text', '')))\n"
    "    out = dict(payload)\n"
    "    out['filtered'] = True\n"
    "    return out\n"
)


# --------------------------------------------------------------------------- #
# Publish + review + catalog
# --------------------------------------------------------------------------- #


async def test_publish_low_risk_auto_approved(session_factory) -> None:
    svc = _make_service(session_factory)
    summary = await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    assert summary["status"] == "approved"
    assert summary["max_risk"] == "low"
    catalog = await svc.catalog()
    assert [c["plugin_id"] for c in catalog] == ["com.acme.tone"]


async def test_publish_is_idempotent_on_digest(session_factory) -> None:
    svc = _make_service(session_factory)
    s1 = await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    s2 = await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    assert s1["digest"] == s2["digest"]
    catalog = await svc.catalog()
    assert len(catalog) == 1


async def test_publish_high_risk_pending_then_review(session_factory) -> None:
    svc = _make_service(session_factory)
    summary = await svc.publish(
        manifest_data=_manifest(caps=["canon.write"]), source=_SOURCE_INGEST
    )
    assert summary["status"] == "pending"
    # Not in the public (approved) catalog yet.
    assert await svc.catalog() == []
    reviewed = await svc.review(
        plugin_id="com.acme.tone", version="1.0.0", decision="approve", reviewer="mod"
    )
    assert reviewed["status"] == "approved"
    assert len(await svc.catalog()) == 1


async def test_publish_rejects_malformed_source(session_factory) -> None:
    svc = _make_service(session_factory)
    with pytest.raises(Exception):  # noqa: B017 - PluginRuntimeError on compile
        await svc.publish(manifest_data=_manifest(), source="def run(:\n  bad")


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #


async def test_publish_requires_signature_when_configured(session_factory) -> None:
    signer = Signer({"acme": b"key"})
    svc = _make_service(
        session_factory,
        config=PluginPlatformConfig(require_signature=True, auto_approve_low_risk=True),
        signer=signer,
    )
    with pytest.raises(SignatureError):
        await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)


async def test_publish_with_valid_signature(session_factory) -> None:
    signer = Signer({"acme": b"key"})
    svc = _make_service(
        session_factory,
        config=PluginPlatformConfig(require_signature=True, auto_approve_low_risk=True),
        signer=signer,
    )
    from app.platform.plugins.manifest import PluginManifest

    manifest = PluginManifest.parse(_manifest())
    digest = artifact_digest(manifest.to_dict(), _SOURCE_INGEST)
    sig = signer.sign(key_id="acme", digest=digest)
    summary = await svc.publish(
        manifest_data=_manifest(), source=_SOURCE_INGEST, signature=sig.to_dict()
    )
    assert summary["signed"] is True


# --------------------------------------------------------------------------- #
# Ratings
# --------------------------------------------------------------------------- #


async def test_rating_aggregates(session_factory) -> None:
    svc = _make_service(session_factory)
    await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    await svc.rate(plugin_id="com.acme.tone", user_id="u1", stars=5)
    stats = await svc.rate(plugin_id="com.acme.tone", user_id="u2", stars=3)
    assert stats.count == 2
    assert stats.average == 4.0
    # Re-rating by u1 replaces, does not double-count.
    stats = await svc.rate(plugin_id="com.acme.tone", user_id="u1", stars=1)
    assert stats.count == 2
    assert stats.average == 2.0


# --------------------------------------------------------------------------- #
# Install / lifecycle
# --------------------------------------------------------------------------- #


async def test_install_enable_and_dispatch(session_factory) -> None:
    svc = _make_service(
        session_factory,
        services_factory=lambda owner, pid: HostServices(),
    )
    await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    inst = await svc.install(
        owner="tenant1", plugin_id="com.acme.tone", version="1.0.0", enable=True
    )
    assert inst.state is PluginState.ENABLED

    report = await svc.dispatch(
        owner="tenant1",
        point=ExtensionPoint.INGEST_FILTER,
        payload={"text": "hello world"},
    )
    assert report.all_ok
    assert report.payload["filtered"] is True


async def test_install_then_enable_separately(session_factory) -> None:
    svc = _make_service(session_factory)
    await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    inst = await svc.install(owner="t", plugin_id="com.acme.tone", version="1.0.0")
    assert inst.state is PluginState.INSTALLED
    inst = await svc.enable(owner="t", plugin_id="com.acme.tone")
    assert inst.state is PluginState.ENABLED
    inst = await svc.disable(owner="t", plugin_id="com.acme.tone")
    assert inst.state is PluginState.DISABLED


async def test_cannot_install_unapproved(session_factory) -> None:
    svc = _make_service(session_factory)
    await svc.publish(manifest_data=_manifest(caps=["canon.write"]), source=_SOURCE_INGEST)
    with pytest.raises(LifecycleError):
        await svc.install(owner="t", plugin_id="com.acme.tone", version="1.0.0")


async def test_grant_clamped_to_risk_ceiling(session_factory) -> None:
    # A tenant limited to MEDIUM cannot install a plugin needing HIGH net.fetch.
    svc = _make_service(
        session_factory,
        config=PluginPlatformConfig(
            auto_approve_low_risk=False, max_grantable_risk=RiskTier.MEDIUM
        ),
    )
    await svc.publish(
        manifest_data=_manifest(
            caps=["net.fetch"],
            hooks=[{"id": "wh", "point": "webhook.action", "entrypoint": "run"}],
        ),
        source="def run(payload, host):\n    return None\n",
    )
    await svc.review(plugin_id="com.acme.tone", version="1.0.0", decision="approve", reviewer="m")
    with pytest.raises(PluginValidationError):
        await svc.install(owner="t", plugin_id="com.acme.tone", version="1.0.0")


async def test_upgrade_and_rollback(session_factory) -> None:
    svc = _make_service(session_factory)
    await svc.publish(manifest_data=_manifest(version="1.0.0"), source=_SOURCE_INGEST)
    await svc.publish(manifest_data=_manifest(version="1.1.0"), source=_SOURCE_INGEST)
    await svc.install(owner="t", plugin_id="com.acme.tone", version="1.0.0", enable=True)

    upgraded = await svc.upgrade(owner="t", plugin_id="com.acme.tone", to_version="1.1.0")
    assert str(upgraded.version) == "1.1.0"
    assert upgraded.state is PluginState.ENABLED

    rolled = await svc.rollback(owner="t", plugin_id="com.acme.tone")
    assert str(rolled.version) == "1.0.0"
    assert rolled.state is PluginState.ENABLED


async def test_dependency_resolution_on_install(session_factory) -> None:
    svc = _make_service(session_factory)
    # Publish a base plugin and a dependent plugin requiring it.
    await svc.publish(
        manifest_data=_manifest(pid="com.acme.base", version="1.2.0"), source=_SOURCE_INGEST
    )
    await svc.publish(
        manifest_data=_manifest(
            pid="com.acme.dependent",
            version="1.0.0",
            deps=[{"plugin_id": "com.acme.base", "range": "^1.0"}],
        ),
        source=_SOURCE_INGEST,
    )
    plan = await svc.plan_install(plugin_id="com.acme.dependent", version="1.0.0")
    assert "com.acme.base" in plan.resolution.chosen
    assert plan.resolution.order.index("com.acme.base") < plan.resolution.order.index(
        "com.acme.dependent"
    )


async def test_missing_dependency_blocks_install_plan(session_factory) -> None:
    from app.platform.plugins.errors import DependencyResolutionError

    svc = _make_service(session_factory)
    await svc.publish(
        manifest_data=_manifest(
            pid="com.acme.dependent",
            version="1.0.0",
            deps=[{"plugin_id": "com.acme.absent", "range": "^1.0"}],
        ),
        source=_SOURCE_INGEST,
    )
    with pytest.raises(DependencyResolutionError):
        await svc.plan_install(plugin_id="com.acme.dependent", version="1.0.0")


async def test_runtime_failure_quarantines(session_factory) -> None:
    svc = _make_service(
        session_factory,
        config=PluginPlatformConfig(auto_approve_low_risk=True, quarantine_threshold=3),
    )
    await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    await svc.install(owner="t", plugin_id="com.acme.tone", version="1.0.0", enable=True)
    inst = None
    for _ in range(3):
        inst = await svc.record_runtime_failure(owner="t", plugin_id="com.acme.tone")
    assert inst is not None
    assert inst.state is PluginState.QUARANTINED

    # A quarantined plugin is not dispatched.
    report = await svc.dispatch(
        owner="t", point=ExtensionPoint.INGEST_FILTER, payload={"text": "x"}
    )
    assert report.outcomes == []


async def test_list_installations(session_factory) -> None:
    svc = _make_service(session_factory)
    await svc.publish(manifest_data=_manifest(), source=_SOURCE_INGEST)
    await svc.install(owner="t", plugin_id="com.acme.tone", version="1.0.0", enable=True)
    installs = await svc.list_installations(owner="t")
    assert len(installs) == 1
    assert installs[0]["state"] == "enabled"
    assert installs[0]["active"] is True
