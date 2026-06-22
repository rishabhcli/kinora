import { type FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { BrandMark } from "../components/common/BrandMark";
import { Spinner } from "../components/common/icons";
import { useAuthStore } from "../stores/authStore";

type Mode = "login" | "register";

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const status = useAuthStore((s) => s.status);
  const error = useAuthStore((s) => s.error);
  const login = useAuthStore((s) => s.login);
  const register = useAuthStore((s) => s.register);
  const clearError = useAuthStore((s) => s.clearError);

  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const from = (location.state as { from?: string } | null)?.from ?? "/";

  useEffect(() => {
    if (status === "authenticated") navigate(from, { replace: true });
  }, [status, from, navigate]);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    try {
      if (mode === "login") await login({ email, password });
      else await register({ email, password });
    } catch {
      // error surfaced via the store
    } finally {
      setSubmitting(false);
    }
  };

  const switchMode = (next: Mode) => {
    setMode(next);
    clearError();
  };

  return (
    <main className="flex min-h-full items-center justify-center px-5 py-12">
      <div className="w-full max-w-md">
        <div className="mb-8 flex flex-col items-center text-center">
          <BrandMark className="h-12 w-12" />
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-kinora-mist">
            {mode === "login" ? "Welcome back" : "Create your library"}
          </h1>
          <p className="mt-2 text-sm text-kinora-muted">
            Kinora turns any book into a film that plays as you read.
          </p>
        </div>

        <div className="glass-strong rounded-3xl p-6 sm:p-8">
          <div className="glass-segment mb-6 grid grid-cols-2 gap-1 rounded-full p-1">
            {(["login", "register"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => switchMode(m)}
                className={`rounded-full px-4 py-2 text-sm font-medium transition-colors ${
                  mode === m
                    ? "bg-kinora-glow text-white shadow"
                    : "text-kinora-muted hover:text-kinora-mist"
                }`}
              >
                {m === "login" ? "Sign in" : "Register"}
              </button>
            ))}
          </div>

          <form onSubmit={onSubmit} className="space-y-4">
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-kinora-muted">
                Email
              </span>
              <input
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="reader@kinora.app"
                className="w-full rounded-xl border border-kinora-line bg-kinora-ink/60 px-4 py-3 text-sm text-kinora-mist outline-none transition-colors placeholder:text-kinora-muted/60 focus:border-kinora-iris/70"
              />
            </label>
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium uppercase tracking-wider text-kinora-muted">
                Password
              </span>
              <input
                type="password"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                required
                minLength={6}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full rounded-xl border border-kinora-line bg-kinora-ink/60 px-4 py-3 text-sm text-kinora-mist outline-none transition-colors placeholder:text-kinora-muted/60 focus:border-kinora-iris/70"
              />
            </label>

            {error ? (
              <p
                role="alert"
                className="rounded-xl border border-kinora-danger/40 bg-kinora-danger/10 px-3 py-2 text-sm text-kinora-danger"
              >
                {error}
              </p>
            ) : null}

            <button
              type="submit"
              disabled={submitting}
              className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-[#6d28d9] px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-[#7c5cff] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {submitting ? <Spinner className="h-4 w-4" /> : null}
              {mode === "login" ? "Sign in" : "Create account"}
            </button>
          </form>
        </div>

        <p className="mt-6 text-center text-xs text-kinora-muted/80">
          {mode === "login" ? "New to Kinora? " : "Already have an account? "}
          <button
            type="button"
            onClick={() => switchMode(mode === "login" ? "register" : "login")}
            className="font-medium text-kinora-iris hover:underline"
          >
            {mode === "login" ? "Create an account" : "Sign in"}
          </button>
        </p>
      </div>
    </main>
  );
}
