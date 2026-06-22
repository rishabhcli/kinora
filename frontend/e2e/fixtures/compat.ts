import type { Page, Route } from "@playwright/test";

import { SEED } from "./seed";

/**
 * Bridge two KNOWN Phase-9 (API) ↔ Phase-10 (frontend) contract mismatches so
 * the REAL UI can run against the REAL backend in e2e. This is a thin, explicit
 * test-layer adapter (it does not modify `backend/app` or `frontend/src`) and is
 * reported as a finding in the Phase-13 deliverable:
 *
 *   1. `GET /api/books`        → backend returns `{ books: [...] }`, the client
 *                                expects a bare `Book[]`. (Same envelope shape
 *                                breaks the shelf without this unwrap.)
 *   2. `GET /api/books/:id/shots` → backend returns `{ book_id, shots: [...] }`
 *                                and each shot OMITS `source_span`, while the
 *                                client expects `Shot[]` with `source_span`
 *                                (the SyncEngine sorts/seeks by it). We unwrap to
 *                                an array and reconstruct `source_span` from the
 *                                deterministic seed grid (shot i → word i*step).
 *
 * Everything else passes through untouched, so the auth / workspace-render /
 * metrics flows are exercised against the unmodified API.
 */
export async function installApiCompat(page: Page): Promise<void> {
  // 1. Unwrap the shelf list: { books: [...] } -> [...]
  await page.route(
    (url) => url.pathname.endsWith("/api/books"),
    async (route: Route) => {
      const resp = await route.fetch();
      const json = await safeJson(resp);
      if (json && Array.isArray((json as { books?: unknown }).books)) {
        await route.fulfill({ response: resp, json: (json as { books: unknown[] }).books });
        return;
      }
      await route.fulfill({ response: resp });
    },
  );

  // 2. Unwrap the shot timeline + reconstruct source_span from the seed grid.
  await page.route(
    (url) => /\/api\/books\/[^/]+\/shots$/.test(url.pathname),
    async (route: Route) => {
      const resp = await route.fetch();
      const json = await safeJson(resp);
      const shots = Array.isArray(json)
        ? (json as Record<string, unknown>[])
        : ((json as { shots?: Record<string, unknown>[] } | null)?.shots ?? null);
      if (!Array.isArray(shots)) {
        await route.fulfill({ response: resp });
        return;
      }
      const patched = shots.map((shot) => withSourceSpan(shot));
      await route.fulfill({ response: resp, json: patched });
    },
  );
}

async function safeJson(resp: { json: () => Promise<unknown> }): Promise<unknown> {
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

function withSourceSpan(shot: Record<string, unknown>): Record<string, unknown> {
  if (shot.source_span) return shot;
  const id = String(shot.shot_id ?? "");
  const match = /(\d+)\s*$/.exec(id);
  const index = match ? Number.parseInt(match[1], 10) : 0;
  const start = index * SEED.wordStep;
  return {
    ...shot,
    source_span: { page: 1, para: 1, word_range: [start, start + SEED.wordStep - 1] },
    est_duration_s: (shot.duration_s as number | undefined) ?? 5,
  };
}
