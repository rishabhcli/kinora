"""Artifact promotion across the environment pipeline (kinora.md §12.6).

A build (an :class:`~deploy.orchestrator.models.Artifact`, content-addressed by
digest) is promoted dev → staging → prod. The pipeline enforces the gating
rules that keep prod safe:

* **Provenance / gap rule** — an artifact may only be promoted *into* an
  environment if it has already **succeeded** in the immediately lower one. You
  cannot ship a digest straight to prod that never ran in staging.
* **Same-digest invariant** — promotion never rebuilds; the *exact* digest that
  passed staging is what lands in prod (no "works on my machine" drift). This is
  the deployment analogue of the §8.7 ``shot_hash`` / §12.1 idempotency idea.
* **Soak rule (optional)** — an artifact must have been healthy in the lower
  environment for at least ``min_soak_s`` before it can be promoted up.

The pipeline only records *intent and provenance*; the actual rollout is run by
the :class:`~deploy.orchestrator.orchestrator.DeploymentOrchestrator`. It tracks,
per environment, the currently-live digest and the set of digests that have
succeeded there.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from deploy.orchestrator.models import Artifact, Environment


class PromotionRejectedError(RuntimeError):
    """Raised when a promotion violates a pipeline gating rule."""


@dataclass(frozen=True, slots=True)
class EnvRecord:
    """Per-environment promotion state."""

    environment: Environment
    live_digest: str | None
    succeeded_digests: frozenset[str]
    #: When each succeeded digest first became healthy here (for the soak rule).
    healthy_since: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class PromotionPipeline:
    """Tracks artifact promotion provenance across dev → staging → prod."""

    now: Callable[[], float]
    min_soak_s: float = 0.0
    require_lower_env: bool = True
    _live: dict[Environment, str | None] = field(default_factory=dict, init=False)
    _succeeded: dict[Environment, set[str]] = field(default_factory=dict, init=False)
    _healthy_since: dict[Environment, dict[str, float]] = field(default_factory=dict, init=False)
    _ordered: tuple[Environment, ...] = field(
        default=(Environment.DEV, Environment.STAGING, Environment.PROD), init=False
    )

    def __post_init__(self) -> None:
        for env in self._ordered:
            self._live.setdefault(env, None)
            self._succeeded.setdefault(env, set())
            self._healthy_since.setdefault(env, {})

    # -- queries -----------------------------------------------------------

    def live_digest(self, env: Environment) -> str | None:
        return self._live[env]

    def has_succeeded(self, env: Environment, digest: str) -> bool:
        return digest in self._succeeded[env]

    def record(self, env: Environment) -> EnvRecord:
        return EnvRecord(
            environment=env,
            live_digest=self._live[env],
            succeeded_digests=frozenset(self._succeeded[env]),
            healthy_since=dict(self._healthy_since[env]),
        )

    def _lower_env(self, env: Environment) -> Environment | None:
        idx = self._ordered.index(env)
        return self._ordered[idx - 1] if idx > 0 else None

    # -- gating ------------------------------------------------------------

    def check_promotable(self, artifact: Artifact, target: Environment) -> None:
        """Raise :class:`PromotionRejectedError` if ``artifact`` cannot enter ``target``.

        Pure check (no state change) so the orchestrator can validate before it
        starts touching infrastructure.
        """
        if self._live[target] == artifact.digest:
            # Already live with this exact digest → idempotent no-op is allowed.
            return

        lower = self._lower_env(target)
        if self.require_lower_env and lower is not None:
            if artifact.digest not in self._succeeded[lower]:
                raise PromotionRejectedError(
                    f"{artifact.short()} cannot enter {target.value}: it has not "
                    f"succeeded in {lower.value} first (gap rule)"
                )
            if self.min_soak_s > 0:
                since = self._healthy_since[lower].get(artifact.digest)
                if since is None:
                    raise PromotionRejectedError(
                        f"{artifact.short()} has no recorded soak start in {lower.value}"
                    )
                soaked = self.now() - since
                if soaked < self.min_soak_s:
                    raise PromotionRejectedError(
                        f"{artifact.short()} has only soaked {soaked:.0f}s in "
                        f"{lower.value}; needs {self.min_soak_s:.0f}s"
                    )

    def is_idempotent(self, artifact: Artifact, target: Environment) -> bool:
        """True iff this digest is already live in ``target`` (deploy is a no-op)."""
        return self._live[target] == artifact.digest

    # -- mutations (called by the orchestrator on outcomes) ----------------

    def mark_succeeded(self, artifact: Artifact, target: Environment) -> None:
        """Record that ``artifact`` is now the live, succeeded digest in ``target``."""
        self._succeeded[target].add(artifact.digest)
        self._live[target] = artifact.digest
        self._healthy_since[target].setdefault(artifact.digest, self.now())

    def mark_rolled_back(self, artifact: Artifact, target: Environment, *, to: str | None) -> None:
        """Record a rollback: ``target`` reverts to the prior live digest ``to``.

        The rolled-back digest is *not* added to ``succeeded`` for ``target`` and
        its soak clock there is cleared, so a later attempt to promote it up
        still requires it to first succeed here.
        """
        self._live[target] = to
        self._healthy_since[target].pop(artifact.digest, None)
