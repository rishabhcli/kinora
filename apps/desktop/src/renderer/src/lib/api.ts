import { createApiClient } from "@kinora/core";

import { authStore, loadPersistedToken, persistToken } from "./auth";
import { API_BASE_URL } from "./config";

/**
 * The shared, authed API client. The token comes from the auth store, falling
 * back to the persisted token so a cold start can call `/me` to restore the
 * session before the store is populated.
 */
export const api = createApiClient({
  baseUrl: API_BASE_URL,
  getToken: () => authStore.getState().token ?? loadPersistedToken(),
  onUnauthorized: () => {
    persistToken(null);
    authStore.getState().setAnonymous();
  },
});
