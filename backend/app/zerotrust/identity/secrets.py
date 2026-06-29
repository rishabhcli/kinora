"""Secret storage + dynamic secrets with lease/renew/revoke (Vault-shaped).

Two complementary capabilities, both encrypted at rest through the KMS envelope:

* **Static KV secrets** (:class:`SecretStore`) — versioned key→value storage at a
  path (``providers/dashscope``), like Vault's KV-v2: every write makes a new
  version, old versions are readable until pruned, and the stored bytes are
  KMS-sealed so the in-memory store never holds plaintext.
* **Dynamic secrets** (:class:`DynamicSecretEngine`) — short-lived credentials
  minted on demand from a registered generator (a DB-role engine, an
  object-store STS engine), each handed out under a **lease**. A lease has a TTL
  and a max-TTL; :meth:`renew` extends it up to the max; :meth:`revoke` kills it
  (and the engine's revoke hook destroys the underlying credential). The lease
  manager sweeps expired leases.

Everything is in-process and deterministic against the injected clock; the KMS is
the :class:`LocalKms` so the secret material is genuinely sealed (round-trips
through AES-GCM), proving the envelope path, not just stored in the clear.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.zerotrust.identity.clock import Clock, SystemClock
from app.zerotrust.identity.errors import (
    LeaseError,
    LeaseExpiredError,
    LeaseRevokedError,
    SecretError,
    SecretNotFoundError,
)
from app.zerotrust.identity.kms import KeyManagementService, WrappedKey

# --------------------------------------------------------------------------- #
# Static KV secrets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SecretVersion:
    """One immutable version of a stored secret (sealed)."""

    version: int
    wrapped: WrappedKey
    created_at: datetime
    destroyed: bool = False


@dataclass(slots=True)
class SecretStore:
    """A versioned, KMS-sealed key/value secret store (KV-v2 shaped)."""

    kms: KeyManagementService
    key_id: str
    clock: Clock = field(default_factory=SystemClock)
    _data: dict[str, list[SecretVersion]] = field(default_factory=dict)

    def put(self, path: str, value: Mapping[str, str] | bytes | str) -> int:
        """Write a new version at *path*. Returns the new version number."""

        plaintext = _encode_secret(value)
        wrapped = self.kms.encrypt(self.key_id, plaintext, aad=path.encode())
        versions = self._data.setdefault(path, [])
        version = (versions[-1].version + 1) if versions else 1
        versions.append(SecretVersion(version, wrapped, self.clock.now()))
        return version

    def get(self, path: str, *, version: int | None = None) -> bytes:
        """Read the (optionally pinned) version at *path* as raw bytes."""

        sv = self._version(path, version)
        if sv.destroyed:
            raise SecretNotFoundError(f"secret {path!r} v{sv.version} is destroyed")
        return self.kms.decrypt(sv.wrapped, aad=path.encode())

    def get_map(self, path: str, *, version: int | None = None) -> dict[str, str]:
        """Read a secret written as a mapping back into a dict."""

        raw = self.get(path, version=version)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretError(f"secret {path!r} is not a JSON map") from exc
        if not isinstance(obj, dict):
            raise SecretError(f"secret {path!r} is not a JSON map")
        return {str(k): str(v) for k, v in obj.items()}

    def latest_version(self, path: str) -> int:
        versions = self._data.get(path)
        if not versions:
            raise SecretNotFoundError(f"no secret at {path!r}")
        return versions[-1].version

    def list_versions(self, path: str) -> tuple[int, ...]:
        return tuple(v.version for v in self._data.get(path, ()))

    def destroy_version(self, path: str, version: int) -> None:
        """Permanently destroy one version (its sealed bytes become unreadable)."""

        versions = self._data.get(path)
        if not versions:
            raise SecretNotFoundError(f"no secret at {path!r}")
        for i, sv in enumerate(versions):
            if sv.version == version:
                versions[i] = SecretVersion(
                    sv.version, sv.wrapped, sv.created_at, destroyed=True
                )
                return
        raise SecretNotFoundError(f"secret {path!r} has no version {version}")

    def delete(self, path: str) -> None:
        """Remove a secret path entirely."""

        if path not in self._data:
            raise SecretNotFoundError(f"no secret at {path!r}")
        del self._data[path]

    def paths(self) -> frozenset[str]:
        return frozenset(self._data)

    def rewrap_all(self) -> int:
        """Re-wrap every sealed version under the KMS's current KEK version.

        Returns the count re-wrapped. The maintenance step after a KEK rotation
        so all stored secrets sit under the fresh key without exposing plaintext.
        """

        count = 0
        needs_rewrap = getattr(self.kms, "needs_rewrap", None)
        for path, versions in self._data.items():
            aad = path.encode()
            for i, sv in enumerate(versions):
                if sv.destroyed:
                    continue
                if needs_rewrap is not None and needs_rewrap(sv.wrapped):
                    versions[i] = SecretVersion(
                        sv.version,
                        self.kms.rewrap(sv.wrapped, aad=aad),
                        sv.created_at,
                    )
                    count += 1
        return count

    def _version(self, path: str, version: int | None) -> SecretVersion:
        versions = self._data.get(path)
        if not versions:
            raise SecretNotFoundError(f"no secret at {path!r}")
        if version is None:
            return versions[-1]
        for sv in versions:
            if sv.version == version:
                return sv
        raise SecretNotFoundError(f"secret {path!r} has no version {version}")


def _encode_secret(value: Mapping[str, str] | bytes | str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return json.dumps(dict(value), separators=(",", ":")).encode()


# --------------------------------------------------------------------------- #
# Dynamic secrets + leases
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GeneratedSecret:
    """What a dynamic-secret generator returns: the credential + an opaque handle.

    ``handle`` is whatever the engine needs at revoke time (a DB role name, an STS
    session id); it is passed back to the engine's revoke hook verbatim.
    """

    data: Mapping[str, str]
    handle: str = ""


@dataclass(slots=True)
class Lease:
    """A time-bounded grant over a dynamic secret."""

    lease_id: str
    role: str
    secret: GeneratedSecret
    issued_at: datetime
    expires_at: datetime
    max_expires_at: datetime
    renewable: bool = True
    revoked: bool = False

    def is_active(self, now: datetime) -> bool:
        return not self.revoked and now <= self.expires_at

    def remaining(self, now: datetime) -> timedelta:
        return max(self.expires_at - now, timedelta(0))


@dataclass(frozen=True, slots=True)
class DynamicSecretRole:
    """A registered generator: how to mint and how to revoke a credential."""

    name: str
    generate: Callable[[], GeneratedSecret]
    revoke: Callable[[GeneratedSecret], None] | None = None
    default_ttl: timedelta = timedelta(minutes=30)
    max_ttl: timedelta = timedelta(hours=4)
    renewable: bool = True


@dataclass(slots=True)
class DynamicSecretEngine:
    """Mints lease-bound dynamic secrets and manages their lifecycle."""

    clock: Clock = field(default_factory=SystemClock)
    _roles: dict[str, DynamicSecretRole] = field(default_factory=dict)
    _leases: dict[str, Lease] = field(default_factory=dict)

    def register_role(self, role: DynamicSecretRole) -> None:
        if role.max_ttl < role.default_ttl:
            raise LeaseError("role max_ttl must be >= default_ttl")
        self._roles[role.name] = role

    def issue(self, role_name: str, *, ttl: timedelta | None = None) -> Lease:
        """Generate a credential for *role_name* and grant a lease over it."""

        role = self._roles.get(role_name)
        if role is None:
            raise LeaseError(f"no such dynamic-secret role {role_name!r}")
        now = self.clock.now()
        grant = min(ttl or role.default_ttl, role.max_ttl)
        secret = role.generate()
        lease = Lease(
            lease_id=uuid.uuid4().hex,
            role=role_name,
            secret=secret,
            issued_at=now,
            expires_at=now + grant,
            max_expires_at=now + role.max_ttl,
            renewable=role.renewable,
        )
        self._leases[lease.lease_id] = lease
        return lease

    def lookup(self, lease_id: str) -> Lease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise LeaseError(f"no such lease {lease_id!r}")
        return lease

    def renew(self, lease_id: str, *, extend: timedelta | None = None) -> Lease:
        """Extend a lease by *extend* (default: the role TTL), capped at max-TTL."""

        lease = self.lookup(lease_id)
        now = self.clock.now()
        if lease.revoked:
            raise LeaseRevokedError(f"lease {lease_id!r} is revoked")
        if not lease.renewable:
            raise LeaseError(f"lease {lease_id!r} is not renewable")
        if now > lease.expires_at:
            raise LeaseExpiredError(f"lease {lease_id!r} already expired")
        role = self._roles[lease.role]
        bump = extend or role.default_ttl
        new_expiry = min(now + bump, lease.max_expires_at)
        if new_expiry <= lease.expires_at:
            # already at the max-TTL ceiling
            new_expiry = lease.max_expires_at
        lease.expires_at = new_expiry
        return lease

    def revoke(self, lease_id: str) -> None:
        """Revoke a lease and run the role's revoke hook on its credential."""

        lease = self.lookup(lease_id)
        if lease.revoked:
            return
        lease.revoked = True
        role = self._roles.get(lease.role)
        if role is not None and role.revoke is not None:
            role.revoke(lease.secret)

    def revoke_all(self, role_name: str | None = None) -> int:
        """Revoke every active lease (optionally only for one role)."""

        count = 0
        for lease in list(self._leases.values()):
            if lease.revoked:
                continue
            if role_name is not None and lease.role != role_name:
                continue
            self.revoke(lease.lease_id)
            count += 1
        return count

    def sweep_expired(self) -> int:
        """Revoke and drop leases whose TTL has elapsed. Returns the count swept."""

        now = self.clock.now()
        swept = 0
        for lease_id, lease in list(self._leases.items()):
            if not lease.revoked and now > lease.expires_at:
                self.revoke(lease_id)
                swept += 1
        return swept

    def active_leases(self) -> tuple[Lease, ...]:
        now = self.clock.now()
        return tuple(lease for lease in self._leases.values() if lease.is_active(now))

    def get_secret(self, lease_id: str) -> Mapping[str, str]:
        """Return the credential if the lease is still active, else raise."""

        lease = self.lookup(lease_id)
        now = self.clock.now()
        if lease.revoked:
            raise LeaseRevokedError(f"lease {lease_id!r} is revoked")
        if now > lease.expires_at:
            raise LeaseExpiredError(f"lease {lease_id!r} is expired")
        return lease.secret.data


__all__ = [
    "DynamicSecretEngine",
    "DynamicSecretRole",
    "GeneratedSecret",
    "Lease",
    "SecretStore",
    "SecretVersion",
]
