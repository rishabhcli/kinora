import { createAuthStore } from "@kinora/core";

/**
 * The desktop auth store + token persistence. For now the token lives in
 * localStorage; the native-capabilities phase moves it to the OS keychain via
 * Electron `safeStorage` over IPC.
 */
const TOKEN_KEY = "kinora.token";

export const authStore = createAuthStore();

export function persistToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage can throw (private mode, quota) — non-fatal.
  }
}

export function loadPersistedToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
