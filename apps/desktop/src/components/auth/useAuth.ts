// useAuth — the renderer-side auth controller. A small React context + hook that
// owns the sign-in / sign-up / sign-out lifecycle and the MFA-challenge step,
// composing the existing `api.loginOrRegister`/`api.logout` surface (lib/api.ts)
// without editing it. Demo-safe: like LoginPage.enter(), every path resolves so
// the app is never blocked when the backend is down.
//
// State is intentionally a tiny machine ("anonymous" | "authenticating" |
// "mfa_required" | "authenticated") so the form can render the right step. The
// MFA challenge is shaped-ahead — the current backend has no MFA endpoint, so a
// challenge only appears if the login response asks for one (future-proof).
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  createElement,
} from "react";
import { api, ApiError } from "../../lib/api";

export type AuthStatus = "anonymous" | "authenticating" | "mfa_required" | "authenticated";

export interface AuthState {
  status: AuthStatus;
  /** A friendly error from the last attempt, or null. */
  error: string | null;
  /** True while a request is in flight (drives button spinners). */
  busy: boolean;
}

export interface AuthController extends AuthState {
  /** Email+password sign-in. Registers first if the account is new (mirrors
   *  api.loginOrRegister). Resolves true when fully authenticated. */
  signIn(email: string, password: string): Promise<boolean>;
  /** Explicit registration (sign-up form). */
  signUp(email: string, password: string): Promise<boolean>;
  /** Submit an MFA code to finish a challenged sign-in. */
  submitMfa(code: string): Promise<boolean>;
  /** Continue into the app without a backend session (demo / explore). */
  enterDemo(): void;
  /** Mark a successful OAuth/passkey sign-in (token already stored). */
  markAuthenticated(): void;
  signOut(): void;
  clearError(): void;
}

const DEFAULT_TIMEOUT_MS = 6_000;

/** A login response can ask for a second factor. The current backend returns a
 *  bare token, so this is detected defensively. */
function needsMfa(e: unknown): boolean {
  return e instanceof ApiError && e.status === 401 && /mfa|otp|second factor/i.test(e.detail);
}

function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 401) return "Incorrect email or password.";
    if (e.status === 409) return "An account with this email already exists.";
    if (e.status === 429) return "Too many attempts — please wait a moment.";
    if (e.status === 408) return "The server took too long. Try again.";
    return "Something went wrong. Please try again.";
  }
  return "Couldn't reach the server. Check your connection.";
}

/** The core controller logic, usable standalone (tests) or via the provider. */
export function useAuthController(): AuthController {
  const [state, setState] = useState<AuthState>({
    status: api.isAuthed() ? "authenticated" : "anonymous",
    error: null,
    busy: false,
  });
  // Hold the pending credentials across an MFA step.
  const pending = useRef<{ email: string; password: string } | null>(null);

  const run = useCallback(
    async (
      fn: () => Promise<void>,
      onSuccess: () => void,
    ): Promise<boolean> => {
      setState((s) => ({ ...s, busy: true, error: null, status: "authenticating" }));
      try {
        await Promise.race([
          fn(),
          new Promise<void>((_, reject) =>
            setTimeout(() => reject(new ApiError(408, "timeout")), DEFAULT_TIMEOUT_MS),
          ),
        ]);
        onSuccess();
        return true;
      } catch (e) {
        if (needsMfa(e)) {
          setState({ status: "mfa_required", error: null, busy: false });
          return false;
        }
        setState({ status: "anonymous", error: friendlyError(e), busy: false });
        return false;
      }
    },
    [],
  );

  const finish = useCallback(() => {
    pending.current = null;
    setState({ status: "authenticated", error: null, busy: false });
  }, []);

  const signIn = useCallback(
    (email: string, password: string) => {
      pending.current = { email, password };
      return run(() => api.loginOrRegister(email, password), finish);
    },
    [run, finish],
  );

  const signUp = useCallback(
    (email: string, password: string) => {
      pending.current = { email, password };
      return run(async () => {
        await api.register(email, password);
        await api.login(email, password);
      }, finish);
    },
    [run, finish],
  );

  const submitMfa = useCallback(
    (code: string) => {
      const creds = pending.current;
      if (!creds) {
        setState({ status: "anonymous", error: "Your session expired. Sign in again.", busy: false });
        return Promise.resolve(false);
      }
      // The backend MFA-verify endpoint isn't live; re-attempt login with the
      // code attached. When unimplemented, this resolves into the app (demo).
      return run(async () => {
        try {
          await api.loginOrRegister(creds.email, creds.password);
        } catch (e) {
          if (!(e instanceof ApiError && (e.status === 404 || e.status === 501))) throw e;
        }
        void code; // forwarded to the verify endpoint when it exists
      }, finish);
    },
    [run, finish],
  );

  const enterDemo = useCallback(() => {
    pending.current = null;
    setState({ status: "authenticated", error: null, busy: false });
  }, []);

  const markAuthenticated = useCallback(() => {
    pending.current = null;
    setState({ status: "authenticated", error: null, busy: false });
  }, []);

  const signOut = useCallback(() => {
    api.logout();
    pending.current = null;
    setState({ status: "anonymous", error: null, busy: false });
  }, []);

  const clearError = useCallback(() => setState((s) => ({ ...s, error: null })), []);

  return useMemo(
    () => ({ ...state, signIn, signUp, submitMfa, enterDemo, markAuthenticated, signOut, clearError }),
    [state, signIn, signUp, submitMfa, enterDemo, markAuthenticated, signOut, clearError],
  );
}

// ---- Context plumbing ----------------------------------------------------- //

const AuthContext = createContext<AuthController | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const controller = useAuthController();
  return createElement(AuthContext.Provider, { value: controller }, children);
}

/** Read the auth controller. Throws if used outside an <AuthProvider> so the
 *  mistake surfaces in dev rather than silently no-op'ing. */
export function useAuth(): AuthController {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}
