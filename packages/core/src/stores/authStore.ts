/**
 * The auth store — a framework-agnostic Zustand *vanilla* store (no React
 * dependency) holding the session token + user. Both shells instantiate one and
 * bind it with `useStore` from their own React; the token here is what the API
 * client's `getToken` reads and the socket sends.
 */
import { createStore } from "zustand/vanilla";

import type { UserResponse } from "../api/types";

export type AuthStatus = "unknown" | "authenticating" | "authenticated" | "anonymous";

export interface AuthState {
  status: AuthStatus;
  token: string | null;
  user: UserResponse | null;
  setAuthenticating: () => void;
  setSession: (token: string, user: UserResponse) => void;
  setAnonymous: () => void;
}

export function createAuthStore() {
  return createStore<AuthState>((set) => ({
    status: "unknown",
    token: null,
    user: null,
    setAuthenticating: () => set({ status: "authenticating" }),
    setSession: (token, user) => set({ status: "authenticated", token, user }),
    setAnonymous: () => set({ status: "anonymous", token: null, user: null }),
  }));
}

export type AuthStore = ReturnType<typeof createAuthStore>;
