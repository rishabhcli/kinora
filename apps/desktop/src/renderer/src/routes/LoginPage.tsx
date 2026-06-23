import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";

/** Log in, persist the token, then load the user — returns an error message or null. */
async function loginAndLoadUser(email: string, password: string): Promise<string | null> {
  const { data, error } = await api.POST("/api/auth/login", { body: { email, password } });
  if (error || !data) return "Invalid email or password.";
  // Put the token in the store *before* /me so the API client authenticates it.
  authStore.getState().setToken(data.access_token);
  persistToken(data.access_token);
  const me = await api.GET("/api/auth/me");
  if (me.error || !me.data) {
    persistToken(null);
    return "Signed in, but could not load your account.";
  }
  authStore.getState().setSession(data.access_token, me.data);
  return null;
}

export default function LoginPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    authStore.getState().setAuthenticating();

    let message: string | null;
    if (mode === "register") {
      const reg = await api.POST("/api/auth/register", { body: { email, password } });
      message =
        reg.error || !reg.data ? "Could not create that account." : await loginAndLoadUser(email, password);
    } else {
      message = await loginAndLoadUser(email, password);
    }

    if (message) {
      setError(message);
      authStore.getState().setAnonymous();
      setBusy(false);
    } else {
      navigate("/");
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-950 px-4 text-neutral-100">
      <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4">
        <div className="text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Kinora</h1>
          <p className="mt-1 text-sm text-neutral-400">watch the book</p>
        </div>
        <input
          type="email"
          required
          placeholder="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-600"
        />
        <input
          type="password"
          required
          placeholder="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full rounded-md border border-neutral-800 bg-neutral-900 px-3 py-2 text-sm outline-none focus:border-neutral-600"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-indigo-500 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-400 disabled:opacity-50"
        >
          {busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
        </button>
        <button
          type="button"
          onClick={() => setMode(mode === "login" ? "register" : "login")}
          className="w-full text-center text-xs text-neutral-400 hover:text-neutral-200"
        >
          {mode === "login" ? "Need an account? Register" : "Have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}
