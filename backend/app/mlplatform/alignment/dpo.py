"""Direct Preference Optimization — a policy trained straight from preferences.

DPO (Rafailov et al., 2023) skips the explicit-reward + RL loop and optimizes the
policy directly on pairwise preferences against a *frozen reference* policy. The
loss for a preference ``y_w ≻ y_l`` given prompt context is

    L = -log σ( β · [ (log π_θ(y_w) - log π_ref(y_w))
                     -(log π_θ(y_l) - log π_ref(y_l)) ] )

i.e. push the policy's *implicit reward* (the β-scaled log-ratio to the reference)
to rank the winner above the loser, while the reference anchor stops the policy
running away from a sensible base distribution.

For Kinora this is **deterministic and simulated**: there is no language model to
fine-tune in this offline platform. The "policy" is a log-linear scorer
``s_θ(φ) = θ·φ`` over a candidate clip's feature vector ``φ``; the implicit policy
log-prob of a candidate is its score minus a (cancelling) partition term, so the
DPO log-ratio reduces to ``(θ - θ_ref)·(φ_w - φ_l)`` — an exact, closed-form,
differentiable objective we optimize by full-batch gradient descent with a fixed
init and fixed step count. No sampling, no network, zero credits.

The result is :class:`DPOPolicy`, whose ``score`` ranks candidates and whose
``implicit_reward`` exposes the β-scaled log-ratio that *is* the learned reward —
the bridge that lets policy-evaluation (``policy.py``) reuse the reward-model
machinery. Distinct from the reward model: that learns ``r(x)``; this learns a
*policy* ``π_θ`` regularized to a reference, which is the object the over-
optimization guardrails actually constrain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import ConvergenceError, DataError
from .linalg import EPS, Features, Float, FloatArray, Standardizer, log_sigmoid, sigmoid
from .types import PreferenceDataset


@dataclass(frozen=True)
class DPOConfig:
    """Hyper-parameters for :class:`DPOTrainer`.

    * ``beta`` — the KL-regularization temperature; smaller = stay closer to the
      reference, larger = trust the preferences more (and risk over-optimizing).
    * ``lr`` / ``steps`` — full-batch gradient-descent schedule (deterministic).
    * ``l2`` — extra ridge on the *deviation from the reference* (a second anchor).
    """

    beta: float = 0.1
    lr: float = 0.5
    steps: int = 500
    l2: float = 0.0
    tol: float = 1e-9

    def __post_init__(self) -> None:
        if self.beta <= 0:
            raise DataError(f"beta must be > 0, got {self.beta}")
        if self.lr <= 0:
            raise DataError(f"lr must be > 0, got {self.lr}")
        if self.steps < 1:
            raise DataError(f"steps must be >= 1, got {self.steps}")
        if self.l2 < 0:
            raise DataError(f"l2 must be >= 0, got {self.l2}")


@dataclass(frozen=True)
class DPOPolicy:
    """A fitted log-linear preference policy with a frozen reference anchor.

    ``theta`` / ``theta_ref`` live in the *standardized* feature space. ``score``
    is the policy's preference for a candidate; ``implicit_reward`` is the β-scaled
    log-ratio to the reference — the DPO-implied reward used by policy evaluation.
    """

    theta: FloatArray
    theta_ref: FloatArray
    beta: float
    standardizer: Standardizer
    dim: int
    converged: bool = True
    final_loss: float = 0.0

    def _phi(self, features: Features) -> FloatArray:
        x = np.atleast_2d(np.asarray(features, dtype=Float))
        if x.shape[1] != self.dim:
            raise DataError(f"expected {self.dim} features, got {x.shape[1]}")
        return self.standardizer.transform(x)

    def score(self, features: Features) -> float:
        """The policy's scalar preference score for one candidate."""

        return float((self._phi(features) @ self.theta).ravel()[0])

    def implicit_reward(self, features: Features) -> float:
        """β·(s_θ - s_ref) — the reward DPO implicitly learns (Rafailov §5)."""

        phi = self._phi(features).ravel()
        return float(self.beta * (phi @ self.theta - phi @ self.theta_ref))

    def prefer_prob(self, winner: Features, loser: Features) -> float:
        """Model's ``P(winner ≻ loser)`` under the implicit-reward BT head."""

        rw = self.implicit_reward(winner)
        rl = self.implicit_reward(loser)
        return float(sigmoid(np.array([rw - rl]))[0])

    def deviation(self) -> float:
        """L2 distance of the policy from its reference (an over-opt proxy)."""

        return float(np.linalg.norm(self.theta - self.theta_ref))

    def to_dict(self) -> dict[str, object]:
        return {
            "theta": [float(t) for t in self.theta],
            "theta_ref": [float(t) for t in self.theta_ref],
            "beta": self.beta,
            "mean": [float(m) for m in self.standardizer.mean],
            "scale": [float(s) for s in self.standardizer.scale],
            "dim": self.dim,
            "converged": self.converged,
            "final_loss": self.final_loss,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> DPOPolicy:
        return cls(
            theta=np.array(d["theta"], dtype=Float),
            theta_ref=np.array(d["theta_ref"], dtype=Float),
            beta=float(d["beta"]),  # type: ignore[arg-type]
            standardizer=Standardizer(
                mean=np.array(d["mean"], dtype=Float),
                scale=np.array(d["scale"], dtype=Float),
            ),
            dim=int(d["dim"]),  # type: ignore[call-overload]
            converged=bool(d.get("converged", True)),
            final_loss=float(d.get("final_loss", 0.0)),  # type: ignore[arg-type]
        )


@dataclass
class DPOTrainer:
    """Fits :class:`DPOPolicy`s from preference data by deterministic gradient descent.

    The objective is the mean DPO loss over the pairs plus an optional ridge on
    ``theta - theta_ref``. Because the log-linear reduction makes the loss convex
    in ``theta``, plain GD with a fixed step converges to the global optimum; the
    history of losses is monotone non-increasing once past any transient, which
    the tests assert.
    """

    config: DPOConfig = field(default_factory=DPOConfig)

    def fit(
        self,
        pairs: PreferenceDataset,
        *,
        reference: DPOPolicy | None = None,
        strict: bool = False,
    ) -> DPOPolicy:
        """Optimize the DPO loss; optionally anchored to an existing ``reference``."""

        if len(pairs) == 0:
            raise DataError("DPO needs at least one preference pair")
        cfg = self.config
        winners = np.array([p.winner for p in pairs], dtype=Float)
        losers = np.array([p.loser for p in pairs], dtype=Float)
        strengths = np.array([p.strength for p in pairs], dtype=Float)

        std = (
            reference.standardizer
            if reference is not None
            else Standardizer.fit(np.vstack([winners, losers]))
        )
        dim = winners.shape[1]
        if reference is not None and reference.dim != dim:
            raise DataError("reference dim mismatch with preference data")
        phi_w = std.transform(winners)
        phi_l = std.transform(losers)
        delta = phi_w - phi_l  # (n, dim) — the only thing the loss depends on

        theta_ref = (
            reference.theta.copy()
            if reference is not None
            else np.zeros(dim, dtype=Float)
        )
        theta = theta_ref.copy()
        w = strengths / float(strengths.sum())  # normalized pair weights

        def _loss(th: FloatArray) -> float:
            # implicit-reward margin = beta * (th - th_ref) · delta
            margin = cfg.beta * (delta @ (th - theta_ref))
            nll = float(-np.sum(w * log_sigmoid(margin)))
            ridge = 0.5 * cfg.l2 * float(np.sum((th - theta_ref) ** 2))
            return nll + ridge

        loss = _loss(theta)
        converged = False
        for _step in range(cfg.steps):
            margin = cfg.beta * (delta @ (theta - theta_ref))
            # d/dθ [-log σ(margin)] = -σ(-margin) * dmargin/dθ
            #   = -(1 - σ(margin)) * beta * delta
            coeff = -(1.0 - sigmoid(margin)) * cfg.beta  # (n,)
            grad = (delta.T @ (w * coeff)) + cfg.l2 * (theta - theta_ref)
            new_theta = theta - cfg.lr * grad
            new_loss = _loss(new_theta)
            # Backtracking guard: if a step increased the loss, shrink it.
            shrink = cfg.lr
            tries = 0
            while new_loss > loss + 1e-15 and tries < 30:
                shrink *= 0.5
                new_theta = theta - shrink * grad
                new_loss = _loss(new_theta)
                tries += 1
            theta = new_theta
            if abs(loss - new_loss) <= cfg.tol * (1.0 + abs(loss)):
                loss = new_loss
                converged = True
                break
            loss = new_loss

        if strict and not converged:
            raise ConvergenceError(f"DPO did not converge in {cfg.steps} steps")
        return DPOPolicy(
            theta=theta,
            theta_ref=theta_ref,
            beta=cfg.beta,
            standardizer=std,
            dim=dim,
            converged=converged,
            final_loss=loss,
        )

    @staticmethod
    def reference_from_reward(
        weights: FloatArray, standardizer: Standardizer, beta: float
    ) -> DPOPolicy:
        """Seed a DPO *reference* from a reward model's standardized weights.

        Lets the policy be anchored to "what the reward model already believes",
        so DPO refines rather than relearns. The bias coefficient (``weights[0]``)
        is dropped because a constant cancels in every preference difference.
        """

        theta = np.asarray(weights, dtype=Float)[1:] / max(beta, EPS)
        return DPOPolicy(
            theta=theta.copy(),
            theta_ref=theta.copy(),
            beta=beta,
            standardizer=standardizer,
            dim=theta.shape[0],
            converged=True,
        )


def dpo_loss(
    policy: DPOPolicy, pairs: PreferenceDataset
) -> float:
    """Mean DPO loss of ``policy`` over ``pairs`` (lower = better ranking)."""

    if len(pairs) == 0:
        raise DataError("dpo_loss needs at least one pair")
    total = 0.0
    for p in pairs:
        margin = policy.implicit_reward(p.winner) - policy.implicit_reward(p.loser)
        total += float(-log_sigmoid(np.array([margin]))[0]) * p.strength
    weight = sum(p.strength for p in pairs)
    return total / weight


def preference_accuracy(policy: DPOPolicy, pairs: PreferenceDataset) -> float:
    """Fraction of pairs the policy ranks correctly (winner scored above loser)."""

    if len(pairs) == 0:
        raise DataError("preference_accuracy needs at least one pair")
    correct = 0
    for p in pairs:
        if policy.implicit_reward(p.winner) > policy.implicit_reward(p.loser):
            correct += 1
    return correct / len(pairs)
