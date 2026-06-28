"""The versioned prompt registry — register / diff / rollback the crew's prompts.

The six agents own their *current* ``VersionedPrompt``s in ``app.agents.prompts``
(this package never edits those). The registry is the **external** system of
record that manages prompts as first-class versioned artifacts:

* it **seeds** itself from ``agents.prompts.PROMPTS`` so the agents' live prompts
  are the ``N.0.0`` baseline of each key (``adapter@v3`` ⇒ semver ``3.0.0``);
* operators **register** new candidate versions with a semver + changelog entry;
  the bump kind can be auto-suggested from the structural :mod:`diff`;
* every version is content-addressed (sha256 of the system text), so a no-op
  re-register is detected and rejected as a duplicate;
* **rollback** marks a prior version ``active`` again and records the rollback in
  the changelog — it never deletes history (append-only).

The registry is pure in-memory; :mod:`app.llmops.store` persists/loads it to the
``llmops_prompt_versions`` / ``llmops_changelog`` tables. Keeping the logic out of
the DB lets the whole thing be unit-tested with zero infra.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.llmops.diff import PromptDiff, diff_prompts, suggest_bump
from app.llmops.errors import (
    DuplicateVersionError,
    PromptNotFoundError,
    RollbackError,
)
from app.llmops.semver import SemVer


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class _PromptLike(Protocol):
    """Structural type of an agent ``VersionedPrompt`` (``.version`` + ``.system``).

    The attributes are declared read-only (``@property``) so a *frozen* dataclass
    like ``app.agents.prompts.VersionedPrompt`` structurally matches — a Protocol
    with mutable attributes would reject the frozen, read-only fields.
    """

    @property
    def version(self) -> str: ...

    @property
    def system(self) -> str: ...


class VersionStatus(StrEnum):
    """Lifecycle of a registered prompt version."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChangeKind(StrEnum):
    """The kind of registry event a changelog entry records."""

    SEED = "seed"
    REGISTER = "register"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """One immutable registered version of a prompt key."""

    key: str
    version: str  # canonical semver string
    prompt_tag: str  # the agents' ``key@vN`` tag this descends from (or synthetic)
    system: str
    sha256: str
    status: VersionStatus
    created_at: datetime

    @property
    def semver(self) -> SemVer:
        return SemVer.parse(self.version)


@dataclass(frozen=True, slots=True)
class ChangelogEntry:
    """An append-only record of a registry mutation."""

    key: str
    version: str
    kind: ChangeKind
    summary: str
    author: str
    created_at: datetime


@dataclass
class PromptRegistry:
    """In-memory registry of prompt keys → ordered versions, plus a changelog."""

    #: ``{key: {version: PromptRecord}}``
    _records: dict[str, dict[str, PromptRecord]] = field(default_factory=dict)
    #: ``{key: active version string}``
    _active: dict[str, str] = field(default_factory=dict)
    #: append-only changelog (chronological)
    _changelog: list[ChangelogEntry] = field(default_factory=list)

    # -- seeding ------------------------------------------------------------- #

    @classmethod
    def seeded_from_agents(cls) -> PromptRegistry:
        """Build a registry pre-loaded with the crew's live prompts as baselines.

        Reads ``app.agents.prompts.PROMPTS`` (the agents' source of truth) and
        registers each as the active ``N.0.0`` version of its key. This is the
        bridge that lets the registry *manage* the agents' prompts without ever
        editing ``agents/prompts.py``.
        """
        from app.agents.prompts import PROMPTS

        registry = cls()
        registry.seed(PROMPTS.items())
        return registry

    def seed(self, prompts: Iterable[tuple[str, _PromptLike]]) -> None:
        """Seed from ``(key, VersionedPrompt)`` pairs (duck-typed: ``.version`` + ``.system``)."""
        for key, prompt in prompts:
            tag: str = prompt.version
            system: str = prompt.system
            try:
                semver = SemVer.from_prompt_tag(tag)
            except Exception:  # noqa: BLE001 - synthetic tag fallback for non-standard tags
                semver = SemVer(1, 0, 0)
            self._insert(
                PromptRecord(
                    key=key,
                    version=str(semver),
                    prompt_tag=tag,
                    system=system,
                    sha256=_sha256(system),
                    status=VersionStatus.ACTIVE,
                    created_at=_now(),
                ),
                make_active=True,
                changelog=ChangeKind.SEED,
                summary=f"seeded {tag} as baseline {semver}",
                author="system",
            )

    # -- registration -------------------------------------------------------- #

    def register(
        self,
        key: str,
        system: str,
        *,
        bump: str | None = None,
        prompt_tag: str | None = None,
        author: str = "operator",
        summary: str | None = None,
        activate: bool = True,
    ) -> PromptRecord:
        """Register a new candidate version of ``key``.

        ``bump`` (``major``/``minor``/``patch``) chooses the next semver relative
        to the current latest; when omitted it is *suggested* from the structural
        diff against the active version (a brand-new key starts at ``1.0.0``).
        Re-registering an identical body (same sha256) raises
        :class:`DuplicateVersionError`.
        """
        existing = self._records.get(key)
        sha = _sha256(system)
        if existing:
            for record in existing.values():
                if record.sha256 == sha:
                    raise DuplicateVersionError(key, record.version)
            active = self.get_active(key)
            prompt_diff = diff_prompts(active.system, system)
            bump_kind = bump or suggest_bump(prompt_diff)
            next_version = active.semver.bump(bump_kind)
            # Never collide with an existing version: keep bumping the same axis.
            while str(next_version) in existing:
                next_version = next_version.bump(bump_kind)
            auto_summary = summary or prompt_diff.summary()
        else:
            next_version = SemVer(1, 0, 0)
            bump_kind = bump or "major"
            auto_summary = summary or "initial version"

        record = PromptRecord(
            key=key,
            version=str(next_version),
            prompt_tag=prompt_tag or f"{key}@v{next_version.major}",
            system=system,
            sha256=sha,
            status=VersionStatus.ACTIVE if activate else VersionStatus.DRAFT,
            created_at=_now(),
        )
        self._insert(
            record,
            make_active=activate,
            changelog=ChangeKind.REGISTER,
            summary=f"register {next_version} ({bump_kind}): {auto_summary}",
            author=author,
        )
        return record

    def _insert(
        self,
        record: PromptRecord,
        *,
        make_active: bool,
        changelog: ChangeKind,
        summary: str,
        author: str,
    ) -> None:
        self._records.setdefault(record.key, {})[record.version] = record
        if make_active:
            self._demote_active(record.key, except_version=record.version)
            self._active[record.key] = record.version
        self._changelog.append(
            ChangelogEntry(
                key=record.key,
                version=record.version,
                kind=changelog,
                summary=summary,
                author=author,
                created_at=_now(),
            )
        )

    def _demote_active(self, key: str, *, except_version: str) -> None:
        """Archive whatever was active so exactly one version is ACTIVE per key."""
        prev = self._active.get(key)
        if prev is not None and prev != except_version:
            old = self._records[key][prev]
            if old.status is VersionStatus.ACTIVE:
                self._records[key][prev] = replace(old, status=VersionStatus.ARCHIVED)

    # -- promotion / rollback ------------------------------------------------ #

    def promote(self, key: str, version: str, *, author: str = "operator") -> PromptRecord:
        """Make a DRAFT/ARCHIVED version the ACTIVE one (forward promotion)."""
        record = self.get(key, version)
        self._demote_active(key, except_version=version)
        promoted = replace(record, status=VersionStatus.ACTIVE)
        self._records[key][version] = promoted
        self._active[key] = version
        self._changelog.append(
            ChangelogEntry(
                key=key,
                version=version,
                kind=ChangeKind.PROMOTE,
                summary=f"promoted {version} to active",
                author=author,
                created_at=_now(),
            )
        )
        return promoted

    def rollback(
        self, key: str, *, to: str | None = None, author: str = "operator"
    ) -> PromptRecord:
        """Roll the active version *back* to a prior version.

        ``to`` names the target; when omitted it is the highest version strictly
        below the current active one. Rolling to the current or a *higher* version
        is rejected (use :meth:`promote` to roll forward).
        """
        active = self.get_active(key)
        if to is None:
            lower = [r.semver for r in self._records[key].values() if r.semver < active.semver]
            if not lower:
                raise RollbackError(f"{key!r} has no version below the active {active.version}")
            target = str(max(lower))
        else:
            target = to
            target_record = self.get(key, target)  # raises if missing
            if target_record.semver >= active.semver:
                raise RollbackError(
                    f"cannot roll back to {target} (>= active {active.version}); use promote()"
                )
        self._demote_active(key, except_version=target)
        rolled = replace(self._records[key][target], status=VersionStatus.ACTIVE)
        self._records[key][target] = rolled
        self._active[key] = target
        self._changelog.append(
            ChangelogEntry(
                key=key,
                version=target,
                kind=ChangeKind.ROLLBACK,
                summary=f"rolled back from {active.version} to {target}",
                author=author,
                created_at=_now(),
            )
        )
        return rolled

    # -- reads --------------------------------------------------------------- #

    def keys(self) -> list[str]:
        return sorted(self._records)

    def has(self, key: str) -> bool:
        return key in self._records

    def get(self, key: str, version: str) -> PromptRecord:
        versions = self._records.get(key)
        if versions is None:
            raise PromptNotFoundError(key)
        record = versions.get(version)
        if record is None:
            raise PromptNotFoundError(key, version)
        return record

    def get_active(self, key: str) -> PromptRecord:
        active = self._active.get(key)
        if active is None:
            raise PromptNotFoundError(key)
        return self._records[key][active]

    def versions(self, key: str, *, descending: bool = True) -> list[PromptRecord]:
        versions = self._records.get(key)
        if versions is None:
            raise PromptNotFoundError(key)
        ordered = sorted(versions.values(), key=lambda r: r.semver, reverse=descending)
        return ordered

    def latest(self, key: str) -> PromptRecord:
        """The highest semantic version registered for ``key`` (regardless of active)."""
        return self.versions(key, descending=True)[0]

    def diff(self, key: str, *, old: str, new: str) -> PromptDiff:
        """Structural diff between two registered versions of ``key``."""
        return diff_prompts(self.get(key, old).system, self.get(key, new).system)

    def changelog(self, key: str | None = None) -> list[ChangelogEntry]:
        """The changelog, optionally filtered to one key (chronological)."""
        if key is None:
            return list(self._changelog)
        return [e for e in self._changelog if e.key == key]

    def export_records(self) -> list[PromptRecord]:
        """All records (every key, every version) — used by the DB store."""
        return [r for versions in self._records.values() for r in versions.values()]


__all__ = [
    "ChangeKind",
    "ChangelogEntry",
    "PromptRecord",
    "PromptRegistry",
    "VersionStatus",
]
