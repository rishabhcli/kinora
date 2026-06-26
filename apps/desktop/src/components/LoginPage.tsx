import { useState, type FormEvent } from "react";
import { api } from "../lib/api";
import logoImg from "../assets/logo-transparent.png";

const DEMO = { email: "demo@kinora.local", password: "demo-password-123" } as const;

export default function LoginPage({ onEnter }: { onEnter: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState<string>(DEMO.email);
  const [password, setPassword] = useState<string>(DEMO.password);
  const [busy, setBusy] = useState(false);

  async function enter() {
    setBusy(true);
    try {
      await api.loginOrRegister(email, password);
    } catch {
      /* backend down — continue in demo mode */
    }
    setBusy(false);
    onEnter();
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    enter();
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden font-sans text-white">
      {/* Left half with static gradient background */}
      <div className="login-hero relative w-1/2 overflow-hidden">
        <div className="aurora-vignette" />

        {/* Tagline content */}
        <div className="absolute inset-0 flex flex-col justify-between p-12" style={{ zIndex: 10 }}>
          <div className="flex items-center gap-3">
            <img src={logoImg} alt="" width={36} height={36} style={{ objectFit: "contain" }} />
            <span className="font-serif text-[18px] font-medium tracking-wide text-white/90">Kinora</span>
          </div>
          <div className="max-w-md">
            <h1 className="font-serif text-[44px] leading-[1.1] font-medium text-white">
              Where stories<br />come to life.
            </h1>
            <p className="mt-5 text-[14px] leading-relaxed text-white/55">
              Your library, reimagined as cinema. Books become films, pages become scenes — watched in the quiet of an evening.
            </p>
          </div>
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.2em] text-white/35">
            <span className="inline-block h-px w-8 bg-white/35" />
            Reading, rewritten
          </div>
        </div>
      </div>

      {/* Right half — login form */}
      <div className="kinora-bg relative flex w-1/2 items-center justify-center overflow-hidden">
      <div
        className="login-form-enter w-full max-w-[380px] px-6"
      >
        {/* Heading */}
        <h2 className="mb-1.5 text-center font-serif text-[22px] font-semibold text-white">
          {mode === "login" ? "Sign in" : "Sign up"}
        </h2>
        <p className="mb-6 text-center text-[13px] text-kinora-muted">
          {mode === "login"
            ? "Welcome back to your library"
            : "Start watching books as films"}
        </p>

        {/* Form */}
        <form onSubmit={onSubmit} className="space-y-4">
          <input
            type="email"
            required
            autoFocus
            placeholder="Email address"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="login-input w-full rounded-lg px-4 py-2.5 text-[13px]"
          />
          <input
            type="password"
            required
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="login-input w-full rounded-lg px-4 py-2.5 text-[13px]"
          />

          {mode === "login" && (
            <div className="flex items-center justify-between text-[12px]">
              <label className="flex cursor-pointer items-center gap-2 text-kinora-muted">
                <input type="checkbox" className="h-3.5 w-3.5 accent-kinora-muted" defaultChecked />
                Remember me
              </label>
              <button type="button" className="text-kinora-muted transition hover:text-kinora-text">
                Forgot password?
              </button>
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="login-btn mt-2 w-full rounded-lg py-2.5 text-[13px] font-semibold transition disabled:opacity-50"
          >
            {busy ? "One moment…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>

        {/* Divider */}
        <div className="my-5 flex items-center gap-3">
          <div className="h-px flex-1" style={{ background: "rgba(255,255,255,0.06)" }} />
          <span className="text-[10px] uppercase tracking-widest text-kinora-subtle">or</span>
          <div className="h-px flex-1" style={{ background: "rgba(255,255,255,0.06)" }} />
        </div>

        {/* Social login */}
        <div className="space-y-2.5">
          {[
            { name: "Google", icon: "google" },
            { name: "Apple", icon: "apple" },
            { name: "GitHub", icon: "github" },
          ].map((p) => (
            <button
              key={p.name}
              type="button"
              onClick={enter}
              className="flex w-full items-center justify-center gap-2.5 rounded-lg py-2 text-[12px] font-medium transition hover:bg-white/8"
              style={{
                background: "rgba(255, 255, 255, 0.06)",
                border: "1px solid rgba(255, 255, 255, 0.08)",
                color: "rgba(255, 255, 255, 0.95)",
              }}
            >
              {p.icon === "google" && (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                  <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
                  <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                  <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
                  <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
                </svg>
              )}
              {p.icon === "apple" && (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="rgba(255,255,255,0.95)">
                  <path d="M17.05 20.28c-.98.95-2.05.8-3.08.35-1.09-.46-2.09-.48-3.24 0-1.44.62-2.2.44-3.06-.35C2.79 15.25 3.51 7.59 9.05 7.31c1.35.07 2.29.74 3.08.8 1.18-.24 2.31-.93 3.57-.84 1.51.12 2.65.72 3.4 1.8-3.12 1.87-2.38 5.98.48 7.13-.57 1.5-1.31 2.99-2.54 4.09l.01-.01zM12.03 7.25c-.15-2.23 1.66-4.07 3.74-4.25.29 2.58-2.34 4.5-3.74 4.25z"/>
                </svg>
              )}
              {p.icon === "github" && (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="rgba(255,255,255,0.95)">
                  <path d="M12 2C6.48 2 2 6.48 2 12c0 4.42 2.87 8.17 6.84 9.5.5.09.66-.22.66-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.45-1.15-1.11-1.46-1.11-1.46-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.89 1.52 2.34 1.08 2.91.83.09-.65.35-1.09.63-1.34-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.02.8-.22 1.65-.33 2.5-.33.85 0 1.7.11 2.5.33 1.91-1.29 2.75-1.02 2.75-1.02.55 1.37.2 2.39.1 2.64.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85V21c0 .27.16.58.67.48C19.13 20.17 22 16.42 22 12c0-5.52-4.48-10-10-10z"/>
                </svg>
              )}
              Continue with {p.name}
            </button>
          ))}
        </div>

        {/* Demo + switch */}
        <button
          type="button"
          onClick={() => { setMode("login"); enter(); }}
          className="mt-3 w-full text-center text-[11px] text-kinora-subtle transition hover:text-kinora-muted"
        >
          Explore the demo library →
        </button>

        <div className="mt-5 flex items-center justify-center gap-1.5 text-[12px]">
          <span className="text-kinora-subtle">
            {mode === "login" ? "New to Kinora?" : "Already have an account?"}
          </span>
          <button
            type="button"
            onClick={() => setMode(mode === "login" ? "register" : "login")}
            className="text-white transition hover:text-white/80"
          >
            {mode === "login" ? "Sign up" : "Sign in"}
          </button>
        </div>
      </div>
      </div>
    </div>
  );
}
