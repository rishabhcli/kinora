# `app.cli` — Kinora admin / operations CLI (living roadmap)

A comprehensive operations CLI for Kinora, layered on the wired composition
`Container` (so it reuses every DI seam — repos, budget service, queue, object
store — exactly as the API does). The architecture is deliberately two-layered:

* **Pure action functions** (`app/cli/actions/`) — async functions that take the
  collaborators they need (a `Container`, or a session + repos) and return typed,
  serializable **result dataclasses**. They contain *all* the logic and are
  fully unit-testable with no terminal, no argv, and (where possible) no infra.
* **Thin command shells** (`app/cli/commands/`) — argparse subcommands that parse
  flags, call exactly one action function, and hand the typed result to the
  renderer. Shells contain no business logic.

Output is uniform via `app/cli/output.py`: every result renders as a human
**table** or as machine **json** (`--format {table,json}`), selected once at the
root parser. Results are `Renderable` (expose `.render_payload()`).

## Ownership / shared-file policy
This package is **new and entirely owned** by this work. The only additive
shared-file changes are a `[project.scripts]` console-script entry (`kinora-admin`)
in `backend/pyproject.toml` and a `scripts/kinora-admin` shim — both additive.
No edits to `composition.py`, `core/config.py`, or existing modules.

## kinora.md anchors
* §8 — canon graph + episodic store + budget-as-a-service (inspect/repair).
* §11.1 — the budget accounting system (reports, caps, remaining-seconds).
* §12.1 — render queue, idempotency, DLQ + replay (queue ops).
* §12.5 — observability (the doctor/health command surfaces these).

## Milestones

### M1 — Foundations (DONE)
- `output.py`: `Format`, `Table`, `render`, color-free ASCII tables (pipe-safe).
- `errors.py`: `CliError` (exit-code carrying), `not_found`, `usage`.
- `context.py`: `CliContext` — owns the `Container`, format, run helpers.
- `formatting.py`: `humanize_seconds`, `ago`, `pct`, `truncate`.
- `__init__.py` exports.

### M2 — Doctor / health (DONE)
- `actions/doctor.py`: `run_doctor(container)` → `DoctorReport` probing Postgres,
  Redis, object store, budget gate, live-video gate, queue reach, DB counts.
- `commands/doctor.py`.

### M3 — Book lifecycle (DONE)
- `actions/books.py`: list / inspect / reingest / delete / set-status, with
  page+scene+shot+defect counts and budget spent per book.
- `commands/books.py`.

### M4 — Budget administration + reports (DONE)
- `actions/budget.py`: report (global + per-book/session/scene), remaining,
  ledger tail, caps, efficiency (§13 accepted-footage metric).
- `commands/budget.py`.

### M5 — Queue inspection + DLQ drain/replay (DONE)
- `actions/queue.py`: stats, inspect job, list DLQ, replay (re-enqueue), purge DLQ,
  reap expired leases, cancel by token.
- `commands/queue.py`.

### M6 — Canon inspection + repair (DONE)
- `actions/canon.py`: entity list/inspect (as-of beat), continuity-state list,
  audit-chain verify (hash chain), branch list, integrity checks.
- `commands/canon.py`.

### M7 — User / tenant administration (DONE)
- `actions/users.py`: list, inspect (with book counts), find by email, reassign a
  book's owner, orphan-book report.
- `commands/users.py`.

### M8 — Render-job inspection (DONE)
- `actions/render_jobs.py`: list inflight, inspect job (DB mirror), per-book defects.
- `commands/render.py`.

### M9 — Backfills + maintenance jobs (DONE)
- `actions/maintenance.py`: stuck-import sweep, cache audit, embedding-coverage
  report, table-row census.
- `commands/maintenance.py`.

### M10 — Entrypoint + packaging (DONE)
- `app/cli/__main__.py` (`python -m app.cli`), `app/cli/main.py` (parser tree).
- additive `[project.scripts] kinora-admin` + `scripts/kinora-admin` shim.

## Verification status
- `make lint` clean: ruff (app/tests/scripts) + mypy (400 source files) pass.
- Tests: 94 CLI tests pass (77 pure no-infra + 17 isolated-infra integration).
  Full backend suite stays green (1118 passed / 177 infra-skipped).
- Live smoke against the running stack: `doctor`, `budget report`,
  `budget efficiency`, `queue stats`, `books list` all render in table + json.
- Integration tests run against an isolated throwaway DB (`kinora_cli_test`) +
  redis db 15, never the live `kinora` DB (per the project isolation rule).

## Shared-file changes (additive only)
- `backend/pyproject.toml` — added `[project.scripts] kinora-admin = "app.cli.main:main"`.
- `backend/scripts/kinora_admin.py` — new venv shim (no edits to existing scripts).

## Future roadmap (not yet built)
- Tenant-scoped quota administration once a tenants table lands.
- CSV/NDJSON output formats; a `watch` mode for queue stats.
- Destructive-op confirmation prompts behind a `--yes` gate (TTY).
- A canon *repair* write-path (currently integrity is read-only diagnostics).
- Queue `requeue-by-shot` / bulk DLQ replay.
