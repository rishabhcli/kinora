// A minimal typed GET helper for the films API. It reuses the base client's
// PUBLIC surface from `../api` — the base URL (`api.base`), the bearer token
// (`auth.token`), and the `ApiError` type — without editing or forking it.
//
// Agent 12 owns `lib/api.ts`. The mission's intended end-state is a generic
// `http` exported from there; once it lands, `films.ts` switches its import
// from `./http` to `../api` and this shim is deleted. See
// `coordination/requests/agent-03.md`.
import { api, auth, ApiError } from "../api";

async function get<T>(path: string): Promise<T> {
  const headers = new Headers();
  if (auth.token) headers.set("Authorization", `Bearer ${auth.token}`);
  const res = await fetch(`${api.base}${path}`, { headers });
  if (!res.ok) {
    throw new ApiError(res.status, await res.text().catch(() => res.statusText));
  }
  return res.status === 204 ? (null as T) : ((await res.json()) as T);
}

/** Shape-compatible with the `http` Agent 12 will export from `lib/api.ts`. */
export const http = { get };
