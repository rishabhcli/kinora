import { createApiClient } from "@kinora/core";

import { authStore, persistToken } from "./auth";
import { API_BASE_URL } from "./config";

/**
 * The shared, authed API client. The bearer token is read from the in-memory
 * auth store, which the bootstrap populates from secure storage on launch.
 */
export const api = createApiClient({
  baseUrl: API_BASE_URL,
  getToken: () => authStore.getState().token,
  onUnauthorized: () => {
    persistToken(null);
    authStore.getState().setAnonymous();
  },
});
