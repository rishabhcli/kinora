"""Streaming data plane.

Includes a partitioned append log, a stateful stream processor, and Postgres CDC
into incrementally maintained materialized views.

Self-contained, additive subpackages; importing this namespace is side-effect free.
"""

from __future__ import annotations

__all__: list[str] = []
