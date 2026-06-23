import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { BookWall } from "../components/BookWall";
import { api } from "../lib/api";
import { authStore, persistToken } from "../lib/auth";

/** The simple demo reader (owns the seeded library) — one tap to explore. */
const DEMO = { email: "e2e@kinora.test", password: "e2e-password-123" } as const;

async function loginAndLoadUser(email: string, password: string): Promise<string | null> {
  const { data, error } = await api.POST("/api/auth/login", { body: { email, password } });
  if (error || !data) return "That email and password didn't match.";
  authStore.getState().setToken(data.access_token);
  persistToken(data.access_token);
  const me = await api.GET("/api/auth/me");
  if (me.error || !me.data) {
    persistToken(null);
    return "Signed in, but couldn't load your account.";
  }
  authStore.getState().setSession(data.access_token, me.data);
  return null;
}

export default function LoginPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState<string>(DEMO.email);
  const [password, setPassword] = useState<string>(DEMO.password);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(currentEmail: string, currentPassword: string) {
    setBusy(true);
    setError(null);
    authStore.getState().setAuthenticating();
    let message: string | null;
    if (mode === "register") {
      const reg = await api.POST("/api/auth/register", {
        body: { email: currentEmail, password: currentPassword },
      });
      message =
        reg.error || !reg.data
          ? "Couldn't create that account."
          : await loginAndLoadUser(currentEmail, currentPassword);
    } else {
      message = await loginAndLoadUser(currentEmail, currentPassword);
    }
    if (message) {
      setError(message);
      authStore.getState().setAnonymous();
      setBusy(false);
    } else {
      navigate("/");
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void run(email, password);
  }

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-walnut font-sans text-white">
      <div className="drag absolute inset-x-0 top-0 z-30 h-12" />
      <BookWall />

      <main className="relative z-20 flex h-full items-center justify-center px-6">
        <section className="glass no-drag w-full max-w-[400px] rounded-glass p-8">
          <header className="mb-7 text-center">
            <h1 className="font-display text-[44px] font-semibold leading-none tracking-tight">
              Kinora
            </h1>
            <p className="mt-2 text-sm text-white/65">Watch the book.</p>
          </header>

          <form onSubmit={onSubmit} className="space-y-3">
            <input
              type="email"
              required
              autoFocus
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="glass-input w-full rounded-xl px-4 py-3 text-sm"
            />
            <input
              type="password"
              required
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="glass-input w-full rounded-xl px-4 py-3 text-sm"
            />
            {error && <p className="px-1 text-sm text-red-300">{error}</p>}
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-2xl bg-gradient-to-b from-ember-glow to-ember-deep py-3 text-[15px] font-semibold text-walnut-deep shadow-[0_12px_34px_-8px_rgba(224,134,58,0.65)] transition hover:brightness-[1.06] active:scale-[0.99] disabled:opacity-60"
            >
              {busy ? "One moment…" : mode === "login" ? "Sign in" : "Create account"}
            </button>
          </form>

          <div className="mt-5 flex items-center justify-between text-xs text-white/55">
            <button
              type="button"
              onClick={() => {
                setEmail(DEMO.email);
                setPassword(DEMO.password);
                setMode("login");
                void run(DEMO.email, DEMO.password);
              }}
              className="rounded-lg px-2 py-1 text-white/75 transition hover:text-white"
            >
              Explore the demo library →
            </button>
            <button
              type="button"
              onClick={() => setMode(mode === "login" ? "register" : "login")}
              className="rounded-lg px-2 py-1 transition hover:text-white"
            >
              {mode === "login" ? "Create account" : "Sign in"}
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}
