# Alignment / preference-optimization platform (`backend/app/mlplatform/alignment/`)

A self-contained **RLHF / preference-optimization stack** layered *over* the
six-agent crew and the §9.5 Critic without touching them. It learns *what the
director actually wants* from the accept / reject / edit signals the system
already logs (§9.5 closed loop), optimizes prompt / policy candidates toward that
preference, and — critically — **guards the optimization against reward-hacking /
over-optimization** with KL bounds. This is facet **B** of the ML platform; facet
**A** (`app.mlplatform.data`) owns the canonical `Dataset` we consume.

Cites: kinora.md **§9.5** (the self-correcting Critic loop), **§10** (prompt
contracts), **§13** (metrics & the honest eval harness).

## Design constraints (per the worktree brief)

- **Owned NEW package.** Everything lives under `app/mlplatform/alignment/`. The
  only other new files are `app/mlplatform/__init__.py` and the
  `tests/test_mlplatform_alignment_*.py` suite. **No shared file is modified** —
  this package is additive-only and self-contained; it is *not* wired into
  `composition.py` / `config.py` / route tables (a thin lazy accessor would be the
  additive seam if/when the platform is surfaced, mirroring `llmops`).
- **Zero live calls, zero credits.** All math is pure NumPy with deterministic
  seeds. The only model/training seam — the fine-tuning executor — is an
  **injected protocol** (`FineTuneExecutor`); the only shipped implementation
  (`LocalExecutor`) runs this package's own pure trainers in-process. A hosted
  DashScope / OpenAI executor would implement the same protocol but is **not**
  shipped. `KINORA_LIVE_VIDEO` stays OFF.
- **Exhaustively tested math.** Every learner is unit-tested for correctness
  *and* convergence (recovers known separators, monotone loss, calibrated
  probabilities, deterministic re-runs, serialization round-trips).
- **Distinct from the Critic's reward sketch.** `app/render/reward.py` is the
  Critic's *clip-level* learned-reward advisory (logistic + BT + anomaly on the
  four §9.5 axes, advisory-only). This is the *platform*: trainable + serializable
  models, a DPO **policy** regularized to a reference, KL/over-optimization
  guardrails, an offline A/B win-rate harness, and a job orchestrator with
  experiment tracking.

## Module map (all under `app/mlplatform/alignment/`)

| Module | Responsibility | Status |
|---|---|---|
| `errors.py` | Exception hierarchy rooted at `AlignmentError` (`DataError`, `NotFittedError`, `ConvergenceError`, `GuardrailTripped`, `OrchestrationError`, `ExperimentError`). | ✅ |
| `types.py` | Value objects (`Sample`, `PreferencePair`) + datasets (`SampleDataset`, `PreferenceDataset`) + the `DatasetLike` structural protocol and `as_sample_dataset` adapter — **the seam that consumes facet A's `Dataset`** by duck-typing. | ✅ |
| `linalg.py` | Deterministic numerical core: stable `sigmoid`/`log_sigmoid`/`softplus`, IRLS/Newton `fit_logistic` (L2-regularized, line-searched), `Standardizer`, `expected_calibration_error`. | ✅ |
| `reward_model.py` | The director reward model: pointwise logistic + pairwise Bradley–Terry + a **combined** calibrated-and-ranked fit; `RewardMetrics` (accuracy / AUC / ECE / log-loss / pair-accuracy); serialization. | ✅ |
| `calibration.py` | Post-hoc probability calibration of any scorer: **Platt scaling** (1-D logistic) + **isotonic regression** (PAVA), with reliability-curve + ECE/Brier diagnostics. The "calibrated" in *calibrated reward model*. | ✅ |
| `dpo.py` | **Direct Preference Optimization**: a log-linear policy trained from preferences against a frozen reference; the implicit-reward log-ratio, deterministic GD with backtracking, `dpo_loss` / `preference_accuracy`. | ✅ |
| `policy.py` | Policy evaluation + **reward-hacking / over-optimization detection**: gold-vs-proxy `PolicyEvaluator`, `estimate_kl`, `over_optimization_report` (proxy-up/gold-down + Goodhart correlation), and the enforceable `KLGuardrail` (allow / warn / block). | ✅ |
| `abtest.py` | Offline **A/B + win-rate harness** (§13): gold-judged paired win-rate with a seeded bootstrap CI + exact sign-test p-value, plus a round-robin `tournament` ranking. | ✅ |
| `signals.py` | Turns raw director episodic events (`accept`/`reject`/`edit`/`degrade` + QA axes) into `Sample`s and `PreferencePair`s — the ingestion seam from §9.5 episodic memory into the learners. | ✅ |
| `acquisition.py` | **Active preference learning**: which unlabeled candidate pairs to ask the director to judge next (uncertainty / disagreement / diversity), so labeling budget buys the most signal. | ✅ |
| `experiments.py` | In-memory **experiment tracking**: `Experiment` / `Run` (params, time-series metrics, artifacts, parent lineage) + a query API (`best_run`, `children`, `query`). | ✅ |
| `orchestrator.py` | Provider-abstracted **fine-tuning-job orchestrator**: `FineTuneSpec` → strict-lifecycle `FineTuneJob` via an injected `FineTuneExecutor` (shipped: `LocalExecutor`), writing params/metrics/artifacts to the tracker. | ✅ |
| `service.py` | The **`AlignmentService` façade**: `train_reward_model` (tracked FT job) + `align_policy` (the full offline RLHF loop — gold reward → KL-swept DPO → over-optimization diagnosis → guardrail-gated best policy). | ✅ |

## The offline RLHF loop (`AlignmentService.align_policy`)

```
director signals ──▶ gold reward model (calibrated)            [reward_model + calibration]
preferences ─────▶ KL sweep of DPO policies (β small→large)    [dpo]
                    each policy evaluated vs the gold model      [policy.PolicyEvaluator]
                    over-optimization diagnosed across the sweep [policy.over_optimization_report]
                    best gold policy that clears the KL budget   [policy.KLGuardrail]
                    every step tracked                           [experiments]
```

The sweep makes Gao et al.'s over-optimization curve *observable*: gold reward
rises with KL, peaks, then falls as the policy starts hacking the proxy. The
guardrail refuses to promote any policy past the KL budget (or one that fails to
improve gold), so the platform fails *closed*. This is the §13 thesis ("a number
you committed to in advance") applied to alignment instead of consistency.

## Consuming facet A

`as_sample_dataset(data)` accepts: an already-built `SampleDataset`; an iterable
of `Sample`; or facet A's `Dataset`, whose rows are duck-typed (a row is taken if
it exposes `features` plus either `reward` or a director `signal`). Facet A never
imports this package; this package never imports facet A's concrete class. Either
facet can land first.
```
