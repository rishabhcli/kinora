import { createApiClient } from "@kinora/core";

import { authStore, persistToken } from "./auth";
import { API_BASE_URL } from "./config";

/** The shared, authed API client (uses React Native's global fetch). */
export const api = createApiClient({
  baseUrl: API_BASE_URL,
  getToken: () => authStore.getState().token,
  onUnauthorized: () => {
    persistToken(null);
    authStore.getState().setAnonymous();
  },
});
