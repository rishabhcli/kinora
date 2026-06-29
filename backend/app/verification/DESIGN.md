# Formal verification — facet A: property + metamorphic + stateful testing

> Owner package: `backend/app/verification/` (new, additive). Tests live in
> `backend/tests/verification/`. Nothing here is imported by the live
> render/scheduler path — it is a pure *test* surface over the deterministic
> policy core. Zero credits, `KINORA_LIVE_VIDEO` irrelevant (no provider calls,
> no DB/Redis/object-store, no ffmpeg).

## 1. What this is

A deep correctness suite for Kinora's **pure decision functions** — the ones the
six-agent pipeline, the scheduler, and the render engine all depend on but which
are small enough to verify *exhaustively in distribution* with property-based and
metamorphic testing rather than a handful of examples. The system under test is
treated as a black box: we verify it, we do **not** modify its logic. Bugs found
are catalogued in §6 and reported (spawned as separate tasks), never fixed here.

Three testing modes, layered:

1. **Property tests** — invariants that must hold for *all* inputs (totality,
   determinism, bounds, monotonicity, threshold seams).
2. **Metamorphic tests** — relations between an input and a transformed input
   (velocity scaling, position translation, QA improvement, beat reordering,
   scene-shot reordering) where no fixed oracle exists but the *relationship* is
   known.
3. **Stateful / model-based tests** — `hypothesis.stateful.RuleBasedStateMachine`
   drives random command sequences against the real component *and* an independent
   reference model, asserting they agree at every step (the §9.7 render state
   machine; the poison tracker).

## 2. Package layout

```
app/verification/
  __init__.py
  DESIGN.md                      ← this file
  properties/
    __init__.py
    strategies.py                ← shrinking-friendly Hypothesis generators
    state_model.py               ← independent reference model of the §9.7 FSM
    relations.py                 ← metamorphic-relation input transforms
tests/verification/
  conftest.py                    ← Hypothesis profiles (dev / ci / deep)
  test_prop_render_mode.py       ← §9.3 Wan-mode decision tree (exhaustive 64-input)
  test_prop_qa_routing.py        ← §9.5 Critic routing + thresholds + advisory-neutrality
  test_prop_arbitration.py       ← §7.2 conflict arbitration (full truth table)
  test_prop_scheduler_zones.py   ← §4.3/§4.4/§4.6 ETA, zones, clamp, stability
  test_prop_reading_model.py     ← §4.3 EWMA model + §4.5 committed-ahead buffer math
  test_prop_scheduler_advanced.py← §4.5 adaptive watermarks + §12.2 fair-share
  test_prop_admission.py         ← §12.2 admission control (backpressure + fairness)
  test_prop_optimizer.py         ← §4.6/§11.1 budget knapsack (DP optimality vs brute force)
  test_prop_retry_escalation.py  ← §9.5 retry/escalation + backoff + failure classification
  test_prop_segment_packer.py    ← §4.2 segment packing (page-bounded, ≤15s ceiling)
  test_prop_sync_map.py          ← §9.4 sync-map math (page-turn, phonemes, rescale, align)
  test_prop_interval_algebra.py  ← §8.5 Allen interval algebra (13 relations)
  test_prop_constraint_network.py← §8.5 composition algebra + AllenNetwork path-consistency
  test_metamorphic_cinematography.py ← §9.3/§10 cinematic-language floor + style-override
  test_stateful_render_machine.py← §9.7 FSM model-based + simulator-driven control flow
  test_prop_render_simulator.py  ← §9.7 zero-IO control-flow simulator end-to-end invariants
  test_metamorphic_timeline.py   ← §4.2 narrative-vs-story timeline reconstruction
  test_metamorphic_scene.py      ← §9.6 scene DAG reordering invariance
  test_stateful_poison.py        ← §4.11/§12.1 poison tracker model-based
```

## 3. Coverage map (what is verified, against what spec)

| Target | Module | Spec | Highlight properties |
|---|---|---|---|
| `decide_render_mode` | cinematographer | §9.3 | exhaustive over all 64 boolean inputs; locked-char+motion never t2v; branch precedence |
| `decide_qa` | critic | §9.5 | gate = conjunction at all 4 thresholds; routing branch order; pass/fail monotone under axis improvement/degradation; advisory is byte-neutral; calibration never loosens floor |
| `decide_arbitration` | showrunner | §7.2 | evolve ⇒ offered ∧ supported; surface ⇒ present-director ∧ user-facing; full 8-case truth table |
| `eta_seconds` / `classify` / `clamp_velocity` / `viewer_zone` / `trajectory_is_stable` | scheduler.zones | §4.3/§4.4/§4.6 | ETA sign/translation-invariance/inverse-velocity scaling; zone partition + monotonicity; faster-reader-pulls-toward-committed; clamp idempotence |
| `ReadingModel` / `recompute_committed_ahead` | scheduler.prediction / model | §4.3/§4.5 | clamped finite velocity; non-negative variance/dwell; monotone forecast; **buffer drains monotonically as reader advances (sawtooth)** |
| `adapt_watermarks` / `FairShareAllocator` | scheduler.adaptive / fairness | §4.5/§12.2 | adaptation only deepens; `L<C<H`; min-band; **fair-share total ≤ pool**, feasibility, non-needy get nothing |
| `decide_admission` | queue.admission | §12.2 | committed/keyframe never shed; only speculative droppable; backpressure precedes session-cap |
| `optimize_promotions` / `shot_value` | scheduler.optimizer | §4.6/§11.1 | **spend invariant (never over budget)**; subset; reading-ordered; **DP value-optimal vs brute force** |
| `decide_retry` / backoff / `classify_failure` | render.retry | §9.5 | cap ⇒ terminal; backoff bounded+monotone; permanent degrades immediately |
| `pack_segments` | render.segment_packer | §4.2 | beat conservation; no page-cross; ≤15s ceiling; ordinals 0..k-1 |
| `page_turn_at` / `split_phonemes` / `rescale_word_timings` / `align_words` / `grapheme_chunks` | render.sync_map | §9.4 | turn inside shot; phonemes tile span no-gap; rescale monotone+bounded; alignment in-range; chunk reconstruction |
| `BeatInterval` Allen algebra | render.continuity_reasoning.intervals | §8.5 | inverse involution; relate totality+converse-symmetry; **overlap matches ground-truth beat-set intersection** |
| `compose` / `AllenNetwork` | render.continuity_reasoning.composition / constraints | §8.5 | EQUALS identity; converse law; composition realised by a witness triple; **non-empty network always path-consistent** (no false contradiction); contradiction cycle detected |
| cinematic-language floor + override | cinematographer / cinematic_language | §9.3/§10 | genre floor ⊇ base floor; `_merge_negative` never drops the floor + de-dups + floor-first; style-override last-wins; axis-only ⇒ no override |
| `ShotStateMachine` | render.states | §9.7 | **stateful model-agreement** with independent reference; production table == documented diagram; liveness (sink reachable everywhere); illegal edges raise + don't mutate |
| `RenderSimulator` / `plan_ladder` | render.simulator / ladder | §9.7/§12.4 | always terminates at a sink; attempts ≤ cap+1; legal state walk; **non-live path spends 0 video-seconds**; ladder chain descends in rank+cost |
| `reconstruct_timeline` | comprehension.timeline | §4.2 | story_order is a permutation; narrative_order preserved; **present line never reordered among itself** |
| scene DAG | render.simulator | §9.6 | **reordering independent shots preserves total spend + ladder distribution** |
| `PoisonTracker` | render.poison | §4.11/§12.1 | **stateful model-agreement**; quarantine sticky until success; permanent failures double-weight; quarantine forces audio card |

## 4. Generator design (shrinking-friendly)

`strategies.py` builds every composite input from small, structured primitives so
the shrinker collapses each field independently to a minimal counterexample.
Two deliberate choices make the suite *find* bugs:

- **Near-threshold emphasis.** Gates flip at exact boundaries (CCS 0.85, style
  0.08, ETA = commit horizon). The `near()`/`around()` strategies mix in
  values sampled tightly around each threshold, exercising the `<` vs `<=` seam
  the bulk-interior uniform float never lands on.
- **Grid-aligned knapsack weights.** The optimizer ceil-quantises durations to a
  0.5s grid; the candidate strategy draws weights *on that grid* so the
  brute-force optimality oracle compares against the identical quantised model.

## 5. Reference models (never check the system against itself)

- **§9.7 FSM** — `state_model.REFERENCE_EDGES` is the legal-edge relation
  transcribed *by hand from the kinora.md diagram*, independent of
  `app.render.states.ALLOWED_TRANSITIONS`. `test_production_table_matches_reference_diagram`
  asserts they are identical, so a silent edit to either is caught.
- **Poison tracker** — the stateful test maintains an independent weighted-failure
  counter and asserts the tracker agrees on failures, quarantine, and forced rung.
- **Knapsack** — `_brute_force_best_value` enumerates all subsets (≤6 candidates)
  as the optimality oracle for the DP.

## 6. Bugs & findings (reported, NOT fixed here)

### BUG-1 — `ReadingModel.observe` divides by zero on a subnormal `dt_ms`
`backend/app/scheduler/prediction.py` ~L163-174. The guard is `if dt_ms <= 0.0:
return`, but the rate is `abs(words) / (dt_ms / 1000.0)`. A positive-but-subnormal
`dt_ms` (the smallest positive double, `5e-324`; any value below ~`1e-320`) passes
the `> 0` guard yet **underflows to exactly `0.0`** after the `/ 1000.0` unit
conversion, raising `ZeroDivisionError` and crashing the estimator. Reachable from
the live `IntentController` when two settled intents arrive with a vanishingly
small positive clock delta.
- Repro: `ReadingModel().observe(words_advanced=1, dt_ms=5e-324)`.
- Pinned by `test_prop_reading_model.py::test_subnormal_dt_ms_crashes_observe_BUG1`
  (`xfail(strict)` — flips to a failure the moment the divide is guarded).
- Suggested fix: early-return when `dt_ms / 1000.0 <= 0.0` (subsumes the existing
  guard), or floor `dt_ms` at a small epsilon (the §4.7 settle cadence is ~200ms;
  a sub-microsecond gap carries no rate information). Spawned as a separate task.

### MINOR-1 — `RetryPolicy.backoff_for` can round a hair above `max_backoff_s`
`backend/app/render/retry.py` ~L96-97. `backoff_for` clamps to the ceiling *then*
`round(·, 3)`, so a ceiling with >3 decimals (e.g. `5.9375`) can round the clamped
value up to `5.938` — a sub-millisecond overshoot above the stated bound. Harmless
in production (defaults are integers). The bound property asserts with a `1e-3`
rounding tolerance and documents the edge. *Same rounding family appears in
`page_turn_at` (sub-millisecond shots) and `rescale_word_timings`
(`round(·, 3)` can land ~5e-4 above the target) — both benign, both noted in the
respective tests with the rounding-granularity tolerance.*

### BUG-2 — empty (zero-width) interval triggers a FALSE continuity contradiction
`backend/app/render/continuity_reasoning/` (composition.py + constraints.py). The
§8.5 Allen composition table (`_build_table`) is computed over **non-empty**
intervals only (`combinations(points, 2)` requires `start < end`), so it
mis-composes any chain routed through a zero-width interval. Because
`BeatInterval` permits `start == end` (only `end < start` is rejected), a fact
with a zero-width lifetime `[n, n)` is constructible — and a network containing one
can have `path_consistency()` **falsely collapse an edge and report
`consistent=False`**, which in production would wrongly flag a legitimate shot as a
timeline violation.
- Repro (false contradiction):
  ```python
  ivs = {"A": BeatInterval(0,1), "B": BeatInterval(1,1), "C": BeatInterval(1,2)}
  AllenNetwork.from_intervals(ivs).path_consistency()  # consistent=False  (WRONG)
  ```
- Composition-level repro: `compose_singletons(MEETS, MEETS) == {BEFORE}`, but for
  `A=[0,1)`, `B=[1,1)`, `C=[1,2)` the true `A relate C` is `MEETS` ∉ `{BEFORE}`.
- Pinned by `test_prop_constraint_network.py::test_empty_interval_network_can_false_contradict_BUG2`
  and `::test_composition_table_is_unsound_for_empty_intervals_MINOR3` (both
  `pytest.fail` when the gap closes, prompting an un-pin). The soundness property is
  scoped to non-empty intervals, where it holds.
- Suggested fix: either reject zero-width lifetimes at construction (if a fact is
  never legitimately zero-width) or extend `_build_table` to include zero-width
  intervals so composition is sound over the full domain. Spawned as a separate task.

### MINOR-3 — Allen composition table unsound over empty intervals
The composition-algebra root cause of BUG-2, recorded separately because it is the
*table*-level defect (independent of the network): `compose_singletons` is unsound
whenever an empty interval mediates the composition. See BUG-2 for the repro and pins.

### MINOR-2 — `FairShareAllocator` work-conservation gap under lopsided weights
`backend/app/scheduler/fairness.py` `allocate`. The water-filling loop drops a
needy session whose proportional `share` rounds below `_EPSILON` (because its
weight is negligible relative to the others), even when the pool still has
remainder and that session still has ceiling room. Result: with two needy sessions
each `deficit=1.0`, `pool=2.0`, and weights `1.0` / `1e-200`, the allocator leaves
~0.8s of the pool **idle** though a reader still needs it — contradicting the
"Work-conserving" docstring claim. The documented hard invariants (total ≤ pool,
no session over its ceiling, determinism) still hold; only full work-conservation
is overstated for pathologically lopsided weights.
- Pinned by `test_prop_scheduler_advanced.py::test_lopsided_weight_strands_pool_MINOR2`
  (asserts current behaviour + `pytest.fail`s if the gap is fixed, prompting an
  un-pin). The work-conservation property is scoped to comparable weights, where
  the gap cannot arise. Spawned as a separate task.

> All three are reported via `spawn_task` chips for the owning domain; none are
> fixed in this worktree (cross-domain logic is out of scope for facet A).

## 7. Additive changes to shared files

This facet is additive-only. Two shared-file changes, both in
`backend/pyproject.toml`:

- **dev dependency** — added `hypothesis>=6.100` to `[project.optional-dependencies].dev`.
  Pure-Python, test-only; nothing in production imports it.
- **lint scope** — added one `[tool.ruff.lint.per-file-ignores]` entry:
  `"tests/verification/*.py" = ["N802", "N806"]`. Bug-regression test names carry a
  `_BUGn`/`_MINORn` suffix and the §7.2 truth-table uses SCREAMING_CASE option
  aliases (`HONOR`, `SURFACE`, `EVOLVE`) for readability — both intentional, so
  pep8-naming is relaxed *only* in this test tree. No production-code lint rules
  change.

No production source file is modified.

## 8. Running the suite

```bash
# from backend/ with the worktree venv:
.venv/bin/python -m pytest tests/verification/ -q                 # dev profile (100 ex)
HYPOTHESIS_PROFILE=ci   .venv/bin/python -m pytest tests/verification/ -q   # 500 ex
HYPOTHESIS_PROFILE=deep .venv/bin/python -m pytest tests/verification/ -q   # 2000 ex soak
.venv/bin/ruff check app/verification tests/verification
.venv/bin/mypy app/verification tests/verification
```

`hypothesis` (6.x) is the one added dev dependency; install it into the venv
(`uv pip install hypothesis` / `pip install hypothesis`). It is a pure-Python,
test-only dependency.

## 9. Status

- **223 passed, 1 xfailed** (the BUG-1 pin) on the `dev` profile; clean at the
  `ci` profile (500 examples) on the core suites with no flakiness.
- `ruff check` + `mypy` clean across `app/verification` and `tests/verification`.
- Findings reported (all spawned as separate fix tasks; none fixed here):
  - **BUG-1** — `ReadingModel.observe` ZeroDivisionError on subnormal `dt_ms`.
  - **BUG-2 / MINOR-3** — empty (zero-width) `BeatInterval` ⇒ unsound Allen
    composition ⇒ *false* continuity contradiction (would block a legitimate shot).
  - **MINOR-1** — `backoff_for` / `page_turn_at` / `rescale_word_timings` can round
    a sub-millisecond hair past their stated bound (benign 3-dp rounding).
  - **MINOR-2** — `FairShareAllocator` strands part of the pool under pathologically
    lopsided session weights (work-conservation gap).
# Formal verification — facet B: an explicit-state model checker

This package is a from-scratch, pure-Python **explicit-state model checker**
(TLA+/Spin in the small) plus machine-checked models of Kinora's three
concurrency-critical control protocols and a fourth lane-fairness protocol. It
exhaustively explores the *interleavings* the unit suite can only sample, so the
real invariants — "the buffer never goes negative", "the budget is never
double-spent", "no cancellation is lost", "evolve-canon never fires without
textual support" — are proven over the whole reachable state space rather than
over a handful of hand-picked schedules.

Nothing here calls a provider, touches Redis, or spends a video-second. It is
pure Python over abstract state and runs in the no-infra unit suite in under a
second. `KINORA_LIVE_VIDEO` stays off; this code never goes near it.

Run it: `make verify-models` (or `python -m app.verification.run`).

---

## 1. Architecture

```
app/verification/
├── modelcheck/              the checker engine (domain-agnostic)
│   ├── spec.py              the DSL: State, Action (guard+effect+fairness), Spec, properties
│   ├── engine.py            BFS/DFS reachability + safety + deadlock + halt-on-violation
│   ├── liveness.py          SCC / weak-fairness liveness (eventually, leads_to) → lassos
│   ├── symmetry.py          permutation-orbit canonicalisation (symmetry reduction)
│   ├── trace.py             counterexample formatting (action traces + lassos)
│   └── report.py            CheckReport / PropertyResult (assertable, printable)
├── specs/                   the protocol models (domain-specific)
│   ├── scheduler_buffer.py  §4.5–§4.9 dual-watermark promotion
│   ├── render_queue.py      §12.1 claim/lease/ack lifecycle
│   ├── arbitration.py       §7.2 conflict resolution (drives the REAL policy fn)
│   └── fairness.py          §12.2 lane admission + per-session fairness
└── run.py                   the consolidated runner (CI gate + DESIGN.md source)
```

### The DSL (`spec.py`)

A model is a `Spec`: a tuple of **initial states** (any immutable hashable
value), a tuple of **actions**, and the **properties** to check. An `Action` is
a `(name, guard, effect, fairness)` quadruple — the moral equivalent of one
TLA+ `Next` disjunct. The `guard` decides whether the action is enabled in a
state; the `effect` yields the successor state(s) (yielding several models
non-determinism). Both must be pure functions of the state.

Each action carries a **fairness** annotation: `WEAK` for autonomous progress (a
worker draining the queue, a timer firing) — liveness may assume it is
eventually taken when continuously enabled — or `NONE` for optional/adversarial
steps (a crash, a far seek) the environment need never do.

### The engine (`engine.py`)

`ModelChecker.check(spec)` enumerates the reachable states (BFS by default, so
safety counterexamples are the *shortest*; DFS available for deep graphs),
hashing each by its canonical fingerprint, and checks:

* **safety invariants** — a violating state yields the shortest action trace to
  it;
* **deadlock** — a non-terminal sink (opt-in, parametrised by an
  "is-this-an-intended-terminal?" predicate);
* **liveness** — delegated to `liveness.py`.

It bounds itself with `max_states` (a mis-specified infinite model fails loudly
as *truncated*, never hangs) and supports **halt-on-violation**: the first pass
stops BFS the moment it *discovers* an invariant breach. Because BFS discovers
states in non-decreasing depth, the offending state is still found at minimal
distance (shortest trace), and a buggy spec with an *unbounded faulty
trajectory* is caught in milliseconds instead of running to the cap. (This is
what makes the double-spend mutation, whose bug makes the state space infinite,
finish instantly.)

### Liveness under weak fairness (`liveness.py`)

Liveness violations are infinite runs; on a finite graph they are always
**lassos** (a finite stem into a repeating cycle). For `eventually(P)`:

1. restrict to the sub-graph of ¬P states (a run that reaches P is fine);
2. find its strongly-connected components (iterative Tarjan — no recursion-depth
   limit on big spaces);
3. **the weak-fairness test**: a cycle is a real counterexample only if the
   system can be trapped on it *without violating fairness* — i.e. for every
   weakly-fair action enabled somewhere on the cycle, the cycle must actually
   *take* it. A cycle that ignores a continuously-enabled fair action is not a
   fair run, so it is not a valid counterexample;
4. if a fair, ¬P, reachable cycle exists, build the lasso (BFS stem + the cycle).

`leads_to(P, Q)` reduces to "a fair ¬Q cycle reachable through a P-state". Step 3
is the part toy checkers skip; getting it right is what lets us actually *prove*
"every committed shot is eventually accepted-or-degraded" rather than merely
"there exists a run where it is".

### Symmetry reduction (`symmetry.py`)

When components are interchangeable (two render workers, three reading
sessions), a state and any permutation of its symmetric parts have the same
future up to renaming. A `SymmetryReduction` canonicalises every state to one
orbit representative before hashing, collapsing the factorial blow-up. The
always-sound canonicaliser — `sort_multiset`, sorting the bag of interchangeable
slots — is the only form the specs use, so no reachable behaviour is hidden.

---

## 2. Checked specs + results

`make verify-models` output (all four specs, **34 properties, all hold**):

```
scheduler_watermark_buffer: 11/11 properties hold · 252 states / 765 transitions · symmetry=none
render_queue_lifecycle:      9/9  properties hold · 85 states / 210 transitions · symmetry=none
conflict_arbitration_policy: 8/8  properties hold · 57 states / 41 transitions  · symmetry=none
render_fairness_lanes:       6/6  properties hold · 53 states / 324 transitions · symmetry=session_orbit
ALL SPECS HOLD ✓
```

### 2.1 Scheduler watermark-promotion (`scheduler_buffer.py`, §4.5–§4.9)

A finite abstraction of one session's dual-watermark buffer: integer slots
(`L=1, H=3` ≈ 25s/75s with 25s shots), a per-session in-flight cap, a fixed
budget cap, the burst/idle hysteresis flag, the idle-pause flag, and a
trajectory token for seeks. Actions are the §4.9 control-loop transitions
(`start_burst`, `promote`, `stop_burst`, `land` — all weakly fair) plus the
environment events (`advance`, `idle_pause`, `wake`, `seek`).

| Property | Kind | Meaning |
|---|---|---|
| `buffer_non_negative` | safety | §4.5 — committed-seconds-ahead is never a debt |
| `inflight_non_negative` / `reserved_non_negative` / `spent_non_negative` | safety | counts never go negative |
| `no_over_commit` | safety | **no double-spend**: `reserved + spent ≤ budget`; a land that frees a reservation without recording the spend, or leaks it, breaks this |
| `inflight_capped` | safety | the committed lane respects the per-session in-flight cap (§4.9) |
| `buffer_bounded` | safety | total-ahead never overshoots the high watermark |
| `idle_implies_not_bursting` | safety | §4.7 — idle-pause halts speculation |
| `settles_to_idle_lane` | liveness | the committed lane always quiesces (the §4.10 burst-then-idle sawtooth — it does not generate forever) |
| `burst_eventually_stops` | leads_to | a burst cannot run forever (hits H, or budget runs out) |
| `reservation_eventually_released` | leads_to | every reservation is released (landed or cancelled) — the budget is never permanently tied up |

### 2.2 Render-queue lifecycle (`render_queue.py`, §12.1)

One job's lifecycle (`QUEUED → RESERVED → SUBMITTED → POLLING →
SUCCEEDED/RETRYING/CANCELLED/DEADLETTER`) across *N* workers, with a lease
(visibility timeout), a reaper, a cancel token, and the retry/back-off ladder.
The lease distinguishes **fresh** (heartbeated — protected) from **lapsed**
(crashed — reapable). A `ghost_active` flag captures the double-submit hazard: a
worker reaped *while still rendering* can ack a second, independent result.

| Property | Kind | Meaning |
|---|---|---|
| `no_double_spend` | safety | a job's budget is debited **at most once** across every interleaving of N workers + reaper + retries (the §12.1 idempotency contract) |
| `single_holder` | safety | at most one worker holds the job (atomic claim + single-holder lease) |
| `terminal_releases_everything` | safety | a terminal job holds no lease, no reservation, no holder |
| `reserved_only_when_leased` | safety | a reservation is held only while actively leased — never on a queued job (no pinned budget) |
| `holder_iff_leased` | safety | holder bookkeeping is consistent with the leased states |
| `attempts_bounded` | safety | retries are bounded; the job dead-letters past the cap (§12.4 degrade) rather than looping |
| `success_excludes_cancel` | safety | the two terminal outcomes are mutually exclusive (no acking a cancelled job) |
| `claimed_job_terminates` | leads_to | every claimed job eventually reaches a terminal state — accepted-or-degraded; the pipeline never blocks on one shot (§4.11/§12.4) |
| `cancel_eventually_honoured` | leads_to | **no lost cancellation**: a requested cancel leads-to the job leaving the active set |

Checked at `workers=2` and `workers=3`, and under a worker-orbit symmetry
reduction (verdict preserved, state space shrunk).

### 2.3 Conflict arbitration (`arbitration.py`, §7.2)

The continuity → arbitration lifecycle (`DRAFTED → CHECKED → APPROVED |
CONFLICT → ARBITRATION → {HONOR | EVOLVE | SURFACE → AWAIT_USER} → APPROVED`).
The `arbitrate` action **drives the real production policy**,
`app.agents.showrunner.decide_arbitration`, over all eight environment worlds
(violation × textual-support × director × user-facing), so the model checks the
*actual* decision logic, not a paraphrase of it.

| Property | Kind | Meaning |
|---|---|---|
| `evolve_requires_textual_support` | safety | §7.2 gate — evolve_canon only ever fires WITH source-text support (never rewrites canon on no evidence) |
| `evolved_iff_evolve_chosen` | safety | the evolved-canon flag tracks the chosen option |
| `surface_requires_director_and_user_facing` | safety | §7.2 gate — surface only with a present director on a user-facing conflict (never dead-ends a prompt with no one to answer) |
| `fallback_is_honor` | safety | the safe default is honor_canon when neither evolve nor surface is eligible |
| `approved_conflict_is_logged` | safety | every approved conflict carries a logged decision (the §7.2 audit trail the demo shows is never skipped) |
| `approved_violation_has_resolution` | safety | no approval that skipped arbitration when a violation existed |
| `conflict_eventually_approved` | leads_to | every raised conflict resolves to an approved shot — never silently dropped or stuck |
| `draft_eventually_approved` | leads_to | the pipeline never strands a shot in the continuity check |

### 2.4 Lane fairness (`fairness.py`, §12.2)

Several readers contend for the shared **4 committed + 2 speculative** render
slots (6 shared workers). Committed enqueues are "always admitted" by preempting
a running speculative job; speculative enqueues are dropped under backpressure;
a per-session committed concurrency cap (2) is the anti-starvation guard.
Sessions are interchangeable → the session-orbit symmetry reduction.

| Property | Kind | Meaning |
|---|---|---|
| `committed_lane_bounded` | safety | the committed lane never exceeds 4 slots |
| `speculative_lane_bounded` | safety | the speculative lane never exceeds 2 slots (backpressure drops overflow) |
| `total_workers_bounded` | safety | the shared pool is never oversubscribed (preemption frees a worker before committed takes it) |
| `per_session_cap_respected` | safety | §12.2 — no session exceeds its committed cap; one reader cannot monopolise the workers and starve others |
| `running_counts_non_negative` | safety | counts never go negative |
| `saturated_committed_lane_drains` | leads_to | a saturated committed lane always drains (the liveness side of anti-starvation — a waiting session is never blocked forever) |

---

## 3. Invariant violations found

**On the production code: none.** Every property of every real spec holds over
the full reachable state space (under the stated abstraction and bounds). In
particular, `decide_arbitration` — the only real production function driven by a
spec — satisfies all six §7.2 invariants across all eight worlds.

Two findings worth recording, both about **model fidelity**, surfaced while
building the checker and were resolved:

1. **Scheduler budget model.** A first cut tracked `budget` as a remaining
   counter and asserted `reserved ≤ budget`. The double-spend *mutation* (a land
   that forgets the debit) did **not** break that invariant — it made budget
   *more* available, so the property was vacuously safe against the very bug it
   was meant to catch. Reworked to the conservation form: `budget` is a fixed
   cap and the live commitment is `reserved + spent ≤ budget`. The mutation now
   breaks it, and the *correct* model still holds. (Lesson: an invariant that a
   plausible bug cannot violate is not testing anything.)

2. **Fairness lane model.** A first cut let a committed admit *preempt to
   overflow* the committed lane; the checker immediately returned a 6-step
   counterexample (`C=5 P=0` after a preempt) showing the committed lane
   exceeding 4. The §12.2 reading is that committed/speculative are distinct
   lanes drawing from one shared pool — committed is hard-bounded at 4 and
   preemption frees a *shared worker*, not a committed slot. Corrected; all six
   properties then hold. (This is the model checker doing its job: a wrong model
   was caught by its own invariant in milliseconds.)

### Mutation testing — the checker has teeth

A checker that never fails is worthless, so `test_verification_spec_mutations.py`
injects, for each protocol, exactly the bug its invariant forbids and asserts
the checker reports a violation **with a counterexample that genuinely exhibits
the bug**:

| Mutation | Property it must trip |
|---|---|
| scheduler land leaks the reservation | `no_over_commit` (double-spend) |
| scheduler advance with no buffer guard | `buffer_non_negative` (underflow) |
| reaper steals a fresh lease → ghost render | `no_double_spend` (double-submit) |
| drop `honour_cancel` + add a churn cycle | `cancel_eventually_honoured` (lost cancel — lasso) |
| arbitrate surfaces with no director | `surface_requires_director_and_user_facing` |
| arbitration spins on CONFLICT forever | `conflict_eventually_approved` (dropped conflict — lasso) |
| fairness admit drops the per-session cap | `per_session_cap_respected` (starvation) |

All eight mutations are caught. The two liveness mutations produce **lassos**
(stem + repeating loop) whose loop states are asserted to actually hold the bad
condition (cancel requested but never terminal; conflict never approved).

---

## 4. Abstraction & soundness notes

* **What the proofs cover.** Each spec is a *finite abstraction* at the level
  where exhaustive exploration is feasible (integer slots instead of real
  seconds; one job's lifecycle across N workers; per-session running-counts
  instead of individual jobs). The properties proven are the protocol's
  *contracts*, not a bit-exact replica of the Python. The arbitration spec is
  the exception — it drives the real `decide_arbitration`, so its proof is about
  the production function directly.
* **Bounds.** Buffer band `L=1,H=3`; budget cap 4–6 slots; 2–3 render workers;
  3–4 reading sessions; retry cap 2; 2 seeks. Chosen large enough to exercise
  the interesting interleavings (preemption, a clip landing concurrently with an
  advance, a seek mid-render, two workers racing a reaper) and small enough to
  finish in well under a second. The classic small-model-checking caveat applies:
  a bug needing more than the bounded number of components could escape; the
  bounds are sized past the structural thresholds (lane sizes, caps) where such
  bugs would first appear.
* **Symmetry soundness.** Only the `sort_multiset` canonicaliser is used (on the
  worker-holder and the per-session-load multisets), which is provably orbit-
  complete — it cannot hide a reachable state.

---

## 5. Remaining roadmap (buildable next, additive)

* **Drive more real code.** The arbitration spec already executes
  `decide_arbitration`; the same pattern could drive the real `RetryPolicy`
  (`app.queue.redis_queue`) and the real zone-classification
  (`app.scheduler.zones`) from inside the specs, turning more proofs into
  statements about production functions rather than abstractions.
* **A composed end-to-end spec.** Cross-protocol invariants — e.g. a seek
  cancelling a committed render *and* releasing its scheduler reservation in one
  atomic story — by composing the scheduler and render-queue specs over a shared
  budget ledger.
* **CTL / nested fairness.** The liveness layer handles `eventually` and
  `leads_to` under weak fairness; strong fairness and nested temporal operators
  (`always eventually`) are natural extensions of the SCC machinery.
* **Partial-order / stubborn-set reduction.** A second state-space reduction
  orthogonal to symmetry, for independent concurrent actions, would push the
  feasible worker/session bounds higher.
* **DOT export + replay.** Emit the reachable graph as Graphviz, and replay a
  counterexample trace against the real Python objects as a generated regression
  test.

## 6. Additive shared-file changes

This package is **entirely new files** — no existing source was modified. The
only shared-file change is **additive**: a `verify-models` target (and its
`.PHONY` + help line) in the root `Makefile`, running
`python -m app.verification.run`. The `app/verification/` tree and the seven
`tests/test_verification_*.py` files are new.
```
make verify-models
```
runs the consolidated checker as a CI gate (exit non-zero on any violation).
