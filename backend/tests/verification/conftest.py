"""Hypothesis configuration for the formal-verification suite.

Three registered profiles trade depth for speed:

* ``dev`` (default) — a brisk 100 examples per property; fast local feedback.
* ``ci`` — 500 examples + a deadline bump; the gate the marathon runs green.
* ``deep`` — 2000 examples, no deadline; an overnight soak that hunts the rare
  shrink. Select with ``--hypothesis-profile=deep``.

The default is read from ``HYPOTHESIS_PROFILE`` so the parent harness can dial the
depth up without editing tests. Determinism (``derandomize``) is *off* so repeated
runs explore fresh inputs; a failure prints the minimal example + a replay seed.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=100, deadline=None)
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=1000,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
