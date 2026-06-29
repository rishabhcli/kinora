"""The plugin-platform service facade.

This is the orchestration layer the API and composition root use. It ties the
pure core (manifest, capability model, resolver, lifecycle, marketplace,
signing) to the persistence layer (:mod:`store`) and the sandbox
(:mod:`runtime` / :mod:`registry`) behind a small set of high-level operations:

* **publish** — validate a manifest, compute the content digest, verify the
  signature (when required), assign the initial review status, persist.
* **review** / **rate** — moderation + social signals.
* **install / enable** — grant the requested capabilities (clamped to a host
  policy), resolve dependencies, transition the lifecycle row.
* **upgrade / rollback** — version changes with re-resolution + ledger.
* **build_registry** — hydrate an in-memory :class:`HookRegistry` of a tenant's
  enabled plugins, each compiled in the sandbox and bound to its grant set and
  host services, ready for :meth:`HookRegistry.dispatch`.

The service takes a :class:`PluginPlatformConfig` (the host policy: signing
requirement, resource ceiling, capability allow-policy, host-services factory)
and a :class:`PluginUnitOfWork` factory that yields the repositories inside a
committing transaction — mirroring how the rest of the backend wires services.

Every repository write goes through a unit-of-work the *caller* commits; the
service itself never commits, matching the project convention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.platform.plugins.broker import HostServices
from app.platform.plugins.capabilities import GrantSet, RiskTier, risk_of
from app.platform.plugins.db_models import PluginRegistryEntry
from app.platform.plugins.errors import (
    LifecycleError,
    PluginNotFoundError,
    PluginValidationError,
    SignatureError,
)
from app.platform.plugins.hooks import ExtensionPoint
from app.platform.plugins.lifecycle import (
    PluginInstallation,
    PluginState,
)
from app.platform.plugins.lifecycle import (
    install as install_fresh,
)
from app.platform.plugins.limits import DEFAULT_CEILING, ResourceLimits
from app.platform.plugins.manifest import HOST_API_VERSION, PluginManifest
from app.platform.plugins.marketplace import (
    RatingStats,
    ReviewDecision,
    ReviewStatus,
    apply_review,
    initial_review_status,
)
from app.platform.plugins.registry import DispatchReport, HookRegistry
from app.platform.plugins.resolver import DependencyResolver, ResolutionResult
from app.platform.plugins.runtime import PluginRuntime
from app.platform.plugins.signing import Signature, Signer, artifact_digest, verify_signature
from app.platform.plugins.store import (
    AuditStore,
    InstallationStore,
    RatingStore,
    RegistryStore,
    ReviewStore,
)
from app.platform.plugins.version import Version


@dataclass(slots=True)
class PluginRepos:
    """The repository bundle one unit-of-work exposes to the service."""

    registry: RegistryStore
    installations: InstallationStore
    ratings: RatingStore
    reviews: ReviewStore
    audit: AuditStore


#: A unit-of-work factory: an async context manager yielding :class:`PluginRepos`
#: and committing on clean exit. Built by the composition root from the DB
#: session factory; the service stays storage-agnostic.
UnitOfWork = Callable[[], "PluginUnitOfWork"]


class PluginUnitOfWork:
    """Async-context bundling the repos over one committing DB session."""

    def __init__(self, session_factory: Callable[[], object]) -> None:
        self._session_factory = session_factory
        self._session: object | None = None

    async def __aenter__(self) -> PluginRepos:
        self._cm = self._session_factory()
        self._session = await self._cm.__aenter__()  # type: ignore[attr-defined]
        s = self._session
        return PluginRepos(
            registry=RegistryStore(s),  # type: ignore[arg-type]
            installations=InstallationStore(s),  # type: ignore[arg-type]
            ratings=RatingStore(s),  # type: ignore[arg-type]
            reviews=ReviewStore(s),  # type: ignore[arg-type]
            audit=AuditStore(s),  # type: ignore[arg-type]
        )

    async def __aexit__(self, *exc: object) -> None:
        await self._cm.__aexit__(*exc)  # type: ignore[attr-defined]


@dataclass(slots=True)
class PluginPlatformConfig:
    """Host policy governing the platform."""

    #: Require a verified signature on every publish (production default True).
    require_signature: bool = False
    #: Auto-approve freshly published LOW-risk plugins (skips the review queue).
    auto_approve_low_risk: bool = True
    #: The operator's resource ceiling; manifest requests are clamped to this.
    resource_ceiling: ResourceLimits = field(default_factory=lambda: DEFAULT_CEILING)
    #: The maximum risk tier a tenant may grant without an admin override.
    max_grantable_risk: RiskTier = RiskTier.HIGH
    #: When a hook fails this many times in production, it is quarantined.
    quarantine_threshold: int = 5


@dataclass(slots=True)
class InstallPlan:
    """The result of planning an install — what would be granted + the order."""

    manifest: PluginManifest
    resolution: ResolutionResult
    granted: GrantSet
    effective_limits: ResourceLimits


class PluginService:
    """High-level operations over the plugin platform."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        config: PluginPlatformConfig | None = None,
        signer: Signer | None = None,
        host_services_factory: Callable[[str, str], HostServices] | None = None,
        runtime: PluginRuntime | None = None,
    ) -> None:
        self._uow = uow
        self._config = config or PluginPlatformConfig()
        self._signer = signer
        self._host_services_factory = host_services_factory or (lambda owner, pid: HostServices())
        self._runtime = runtime or PluginRuntime()

    @property
    def config(self) -> PluginPlatformConfig:
        return self._config

    # ------------------------------------------------------------------ #
    # Publishing / marketplace
    # ------------------------------------------------------------------ #

    async def publish(
        self,
        *,
        manifest_data: dict[str, object],
        source: str,
        signature: dict[str, object] | None = None,
        actor: str | None = None,
    ) -> dict[str, object]:
        """Validate + persist a new artifact. Returns the catalog entry summary."""
        manifest = PluginManifest.parse(manifest_data)
        if not manifest.supports_host(HOST_API_VERSION):
            raise PluginValidationError(
                f"plugin targets host API {manifest.api_version}, this host is {HOST_API_VERSION}"
            )
        # Validate the source compiles in the sandbox (no execution side effects
        # beyond top-level body; a malformed plugin is rejected at publish).
        self._runtime.load(
            plugin_id=manifest.id,
            version=str(manifest.version),
            source=source,
            import_allowlist=manifest.import_allowlist,
            entry_module=manifest.entry_module,
        )
        digest = artifact_digest(manifest.to_dict(), source)
        sig_obj = self._verify_publish_signature(manifest, source, signature, digest)
        status = initial_review_status(
            manifest, auto_approve_low_risk=self._config.auto_approve_low_risk
        )
        async with self._uow() as repos:
            row = await repos.registry.publish(
                manifest=manifest,
                source=source,
                digest=digest,
                status=status,
                signature=sig_obj,
            )
            await repos.audit.record(
                plugin_id=manifest.id,
                action="publish",
                actor=actor,
                summary=f"published {manifest.ref} ({status.value})",
                detail={"digest": digest, "signed": sig_obj is not None},
            )
        return _entry_summary(row)

    def _verify_publish_signature(
        self,
        manifest: PluginManifest,
        source: str,
        signature: dict[str, object] | None,
        digest: str,
    ) -> Signature | None:
        if signature is None:
            if self._config.require_signature:
                raise SignatureError("publish requires a signature but none was provided")
            return None
        if self._signer is None:
            raise SignatureError("no signer configured to verify the artifact signature")
        sig = Signature.from_dict(signature)
        verify_signature(self._signer, sig, manifest=manifest.to_dict(), source=source)
        # Defence in depth: the verified digest must equal the recomputed one.
        if sig.digest != digest:  # pragma: no cover - verify already checks this
            raise SignatureError("signature digest mismatch")
        return sig

    async def review(
        self,
        *,
        plugin_id: str,
        version: str,
        decision: str,
        reviewer: str | None,
        notes: str = "",
    ) -> dict[str, object]:
        """Apply a moderation decision to a published artifact."""
        dec = ReviewDecision(decision)
        async with self._uow() as repos:
            row = await repos.registry.get(plugin_id, version)
            new_status = apply_review(ReviewStatus(row.status), dec)
            await repos.registry.set_status(plugin_id, version, new_status)
            await repos.reviews.record(
                plugin_id=plugin_id,
                version=version,
                decision=dec.value,
                reviewer=reviewer,
                notes=notes,
            )
            await repos.audit.record(
                plugin_id=plugin_id,
                action=f"review.{dec.value}",
                actor=reviewer,
                summary=f"{plugin_id}@{version} -> {new_status.value}",
            )
            row = await repos.registry.get(plugin_id, version)
            return _entry_summary(row)

    async def rate(
        self, *, plugin_id: str, user_id: str, stars: int, review: str = ""
    ) -> RatingStats:
        """Record/update a user's rating and refresh the registry aggregate."""
        async with self._uow() as repos:
            count_delta, sum_delta = await repos.ratings.upsert(
                plugin_id=plugin_id, user_id=user_id, stars=stars, review=review
            )
            return await repos.registry.apply_rating_delta(
                plugin_id, count_delta=count_delta, sum_delta=sum_delta
            )

    async def catalog(
        self, *, include_pending: bool = False, limit: int = 100, offset: int = 0
    ) -> list[dict[str, object]]:
        async with self._uow() as repos:
            rows = await repos.registry.list_catalog(
                include_pending=include_pending, limit=limit, offset=offset
            )
            return [_entry_summary(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Install / enable / lifecycle
    # ------------------------------------------------------------------ #

    async def plan_install(
        self, *, plugin_id: str, version: str, requested_grants: list[str] | None = None
    ) -> InstallPlan:
        """Plan (without persisting) an install: resolve deps + compute grants."""
        async with self._uow() as repos:
            entry = await repos.registry.get(plugin_id, version)
            if not ReviewStatus(entry.status).is_installable:
                raise LifecycleError(
                    f"{plugin_id}@{version} is not installable (status={entry.status})"
                )
            manifest = PluginManifest.parse(entry.manifest)
            available = await repos.registry.available_plugins()
        resolution = DependencyResolver(available).resolve(manifest)
        granted = self._compute_grants(manifest, requested_grants)
        effective = manifest.limits.clamp_to(self._config.resource_ceiling)
        return InstallPlan(
            manifest=manifest,
            resolution=resolution,
            granted=granted,
            effective_limits=effective,
        )

    def _compute_grants(self, manifest: PluginManifest, requested: list[str] | None) -> GrantSet:
        """The grant set to install with: requested ∩ declared, clamped to policy.

        A tenant can grant *at most* what the manifest declares (you cannot grant
        a capability the plugin never asked for) and *at most* its own
        max-grantable-risk tier (HIGH-risk grants need an admin override). When
        ``requested`` is None the full declared set is granted (subject to risk).
        """
        declared = manifest.capabilities
        if requested is None:
            chosen = declared
        else:
            req = GrantSet.from_iterable(requested)
            if not req.is_subset_of(declared):
                raise PluginValidationError("requested grants exceed what the manifest declares")
            chosen = req
        ceiling = self._config.max_grantable_risk
        for scope in chosen.grants:
            if risk_of(scope).rank > ceiling.rank:
                raise PluginValidationError(
                    f"capability {scope!r} ({risk_of(scope).value}) exceeds the grantable"
                    f" ceiling ({ceiling.value})"
                )
        return chosen

    async def install(
        self,
        *,
        owner: str,
        plugin_id: str,
        version: str,
        requested_grants: list[str] | None = None,
        enable: bool = False,
        actor: str | None = None,
    ) -> PluginInstallation:
        """Install (and optionally enable) a plugin for ``owner``."""
        plan = await self.plan_install(
            plugin_id=plugin_id, version=version, requested_grants=requested_grants
        )
        async with self._uow() as repos:
            existing = await repos.installations.get(owner, plugin_id)
            if existing is not None and existing.state is not PluginState.UNINSTALLED:
                raise LifecycleError(f"{plugin_id} is already installed for {owner!r}")
            installation = install_fresh(plugin_id, Version.parse(version))
            if enable:
                installation = installation.enable()
            await repos.installations.save(
                owner, installation, granted=list(plan.granted.to_sorted())
            )
            await repos.registry.bump_install_count(plugin_id, version)
            await repos.audit.record(
                plugin_id=plugin_id,
                action="install",
                actor=actor,
                summary=f"installed {plugin_id}@{version} for {owner} (enable={enable})",
                detail={"granted": list(plan.granted.to_sorted())},
            )
            return installation

    async def enable(
        self, *, owner: str, plugin_id: str, actor: str | None = None
    ) -> PluginInstallation:
        return await self._transition(owner, plugin_id, "enable", lambda i: i.enable(), actor)

    async def disable(
        self, *, owner: str, plugin_id: str, actor: str | None = None
    ) -> PluginInstallation:
        return await self._transition(owner, plugin_id, "disable", lambda i: i.disable(), actor)

    async def uninstall(
        self, *, owner: str, plugin_id: str, actor: str | None = None
    ) -> PluginInstallation:
        return await self._transition(owner, plugin_id, "uninstall", lambda i: i.uninstall(), actor)

    async def upgrade(
        self, *, owner: str, plugin_id: str, to_version: str, actor: str | None = None
    ) -> PluginInstallation:
        """Upgrade an installed plugin to ``to_version`` (re-resolves deps)."""
        target = Version.parse(to_version)
        async with self._uow() as repos:
            current = await repos.installations.get(owner, plugin_id)
            if current is None:
                raise PluginNotFoundError(f"{plugin_id} is not installed for {owner!r}")
            entry = await repos.registry.get(plugin_id, to_version)
            if not ReviewStatus(entry.status).is_installable:
                raise LifecycleError(f"{plugin_id}@{to_version} is not installable")
            manifest = PluginManifest.parse(entry.manifest)
            available = await repos.registry.available_plugins()
            DependencyResolver(available).resolve(manifest)  # raises on conflict
            was_enabled = current.state is PluginState.ENABLED
            upgrading = current.begin_upgrade(target)
            # Commit the upgrade by re-enabling at the new version (if it was on).
            committed = upgrading.enable() if was_enabled else upgrading
            granted = list(self._compute_grants(manifest, None).to_sorted())
            await repos.installations.save(owner, committed, granted=granted)
            await repos.audit.record(
                plugin_id=plugin_id,
                action="upgrade",
                actor=actor,
                summary=f"upgraded {plugin_id} -> {to_version} for {owner}",
            )
            return committed

    async def rollback(
        self, *, owner: str, plugin_id: str, to_version: str | None = None, actor: str | None = None
    ) -> PluginInstallation:
        """Roll an installed plugin back to a prior version in its ledger."""
        async with self._uow() as repos:
            current = await repos.installations.get(owner, plugin_id)
            if current is None:
                raise PluginNotFoundError(f"{plugin_id} is not installed for {owner!r}")
            target = Version.parse(to_version) if to_version else None
            rolled = current.rollback(to=target)
            entry = await repos.registry.get(plugin_id, str(rolled.version))
            manifest = PluginManifest.parse(entry.manifest)
            granted = list(self._compute_grants(manifest, None).to_sorted())
            await repos.installations.save(owner, rolled, granted=granted)
            await repos.audit.record(
                plugin_id=plugin_id,
                action="rollback",
                actor=actor,
                summary=f"rolled back {plugin_id} -> {rolled.version} for {owner}",
            )
            return rolled

    async def record_runtime_failure(self, *, owner: str, plugin_id: str) -> PluginInstallation:
        """Bump the failure counter (the circuit breaker) after a hook error."""
        async with self._uow() as repos:
            current = await repos.installations.get(owner, plugin_id)
            if current is None:
                raise PluginNotFoundError(f"{plugin_id} is not installed for {owner!r}")
            updated = current.record_failure(quarantine_threshold=self._config.quarantine_threshold)
            granted = await repos.installations.granted_capabilities(owner, plugin_id)
            await repos.installations.save(owner, updated, granted=granted)
            if (
                updated.state is PluginState.QUARANTINED
                and current.state is not PluginState.QUARANTINED
            ):
                await repos.audit.record(
                    plugin_id=plugin_id,
                    action="quarantine",
                    actor="system",
                    summary=f"{plugin_id} quarantined for {owner} after repeated failures",
                )
            return updated

    async def _transition(
        self,
        owner: str,
        plugin_id: str,
        action: str,
        fn: Callable[[PluginInstallation], PluginInstallation],
        actor: str | None,
    ) -> PluginInstallation:
        async with self._uow() as repos:
            current = await repos.installations.get(owner, plugin_id)
            if current is None:
                raise PluginNotFoundError(f"{plugin_id} is not installed for {owner!r}")
            updated = fn(current)
            granted = await repos.installations.granted_capabilities(owner, plugin_id)
            await repos.installations.save(owner, updated, granted=granted)
            await repos.audit.record(
                plugin_id=plugin_id,
                action=action,
                actor=actor,
                summary=f"{action} {plugin_id} for {owner} -> {updated.state.value}",
            )
            return updated

    async def list_installations(self, *, owner: str) -> list[dict[str, object]]:
        async with self._uow() as repos:
            installs = await repos.installations.list_all(owner)
            return [_installation_summary(i) for i in installs]

    # ------------------------------------------------------------------ #
    # Sandbox hydration + dispatch
    # ------------------------------------------------------------------ #

    async def build_registry(self, *, owner: str) -> HookRegistry:
        """Compile every ENABLED plugin for ``owner`` into a live HookRegistry."""
        registry = HookRegistry(runtime=self._runtime)
        async with self._uow() as repos:
            active = await repos.installations.list_active(owner)
            for installation in active:
                version = str(installation.version)
                try:
                    entry = await repos.registry.get(installation.plugin_id, version)
                except PluginNotFoundError:
                    continue
                manifest = PluginManifest.parse(entry.manifest)
                granted = GrantSet.from_iterable(
                    await repos.installations.granted_capabilities(owner, installation.plugin_id)
                )
                limits = manifest.limits.clamp_to(self._config.resource_ceiling)
                services = self._host_services_factory(owner, installation.plugin_id)
                plugin = self._runtime.load(
                    plugin_id=manifest.id,
                    version=version,
                    source=entry.source,
                    import_allowlist=manifest.import_allowlist,
                    entry_module=manifest.entry_module,
                )
                registry.register_plugin(
                    plugin=plugin,
                    hooks=manifest.hooks,
                    grants=granted,
                    limits=limits,
                    services=services,
                )
        return registry

    async def dispatch(
        self, *, owner: str, point: ExtensionPoint, payload: object, fail_fast: bool = False
    ) -> DispatchReport:
        """Build the tenant's registry and dispatch ``point`` over ``payload``."""
        registry = await self.build_registry(owner=owner)
        return registry.dispatch(point, payload, fail_fast=fail_fast)


def _entry_summary(row: PluginRegistryEntry) -> dict[str, object]:
    return {
        "plugin_id": row.plugin_id,
        "version": row.version,
        "name": row.name,
        "publisher": row.publisher,
        "status": row.status,
        "max_risk": row.max_risk,
        "yanked": row.yanked,
        "signed": row.signed,
        "digest": row.digest,
        "rating_average": (
            round(row.rating_sum / row.rating_count, 3) if row.rating_count else 0.0
        ),
        "rating_count": row.rating_count,
        "install_count": row.install_count,
    }


def _installation_summary(installation: PluginInstallation) -> dict[str, object]:
    return {
        "plugin_id": installation.plugin_id,
        "version": str(installation.version),
        "state": installation.state.value,
        "failure_count": installation.failure_count,
        "history": [r.to_dict() for r in installation.history],
        "active": installation.is_active,
    }


__all__ = [
    "InstallPlan",
    "PluginPlatformConfig",
    "PluginRepos",
    "PluginService",
    "PluginUnitOfWork",
    "UnitOfWork",
]
