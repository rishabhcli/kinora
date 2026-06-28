# Critic — Multimodal QA + Learned Reward (DESIGN)

Domain owner files:
- `backend/app/agents/critic.py` — the §9.5 Critic agent (4 checks + `decide_qa` routing).
- `backend/app/render/continuity_qa.py` — deterministic seam QA between two shots.

New files added by this work (all owned by this domain, all under
`backend/app/render/` + `backend/tests/`). ~2300 lines of source + ~1460 lines of
tests; 112 new tests, all green:
- `backend/app/render/reward.py` — the learned-reward CORE: logistic reward,
  threshold calibration, anomaly detection, pairwise A/B (Bradley-Terry),
  best-of-N selection, isotonic probability calibration (PURE, network-free).
- `backend/app/render/qa/__init__.py` — the QA package re-exporting the subsystem.
- `backend/app/render/qa/dataset.py` — `RewardSignalSource` seam + `build_reward_dataset`.
- `backend/app/render/qa/calibration.py` — offline calibration pass → `CriticCalibration`.
- `backend/app/render/qa/identity.py` — per-character identity verification at scale.
- `backend/app/render/qa/temporal.py` — robust temporal-coherence (flicker/morph/limb).
- `backend/app/render/qa/aesthetic.py` — perceptual / aesthetic quality scoring.
- `backend/app/render/qa/active.py` — active-learning queue for anomalies/low-margin.
- `backend/app/render/qa/metrics.py` — the §13 eval harness (CCS / accepted-footage
  efficiency / regen rate / style-drift variance; crew-vs-baseline; multi-run spread).
- `backend/app/render/qa/report.py` — learned-model audit (confusion / Brier / ROC-AUC
  / threshold sweep) — how an operator decides to trust + threshold the learned layer.
- `backend/app/render/qa/drift.py` — fleet-level QA distribution-drift monitoring (PSI
  + windowed mean shift) for silent regressions a fixed threshold never catches.
- `backend/tests/test_render_reward.py` + `test_render_qa_*.py` +
  `test_agents_critic_reward.py` — unit tests for each subsystem.

> KINORA_LIVE_VIDEO stays OFF. Every line of this subsystem is pure Python over
> already-measured numbers (the four QA sub-scores + a director label) or over
> already-extracted frames. No model call, no network, no credits. The Critic's
> VL/embedding calls are unchanged.

---

## 1. Problem & framing

Today (§9.5) the Critic scores four hard-threshold checks and `decide_qa` routes
the repair deterministically from those four numbers:

| Check | Metric | Pre-registered pass |
|---|---|---|
| Identity | CCS = cos(crop, locked ref) | ≥ **0.85** |
| Style | cosine distance of clip style vs scene centroid | ≤ **0.08** |
| Timeline | VL boolean (no contradiction) | **true** |
| Motion | VL artifact rating | ≤ **0.25** |

Those four numbers are honest and pre-registered (§13 demands pre-registration so
the chart can't be tuned to flatter the result). But several things are missing and
are exactly what a "self-improving memory system" (§9.5 closing line) needs:

1. **The thresholds are guesses.** `0.85 / 0.08 / 0.25` are reasonable priors, but
   the *right* boundary for "the director will accept this" is learnable from the
   accept/reject signal the system already accumulates in episodic memory + prefs.
2. **The verdict ignores accumulated outcomes.** Every accepted/degraded shot and
   every director edit is a labeled example of "good / not good enough", yet the
   Critic re-derives its call from scratch every time.
3. **Novel failure modes are invisible.** A clip can pass all four checks and still
   be weird (a failure mode we never wrote a threshold for). Anomaly detection over
   the QA-vector distribution flags out-of-distribution clips for a human look.
4. **Identity does not scale to many characters.** One averaged CCS hides one wrong
   face in a 12-character crowd. We need a per-character CCS vector + an aggregate
   gate that fails on the *weakest* present character.
5. **Temporal coherence is a single VL number.** Flicker / morph / extra-limb are
   distinct, perceptually-grounded signals computable from the frame sequence.
6. **No perceptual/aesthetic axis.** Sharpness, exposure, contrast, color sanity —
   cheap deterministic proxies that catch "technically consistent but ugly".

This subsystem adds a **learned reward layer on top of** — never replacing — the
pre-registered checks. The hard pre-registered gate is preserved verbatim; the
learned layer only ever makes the Critic *more cautious* (it can advise a borderline
clip down, never silently pass a clip the pre-registered gate would fail). That is
the only way to keep §13's pre-registration honest.

---

## 2. Architecture

```
                    accumulated signals (read via EXISTING seams)
   episodic.qa records ─┐   prefs (director edits) ─┐
   (accept / degrade)   │   (reject-with-correction)│
                        ▼                            ▼
              ┌──────────────────────────────────────────┐
              │  RewardDataset  (pure value object)        │
              │   QASample(ccs, style_drift, timeline_ok,  │
              │            motion_artifact, ..., accepted) │
              └──────────────────────────────────────────┘
                        │ fit (deterministic GD / closed-form)
                        ▼
   ┌───────────────┐  ┌────────────────────┐  ┌──────────────────┐
   │ LearnedReward │  │ ThresholdCalibrator│  │ AnomalyDetector  │
   │ logistic over │  │ per-axis boundary  │  │ robust z over    │
   │ N sub-scores  │  │ honoring floors    │  │ QA-vector dist.  │
   │ → reward 0..1 │  │ → CalibratedQA     │  │ → novelty flag   │
   └───────────────┘  └────────────────────┘  └──────────────────┘
                        │                  │
                        ▼                  ▼
              ┌──────────────────────────────────────────┐
              │  decide_qa(...)  — STILL PURE              │
              │   hard pre-registered gate (unchanged)     │
              │   + optional RewardAdvice (advisory only)  │
              └──────────────────────────────────────────┘

   per-clip multimodal QA (frames in, numbers out, all pure):
     identity.py   → per-character CCS vector + aggregate gate
     temporal.py   → flicker / morph / extra-limb sub-scores
     aesthetic.py  → sharpness / exposure / contrast / palette
                         │
                         ▼   PairwisePreference (Bradley-Terry weights)
              A/B: rank two candidate clips for the same shot
```

### Pure-function discipline (the §9.5 contract)

`decide_qa` stays a **pure function of numbers**. The learned layer is injected as
an *optional, already-computed* `RewardAdvice` value object — `decide_qa` does not
call a model, hit a DB, or read global state. The reward model is fit *offline*
(in a calibration pass that reads episodic via the existing `EpisodicService` /
`PrefsService` seams) and the resulting `CalibratedThresholds` + `RewardWeights` +
anomaly model are passed in. This keeps every routing branch unit-testable by
injecting the numbers, exactly as the existing `test_agents_critic.py` does.

The per-clip multimodal modules (`identity` / `temporal` / `aesthetic`) take
already-decoded frames / embeddings and return plain numbers; they do not import
ffmpeg or a network client. The Critic decodes frames once (it already does) and
hands them down.

---

## 3. Contracts (new — in `app/render/reward.py` + `app/render/qa/*`)

```python
@dataclass(frozen=True, slots=True)
class QASample:                  # one labeled training row
    ccs: float
    style_drift: float
    timeline_ok: bool
    motion_artifact: float
    aesthetic: float = 1.0       # optional extra axes (default-neutral)
    temporal: float = 1.0
    accepted: bool = True        # director ground-truth label

@dataclass(frozen=True, slots=True)
class RewardWeights:             # logistic weights over the normalized features
    bias: float; w_ccs: float; w_style: float; w_timeline: float
    w_motion: float; w_aesthetic: float; w_temporal: float

@dataclass(frozen=True, slots=True)
class CalibratedThresholds:      # mirrors QAThresholds, learned + floored
    ccs_min: float; style_drift_max: float; motion_artifact_max: float
    n_samples: int               # provenance
    pinned: bool                 # True ⇒ too little data ⇒ pre-registered floor

@dataclass(frozen=True, slots=True)
class AnomalyModel:              # robust per-axis median + MAD
    median: tuple[float, ...]; mad: tuple[float, ...]; n: int

@dataclass(frozen=True, slots=True)
class RewardAdvice:              # what the learned layer tells decide_qa
    reward: float                # P(director accepts) in [0,1]
    anomaly: bool; anomaly_score: float
    margin: float                # distance of weakest axis from its threshold
    flagged_for_review: bool
```

### Functions (pure)

- `fit_reward(samples, *, l2, iters, lr) -> RewardWeights` — ridge-regularized
  logistic regression via deterministic gradient descent (fixed init, fixed iters).
- `reward_of(weights, sample-like) -> float`.
- `calibrate_thresholds(samples, *, floor, min_samples) -> CalibratedThresholds`
  — per-axis Youden-J boundary on the accept/reject ROC, clamped so the learned
  bound is **never looser** than the pre-registered floor (honest pre-registration).
- `fit_anomaly(samples) -> AnomalyModel`; `score_anomaly(model, sample) -> float`.
- `advise(weights, anomaly_model, thresholds, *, ccs, ...) -> RewardAdvice`.
- `fit_pairwise(pairs) -> RewardWeights` — Bradley-Terry logistic over feature
  *differences* for A/B "which clip is better" learning.
- `rank_pair(weights, a, b) -> int` — -1 / 0 / +1 (a better / tie / b better).

### Per-clip multimodal QA (in `app/render/qa/`)

- `identity.verify_identities(crops_by_char, refs_by_char, embedder) -> IdentityReport`
  — per-character CCS, aggregate = min across present characters (weakest face gates).
- `temporal.temporal_coherence(frames) -> TemporalReport` — flicker (frame-to-frame
  luminance jump), morph (structural drift), extra-limb proxy (edge-density spike);
  all from decoded frames, pure numpy-free pixel math over `bytes`/PNG via a tiny
  decoder seam.
- `aesthetic.aesthetic_score(frames) -> AestheticReport` — sharpness, exposure,
  contrast, palette sanity → one 0..1 perceptual score.

### Integration into `decide_qa` (additive, backward-compatible)

`decide_qa` gains one new keyword-only optional parameter `advice`. Rules
(pre-registration-preserving):
- The hard pre-registered gate is evaluated first and **unchanged**.
- If the gate **fails**, routing is unchanged (advice never rescues a failing clip).
- If the gate **passes** but `advice.flagged_for_review`, the verdict stays `PASS`
  and `RepairAction.ACCEPT`, but the `QARecord` carries `flagged_for_review=True` +
  `learned_reward` + `anomaly_score` for the director feed. Learned signals
  *inform*; the pre-registered gate *decides*.
- `advice is None` ⇒ byte-identical to today.

---

## 4. Reading accumulated signals (the seams — no cross-domain internals)

The reward dataset is assembled by a thin reader using ONLY public service methods:
- shot rows carry `qa` + `status`; `status == accepted` → `accepted=True`,
  `status == degraded` → `accepted=False` (retry cap fell through ⇒ implicit reject).
- prefs priors: a shot that triggered a director edit on the same beat is an implicit
  reject-with-correction (Phase 2).

A `RewardSignalSource` Protocol is the seam, satisfied by `EpisodicService` without
changing it. `build_reward_dataset` maps episodic `qa` dicts → `QASample`.

---

## 5. Phased roadmap (living — done vs. planned; legend: done / planned)

**Phase 1 — learned reward core.** ✅
- `reward.py`: `QASample`, `RewardWeights`, `fit_reward`, `reward_of`,
  `calibrate_thresholds`, `CalibratedThresholds`, anomaly model + scoring,
  pairwise preference fit + `rank_pair`, `advise`.
- `decide_qa` extended with optional advisory `advice`.
- `QARecord` extended additively with `learned_reward`, `flagged_for_review`,
  `anomaly_score`.

**Phase 2 — dataset seam + calibration pass.** ✅
- `qa/dataset.py`: `RewardSignalSource` Protocol + `build_reward_dataset`.
- `qa/calibration.py`: offline `CalibrationPass` that fits weights/thresholds/anomaly
  from a signal source and returns a `CriticCalibration` bundle the Critic consumes.

**Phase 3 — multimodal per-clip QA.** ✅
- `qa/identity.py`: per-character CCS at scale (weakest-face gate).
- `qa/temporal.py`: flicker / morph / extra-limb from frame sequence.
- `qa/aesthetic.py`: perceptual quality proxies.
- Critic wires them: identity vector + temporal + aesthetic feed the QARecord and
  the reward features.

**Phase 4 — active learning + provenance.** ✅
- `qa/active.py`: a deterministic queue that surfaces anomalies + low-margin passes
  for human labeling; the labels feed the next calibration round.

**Phase 5 — eval harness, audit, drift, A/B, isotonic.** ✅
- `qa/metrics.py`: the §13 eval harness — per-character CCS, accepted-footage
  efficiency, regen rate, style-drift variance, crew-vs-baseline `compare_arms`, and
  multi-run mean±spread (`aggregate_runs`) for the honest §13 chart.
- `qa/report.py`: the learned-model audit — confusion matrix + precision/recall/F1,
  Brier score, ROC-AUC (Mann–Whitney), and a threshold sweep so the review-floor is
  chosen with eyes open, not a magic number.
- `qa/drift.py`: fleet-level QA distribution-drift monitoring — PSI + windowed mean
  shift, per axis, with a degrading/improving direction; catches silent regressions
  where every clip still passes but the *population* worsens.
- `reward.py`: `select_best` (A/B-of-N "keep the preferred clip" decision) and
  `fit_isotonic` / `IsotonicCalibrator` (monotone reliability-curve calibration of the
  raw reward to honest probabilities, ranking-preserving).

**Phase 6 — live wiring (planned, needs sibling-domain seams).** ⏳
- Periodic `CalibrationPass` on the idle-sweeper cadence; per-book `qa_calibration`
  row (additive migration); surface `learned_reward` / `flagged_for_review` on the
  live agent-activity feed; expose the §13 `compare_arms` slide on the metrics panel.
- Pairwise A/B as a live render-two-seeds loop using `select_best` (gated behind
  KINORA_LIVE_VIDEO so it spends zero credits until intentionally enabled).
- Fold director-edit signals (prefs `record_changes`) in as reject-with-correction
  labels via a `PrefsSignalSource` adapter over the existing prefs seam.
- Apply `IsotonicCalibrator` to the reward inside `advise` once labels are plentiful,
  keeping the raw logistic as the cold-start fallback.

---

## 6. Cross-domain contract changes (RECORDED — additive only)

All changes below are **additive** (new optional fields / new params with safe
defaults) so sibling domains are unaffected.

1. **`backend/app/agents/contracts.py` — `QARecord`** (shared file): added optional
   fields, all defaulted, `extra="forbid"` preserved:
   - `learned_reward: float | None = None`
   - `flagged_for_review: bool = False`
   - `anomaly_score: float | None = None`
   - `per_character_ccs: dict[str, float] | None = None`
   - `temporal: float | None = None`
   - `aesthetic: float | None = None`
   Existing producers (the Critic) and consumers (pipeline, episodic log) keep
   working unchanged because the fields default. **No field renamed or removed.**

2. **`backend/app/agents/critic.py` — `decide_qa`**: added keyword-only
   `advice: RewardAdvice | None = None`. Default `None` ⇒ identical behaviour.

3. **`backend/app/agents/critic.py` — `Critic.score`**: added keyword-only
   `calibration: CriticCalibration | None = None` and richer optional inputs
   (per-character crops/refs). All optional with safe defaults, so the
   `ClipCritic` Protocol in `pipeline.py` (a sibling domain's seam) is still
   satisfied — callers pass exactly the existing kwargs.

No sibling-domain file is edited. The only shared file touched is
`contracts.py`, additively, as documented here.
