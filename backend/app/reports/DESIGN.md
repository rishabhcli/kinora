# Reports & document generation (`backend/app/reports/`)

A self-contained reporting + document-generation subsystem: a composable report
model rendered to **PDF / HTML / CSV / JSON**, reader-facing keepsakes and
operator dashboards sourced through read-only seams, a pure-Python SVG chart
engine (no new deps), on-demand + scheduled generation with artifact storage +
signed retrieval, branding/theming, and deterministic golden-file tests.

This is a **new package** — additive only. It touches four shared files
additively (documented below) and adds one Alembic migration on the current head.

## Layers (bottom → top)

| Module | Role |
|---|---|
| `model.py` | The composable, immutable, JSON-round-trippable document model — `Report` → `Section` → blocks (`Heading`, `Paragraph`, `KeyValue`, `Table`, `Chart`, `Callout`, `Badge`, `Divider`, `Spacer`). Renderer-agnostic. |
| `theme.py` | `Brand` (palette + type scale + logo). House dark brand + a light certificate brand. Hex→RGB for the PDF. |
| `charts.py` | Pure-Python **SVG** renderer: bar, grouped-bar, line, area, pie, donut, sparkline, progress. Deterministic (fixed coordinate rounding). The same SVG embeds in HTML and rasterises into the PDF. |
| `format.py` | Format-once helpers (ints, %, durations, dates) so every renderer agrees byte-for-byte — the basis of the golden tests. |
| `render/` | Four renderers: `json_render` (model verbatim, the machine contract), `csv_render` (every table flattened), `html` (self-contained, themed, inline SVG), `pdf` (PyMuPDF flowing layout engine with pagination + cover + footers). `render(report, fmt, brand) -> bytes` dispatch + a `MediaType` registry. |
| `sources.py` | **Read-only** data seams: `ReaderSource` (progress, library rollups) + `OperatorSource` (budget §11, quality §13, render throughput §12, library overview). Returns frozen aggregate dataclasses, never live rows. SELECT-only, zero video-seconds. |
| `builders/` | **Pure functions** aggregate → `Report`: reader (`reading_progress`, `completion_certificate`, `year_in_review`, `highlights_digest`) + operator (`budget`, `quality`, `render_throughput`, `library_overview`). |
| `storage.py` | `ReportArtifactStore` — content-addressed object storage + signed retrieval (the §8.7 "re-read costs nothing" idea applied to documents). |
| `db_model.py` + `repository.py` | `report_artifacts` index table + its repo (create / dedup / list / expire). |
| `service.py` | The orchestrator: build → render → (dedup) store → index. One path for on-demand + scheduled. `ReportRequest` → `GeneratedReport`. |
| `schedule.py` | Pure scheduled-report planner (`due_jobs(now, last_runs)`); default operator dashboards + per-reader weekly digest. No clock, no loop. |
| `run.py` | `python -m app.reports.run` CLI. |
| `../api/routes/reports.py` | `POST /reports`, `GET /reports`, `GET /reports/{id}`, `GET /reports/{id}/download`, `GET /reports/preview`. |

## §-citations
- **§13** — operator quality report mirrors the eval metrics: accepted-footage
  efficiency, regeneration rate, CCS, crew-vs-baseline grouped bars. Pre-registered
  thresholds (`EFFICIENCY_TARGET`/`CCS_TARGET`/`REGEN_TARGET`) drive the PASS badge.
- **§11** — budget report reads the append-only ledger (committed + outstanding
  reserved + remaining vs ceiling).
- **§12** — throughput report reads render-job statuses (success rate, dead-letters,
  retries).
- **§8.7** — content-addressed artifact dedup (sha256 of rendered bytes).

## Additive shared-file changes (no edits to others' logic)
1. `app/api/routes/__init__.py` — append `reports` to imports + `ROUTERS`.
2. `app/db/models/__init__.py` — import `ReportArtifact` so its table registers on
   `Base.metadata` (single table-registration entry point).
3. `app/core/config.py` — add `report_operator_emails` + `report_url_ttl_s`
   settings and an `is_report_operator(email)` helper (operator-report gate; locked
   down outside `local` until an allowlist is set).
4. `migrations/versions/r1e2p3o4r5t6_report_artifacts.py` — new table on head
   `a1b2c3d4e5f6` (unique revision id; up + down verified, fully reversible).

## Tests
- Unit (no infra): `test_reports_model`, `_format`, `_charts`, `_theme`, `_render`,
  `_builders`, `_storage`, `_schedule` — 85 tests.
- Golden-file: `test_reports_golden` + `tests/reports_golden/*` (SVG charts + JSON/CSV/HTML
  for 4 report kinds). Regenerate with `KINORA_REGEN_GOLDEN=1`.
- Integration (isolated infra `kinora_reports_test` :5433 / redis db 15 / MinIO):
  `test_reports_service`, `test_api_reports` — full generate→store→index→retrieve→download,
  dedup, ownership, operator gate. Skip cleanly when infra env is unset.

## Roadmap (future phases)
- Wire `ReportService` into the composition root + a scheduled-generation sweep on
  the API (the `schedule.due_jobs` planner is ready; only the loop + a
  `report_runs` last-run store are pending).
- Retention sweep using `ReportArtifactRepo.list_expired` + `expires_at`.
- More chart families (stacked bar, heatmap) + a contents/TOC block for long PDFs.
- Email delivery of the highlights digest (HTML render is already email-ready).
- A desktop "My reports" surface backed by `GET /reports`.
