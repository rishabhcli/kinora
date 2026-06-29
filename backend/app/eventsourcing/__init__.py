"""Event Sourcing core for the Kinora backend (kinora.md §6, §9.7).

Two facets compose this package:

* **Facet A — the event store** (:mod:`app.eventsourcing.store`): the append-only
  persistence seam (the :class:`~app.eventsourcing.store.protocol.EventStore`
  protocol, optimistic-concurrency contract, and concrete adapters). Owned by a
  sibling agent; this package defines the protocol **minimally** here when that
  facet is absent on disk so the write side stays testable in isolation
  (see ``app/eventsourcing/DESIGN.md``).
* **Facet B — the command + aggregate model** (:mod:`app.eventsourcing.domain`):
  the CQRS *write side*. A command bus with middleware (validation, an auth seam,
  idempotency), aggregate roots that *decide -> emit domain events* and *rebuild
  from history*, an event-versioning + upcaster framework, the domain events that
  model the reading-Session lifecycle / the §9.7 render-shot lifecycle / canon-edit
  flows, optimistic-concurrency retries, and a saga-trigger seam.

Everything here is **pure and import-safe**: no sockets, no DB, no event loop at
import time. The decision functions on each aggregate are deterministic and
exhaustively unit-tested; persistence is reached only through the injected
:class:`EventStore` protocol.
"""

from __future__ import annotations

__all__ = ["domain", "store"]
