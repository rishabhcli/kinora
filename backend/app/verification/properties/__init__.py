"""Property + metamorphic test building blocks for the deterministic policy core.

This sub-package holds the *reusable* machinery the verification suite is built
from — kept out of ``tests/`` so it can be imported by several test modules
without duplication and type-checked by mypy like any other package:

* :mod:`strategies` — shrinking-friendly Hypothesis strategies for the policy
  inputs: render-mode booleans, QA scorecards (with deliberate near-threshold
  emphasis), arbitration conflicts, scheduler reading positions, beats/segments,
  and §9.7 command sequences.
* :mod:`state_model` — a tiny, independent **reference model** of the §9.7 render
  state machine the stateful tests check the real machine against.
* :mod:`relations` — metamorphic-relation helpers (beat reordering, velocity
  scaling, threshold-monotone transforms) shared across metamorphic tests.

Everything is pure and import-light; nothing here reaches infra.
"""

from __future__ import annotations

__all__: list[str] = []
