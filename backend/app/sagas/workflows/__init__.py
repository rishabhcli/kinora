"""Concrete saga definitions for Kinora's multi-step flows.

These build real :class:`~app.sagas.definition.Workflow` graphs for the two
durable pipelines the engine exists to make recoverable:

* :mod:`app.sagas.workflows.ingest` — Phase-A ingest
  (parse → keyframes → identity → canon), with compensations that delete the
  artefacts a failed import leaves behind.
* :mod:`app.sagas.workflows.render_shot` — the §9.7 per-shot render saga
  (reserve budget → design → generate → normalize → persist → QA), with the
  budget reservation released and the OSS object deleted on a failure past the
  point of generation, plus the §9.5/§12.4 branch to the degradation ladder.

The actions/compensations are expressed against small, injected **ports**
(Protocols) so the workflows are pure orchestration: tests drive them with
in-memory fakes and the production composition root wires the real
budget/provider/storage/QA services to the same ports. No provider, DB, or
ffmpeg is imported here.
"""

from __future__ import annotations
