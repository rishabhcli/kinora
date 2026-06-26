"""``app.optim`` — Agent 07's optimization infrastructure (additive, behavior-preserving).

Everything here is **opt-in**: importing the package changes nothing. The modules give the
rest of the backend cheap, well-tested building blocks the composition root can wire behind
default-off flags:

* :mod:`app.optim.cost_meter` — USD cost on top of the physical ``providers.types.Usage``
  units (tokens / images / audio-seconds / video-seconds), rolled up per model / operation /
  book / session. Attaches via the designed ``create_providers(usage_sink=...)`` seam.
* :mod:`app.optim.routing` — a model-router table (cheapest Qwen model that holds the quality
  bar per call-site); the default table is the identity, so wiring is a no-op until an
  override is enabled.
* :mod:`app.optim.prompt_compress` — pure helpers to trim/dedupe context and estimate tokens.
* :mod:`app.optim.batch` — bounded-concurrency + ``429 Throttling.RateQuota`` backoff helpers.

None of these duplicate existing infra (Prometheus ``observability.metrics``, the ``shot_cache``
content-hash clip cache, ``BudgetService`` video-seconds accounting) — they sit *above* it.
"""

from __future__ import annotations

__all__: list[str] = []
