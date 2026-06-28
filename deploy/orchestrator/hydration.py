"""Config & secret hydration with redaction (kinora.md §12.6).

Before a new version is provisioned the orchestrator hydrates its runtime
config: the §12.6 ``OSS_*`` / ``DASHSCOPE_*`` / ``REDIS_URL`` / ``DATABASE_URL``
env, plus the safety gates (``KINORA_LIVE_VIDEO``). Secrets come from a secret
store (Alibaba KMS / a ``.env`` in dev); this module:

* pulls non-secret config and secret values through two Protocols,
* validates that every **required** key is present (fail fast — never ship a
  half-configured render-worker),
* enforces the hard safety invariant that ``KINORA_LIVE_VIDEO`` is OFF unless
  *explicitly* turned on (mirrors the repo-wide rule), and
* produces a :class:`HydratedConfig` whose ``redacted()`` view is safe to log —
  secret values become ``***`` so the audit trail never leaks an OSS secret.

No real KMS/SDK import: the secret source is a Protocol filled by a fake in
tests and the simulator.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: Keys whose *values* must be redacted anywhere config is rendered/logged.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "DASHSCOPE_API_KEY",
        "OSS_AK",
        "OSS_SECRET",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "REDIS_URL",  # contains the broker password
        "DATABASE_URL",  # contains the db password
        "JWT_SECRET",
        "OPENAI_API_KEY",
    }
)

#: Config keys a render-worker deployment cannot start without (§12.6 table).
REQUIRED_RENDER_KEYS: frozenset[str] = frozenset(
    {
        "DASHSCOPE_API_KEY",
        "OSS_ENDPOINT",
        "OSS_AK",
        "OSS_SECRET",
        "OSS_BUCKET",
        "REDIS_URL",
        "DATABASE_URL",
    }
)


@runtime_checkable
class ConfigSource(Protocol):
    """Non-secret runtime config (env file, parameter store)."""

    def load(self) -> Mapping[str, str]:
        ...


@runtime_checkable
class SecretSource(Protocol):
    """Secret values (Alibaba KMS, sealed secrets, dev ``.env``)."""

    def fetch(self, keys: frozenset[str]) -> Mapping[str, str]:
        ...


class HydrationError(RuntimeError):
    """Raised when hydration cannot satisfy the required config contract."""


def _is_secret(key: str) -> bool:
    return key in SECRET_KEYS


def redact_value(key: str, value: str) -> str:
    return "***" if _is_secret(key) else value


@dataclass(frozen=True, slots=True)
class HydratedConfig:
    """The merged, validated runtime config for a deployment target."""

    values: Mapping[str, str]
    secret_keys: frozenset[str]

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    @property
    def live_video_enabled(self) -> bool:
        return self.values.get("KINORA_LIVE_VIDEO", "false").lower() in {"1", "true", "yes", "on"}

    def redacted(self) -> dict[str, str]:
        """A logging-safe view: secret values replaced with ``***``."""
        return {k: ("***" if k in self.secret_keys else v) for k, v in self.values.items()}

    def fingerprint(self) -> str:
        """A stable, secret-free fingerprint of the config (order-independent).

        Secret *presence* is included (key name) but never the value, so a
        config change is detectable in the audit trail without leaking secrets.
        """
        import hashlib

        parts = []
        for k in sorted(self.values):
            v = "***" if k in self.secret_keys else self.values[k]
            parts.append(f"{k}={v}")
        digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()
        return digest[:16]


@dataclass(slots=True)
class Hydrator:
    """Merges a :class:`ConfigSource` and :class:`SecretSource` into a
    :class:`HydratedConfig`, enforcing the required-key and safety contracts.
    """

    config_source: ConfigSource
    secret_source: SecretSource
    required_keys: frozenset[str] = field(default=REQUIRED_RENDER_KEYS)
    #: Hard gate: refuse to hydrate with KINORA_LIVE_VIDEO on unless allowed.
    allow_live_video: bool = False

    def hydrate(self) -> HydratedConfig:
        config = dict(self.config_source.load())
        needed_secrets = frozenset(k for k in self.required_keys if _is_secret(k)) | (
            SECRET_KEYS & frozenset(config)
        )
        secrets = dict(self.secret_source.fetch(needed_secrets))
        merged: dict[str, str] = {**config, **secrets}

        present_secret_keys = frozenset(k for k in merged if _is_secret(k))

        missing = sorted(k for k in self.required_keys if not merged.get(k))
        if missing:
            raise HydrationError(f"missing required config keys: {', '.join(missing)}")

        live = merged.get("KINORA_LIVE_VIDEO", "false").lower() in {"1", "true", "yes", "on"}
        if live and not self.allow_live_video:
            raise HydrationError(
                "KINORA_LIVE_VIDEO is on but the hydrator was not allowed to enable "
                "live video — refusing to deploy a credit-spending config"
            )
        # Normalise the gate to an explicit value so the audit trail is unambiguous.
        merged.setdefault("KINORA_LIVE_VIDEO", "false")

        return HydratedConfig(values=merged, secret_keys=present_secret_keys)


# ---------------------------------------------------------------------------
# In-memory sources for tests + the simulator (no KMS, no file I/O).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DictConfigSource:
    data: Mapping[str, str]

    def load(self) -> Mapping[str, str]:
        return dict(self.data)


@dataclass(slots=True)
class DictSecretSource:
    data: Mapping[str, str]

    def fetch(self, keys: frozenset[str]) -> Mapping[str, str]:
        # Return only the requested keys that exist (a real KMS would 404 the rest).
        return {k: self.data[k] for k in keys if k in self.data}
