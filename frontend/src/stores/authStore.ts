import { create } from "zustand";

import { ApiError, auth } from "../api/client";
import { getToken, setToken } from "../api/token";
import type { Credentials, User } from "../api/types";

export type AuthStatus =
  | "unknown"
  | "authenticating"
  | "authenticated"
  | "anonymous";

interface AuthState {
  token: string | null;
  user: User | null;
  status: AuthStatus;
  error: string | null;
  login: (creds: Credentials) => Promise<void>;
  register: (creds: Credentials) => Promise<void>;
  logout: () => void;
  /** On app load: if a token is persisted, validate it via /auth/me. */
  bootstrap: () => Promise<void>;
  clearError: () => void;
}

function messageFor(err: unknown, on401: string): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return on401;
    if (err.status === 0) return err.message;
    return err.message;
  }
  return "Something went wrong. Please try again.";
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: getToken(),
  user: null,
  status: getToken() ? "unknown" : "anonymous",
  error: null,

  login: async (creds) => {
    set({ status: "authenticating", error: null });
    try {
      const { access_token } = await auth.login(creds);
      setToken(access_token);
      const user = await auth.me();
      set({ token: access_token, user, status: "authenticated", error: null });
    } catch (err) {
      setToken(null);
      set({
        token: null,
        user: null,
        status: "anonymous",
        error: messageFor(err, "Incorrect email or password."),
      });
      throw err;
    }
  },

  register: async (creds) => {
    set({ status: "authenticating", error: null });
    try {
      await auth.register(creds);
    } catch (err) {
      set({
        status: "anonymous",
        error: messageFor(err, "That email is already registered."),
      });
      throw err;
    }
    // Registration succeeded → log straight in.
    await get().login(creds);
  },

  logout: () => {
    setToken(null);
    set({ token: null, user: null, status: "anonymous", error: null });
  },

  bootstrap: async () => {
    const token = getToken();
    if (!token) {
      set({ status: "anonymous" });
      return;
    }
    set({ status: "authenticating" });
    try {
      const user = await auth.me();
      set({ token, user, status: "authenticated" });
    } catch {
      setToken(null);
      set({ token: null, user: null, status: "anonymous" });
    }
  },

  clearError: () => set({ error: null }),
}));
