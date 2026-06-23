/**
 * The typed Kinora API client — a thin, fully-typed wrapper over `openapi-fetch`
 * bound to the generated {@link paths}. Both the desktop (Electron) and mobile
 * (Expo) shells construct one of these, injecting their own token storage and
 * 401 handler; everything above it (stores, hooks) stays platform-agnostic.
 */
import createClient, { type Client, type Middleware } from "openapi-fetch";

import type { paths } from "./schema";

/** Returns the current bearer token (or null/undefined when signed out). */
export type TokenProvider = () =>
  | string
  | null
  | undefined
  | Promise<string | null | undefined>;

export interface ApiClientOptions {
  /** Base URL of the cloud backend, e.g. `https://api.kinora.app`. */
  baseUrl: string;
  /** Current bearer token; injected as `Authorization` on every request. */
  getToken?: TokenProvider;
  /** Invoked whenever a response is 401, so the shell can clear the session. */
  onUnauthorized?: () => void;
}

export type KinoraClient = Client<paths>;

export function createApiClient(opts: ApiClientOptions): KinoraClient {
  const client = createClient<paths>({ baseUrl: opts.baseUrl });

  const auth: Middleware = {
    async onRequest({ request }) {
      const token = await opts.getToken?.();
      if (token) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    },
    onResponse({ response }) {
      if (response.status === 401) opts.onUnauthorized?.();
      return response;
    },
  };

  client.use(auth);
  return client;
}
