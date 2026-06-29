"""Kinora platform-engineering namespace.

Cross-cutting *platform* subsystems the product is built on top of (not features
of the product itself), sitting above the domain (agents, render, ingest) and
below the API surface. Each sub-package is self-contained and additive; importing
this namespace is side-effect free — subsystems wire in explicitly when enabled.

Members: :mod:`app.platform.plugins` (sandboxed extension platform),
:mod:`app.platform.workflows` (Temporal-style durable-execution engine),
:mod:`app.platform.authz` (unified authorization plane).
"""

from __future__ import annotations

__all__: list[str] = []
