"""Secret-backend abstraction: resolution, caching, rotation, redaction.

Pure — env backend reads an injected mapping, file backend uses tmp_path, no
network. KINORA_LIVE_VIDEO is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.configmgmt.secrets import (
    EnvSecretBackend,
    FileSecretBackend,
    SecretNotFoundError,
    SecretRef,
    SecretResolver,
    SecretValue,
    StaticSecretBackend,
    env_resolver,
)
from app.core.logging import REDACTED


class _Clock:
    """A controllable monotonic clock for deterministic TTL tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- #
# SecretValue never prints itself
# --------------------------------------------------------------------------- #


def test_secret_value_is_redacted_in_repr_and_str() -> None:
    sv = SecretValue("super-secret", name="k")
    assert sv.reveal() == "super-secret"
    assert str(sv) == REDACTED
    assert REDACTED in repr(sv)
    assert "super-secret" not in repr(sv)
    assert bool(sv) is True
    assert len(sv) == len("super-secret")


def test_secret_value_equality_by_material() -> None:
    assert SecretValue("a", name="x") == SecretValue("a", name="y")
    assert SecretValue("a", name="x") != SecretValue("b", name="x")


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


def test_env_backend_upcases_and_prefixes() -> None:
    backend = EnvSecretBackend(prefix="kin_", _environ={"KIN_API_KEY": "v"})
    assert backend.fetch(SecretRef("api_key")) == "v"
    assert backend.fetch(SecretRef("missing")) is None


def test_file_backend_reads_and_strips_newline(tmp_path: Path) -> None:
    (tmp_path / "db_pw").write_text("hunter2\n", encoding="utf-8")
    backend = FileSecretBackend(tmp_path)
    assert backend.fetch(SecretRef("db_pw")) == "hunter2"
    assert backend.fetch(SecretRef("absent")) is None


def test_file_backend_versioned_then_falls_back(tmp_path: Path) -> None:
    (tmp_path / "tok").write_text("base", encoding="utf-8")
    (tmp_path / "tok.v2").write_text("pinned", encoding="utf-8")
    backend = FileSecretBackend(tmp_path)
    assert backend.fetch(SecretRef("tok", version="v2")) == "pinned"
    assert backend.fetch(SecretRef("tok", version="v9")) == "base"  # fallback


def test_static_backend_put_and_versioned() -> None:
    b = StaticSecretBackend()
    b.put("k", "cur")
    b.put("k", "old", version="v1")
    assert b.fetch(SecretRef("k")) == "cur"
    assert b.fetch(SecretRef("k", version="v1")) == "old"
    assert b.fetch(SecretRef("k", version="vX")) == "cur"  # version fallback


# --------------------------------------------------------------------------- #
# Resolver: chain, required/optional, reveal
# --------------------------------------------------------------------------- #


def test_resolver_tries_fallbacks_in_order() -> None:
    primary = StaticSecretBackend({"only_primary": "p"})
    fallback = EnvSecretBackend(_environ={"SHARED": "f"})
    r = SecretResolver(primary, fallbacks=(fallback,))
    assert r.resolve("only_primary").reveal() == "p"
    got = r.resolve("shared")
    assert got.reveal() == "f"
    assert got.source == "env"


def test_resolver_required_miss_raises() -> None:
    r = SecretResolver(StaticSecretBackend())
    with pytest.raises(SecretNotFoundError):
        r.resolve("nope")
    assert r.resolve_optional("nope") is None


def test_reveal_convenience() -> None:
    r = SecretResolver(StaticSecretBackend({"k": "v"}))
    assert r.reveal("k") == "v"


def test_env_resolver_factory() -> None:
    # env_resolver uses os.environ; just assert it builds an env-backed resolver.
    r = env_resolver(prefix="x_")
    assert r.backends[0].name == "env"


# --------------------------------------------------------------------------- #
# Caching + rotation
# --------------------------------------------------------------------------- #


def test_cache_hit_within_ttl_does_not_refetch() -> None:
    clock = _Clock()
    backend = StaticSecretBackend({"k": "v1"})
    r = SecretResolver(backend, ttl_s=100.0, clock=clock)
    assert r.resolve("k").reveal() == "v1"
    backend.put("k", "v2")  # change underlying store
    clock.now = 50.0  # still within TTL
    assert r.resolve("k").reveal() == "v1"  # served from cache
    clock.now = 150.0  # past TTL
    assert r.resolve("k").reveal() == "v2"  # re-fetched


def test_rotation_hook_fires_on_change_only() -> None:
    clock = _Clock()
    backend = StaticSecretBackend({"k": "v1"})
    r = SecretResolver(backend, ttl_s=0.0, clock=clock)  # never cache
    events: list[tuple[str, str | None, str]] = []
    r.add_rotation_hook(
        lambda name, old, new: events.append((name, old.reveal() if old else None, new.reveal()))
    )
    r.resolve("k")  # first resolution => old is None
    r.resolve("k")  # unchanged => no new event
    backend.put("k", "v2")
    r.resolve("k")  # rotated
    assert events == [("k", None, "v1"), ("k", "v1", "v2")]


def test_invalidate_forces_refetch() -> None:
    backend = StaticSecretBackend({"k": "v1"})
    r = SecretResolver(backend, ttl_s=1000.0)
    assert r.resolve("k").reveal() == "v1"
    backend.put("k", "v2")
    assert r.resolve("k").reveal() == "v1"  # cached
    r.invalidate("k")
    assert r.resolve("k").reveal() == "v2"


def test_invalidate_all() -> None:
    backend = StaticSecretBackend({"a": "1", "b": "2"})
    r = SecretResolver(backend, ttl_s=1000.0)
    r.resolve("a")
    r.resolve("b")
    backend.put("a", "1x")
    backend.put("b", "2x")
    r.invalidate()  # whole cache
    assert r.resolve("a").reveal() == "1x"
    assert r.resolve("b").reveal() == "2x"


def test_rotation_hook_failure_is_isolated() -> None:
    backend = StaticSecretBackend({"k": "v1"})
    r = SecretResolver(backend, ttl_s=0.0)

    def _boom(name: str, old: SecretValue | None, new: SecretValue) -> None:
        raise RuntimeError("hook exploded")

    r.add_rotation_hook(_boom)
    # The exploding hook must not break resolution.
    assert r.resolve("k").reveal() == "v1"
