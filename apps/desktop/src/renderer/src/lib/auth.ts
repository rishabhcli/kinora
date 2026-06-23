import { createAuthStore } from "@kinora/core";

/**
 * The desktop auth store + token persistence. In Electron the token is stored
 * encrypted via the main process's safeStorage bridge; outside Electron (tests,
 * a plain browser) it falls back to localStorage.
 */
const TOKEN_KEY = "kinora.token";

export const authStore = createAuthStore();

interface SecureBridge {
  getToken: () => Promise<string | null>;
  setToken: (token: string | null) => Promise<void>;
}

function secureBridge(): SecureBridge | null {
  return (globalThis as { kinora?: { secure?: SecureBridge } }).kinora?.secure ?? null;
}

export async function loadPersistedToken(): Promise<string | null> {
  const bridge = secureBridge();
  if (bridge) return bridge.getToken();
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function persistToken(token: string | null): void {
  const bridge = secureBridge();
  if (bridge) {
    void bridge.setToken(token);
    return;
  }
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage can throw (private mode, quota) — non-fatal.
  }
}
