import { useState, type FormEvent } from "react";
import { motion } from "framer-motion";
import BookWall from "./BookWall";

const DEMO = { email: "demo@kinora.local", password: "demo-password-123" } as const;

/** The login screen, set against a living wall of scrolling book covers.
 *  There is no backend in this build, so "signing in" is a mock that simply
 *  enters the app — the form is here for the look and the flow. */
export default function LoginPage({ onEnter }: { onEnter: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState<string>(DEMO.email);
  const [password, setPassword] = useState<string>(DEMO.password);
  const [busy, setBusy] = useState(false);

  function enter() {
    setBusy(true);
    window.setTimeout(() => {
      setBusy(false);
      onEnter();
    }, 450);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    enter();
  }

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-kinora-bg-deep font-sans text-kinora-text">
      <BookWall columns={5} />

      <main className="relative z-20 flex h-full items-center justify-center px-6">
        <motion.section
          initial={{ opacity: 0, y: 16, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
          className="glass-card w-full max-w-[400px] rounded-3xl p-8 backdrop-blur-2xl"
          style={{ background: "rgba(20, 18, 16, 0.55)" }}
        >
          <header className="mb-7 text-center">
            <h1 className="font-serif text-[44px] font-semibold leading-none tracking-tight text-kinora-text">
              Kinora
            </h1>
            <p className="mt-2 text-sm text-kinora-muted">Watch the book.</p>
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
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-2xl bg-gradient-to-b from-kinora-gold-light to-kinora-gold py-3 text-[15px] font-semibold text-kinora-bg-deep shadow-[0_12px_34px_-8px_rgba(212,164,78,0.6)] transition hover:brightness-[1.06] active:scale-[0.99] disabled:opacity-60"
            >
              {busy ? "One moment…" : mode === "login" ? "Sign in" : "Create account"}
            </button>
          </form>

          <div className="mt-5 flex items-center justify-between text-xs text-kinora-muted">
            <button
              type="button"
              onClick={() => {
                setMode("login");
                enter();
              }}
              className="rounded-lg px-2 py-1 text-kinora-text/80 transition hover:text-kinora-text"
            >
              Explore the demo library →
            </button>
            <button
              type="button"
              onClick={() => setMode(mode === "login" ? "register" : "login")}
              className="rounded-lg px-2 py-1 transition hover:text-kinora-text"
            >
              {mode === "login" ? "Create account" : "Sign in"}
            </button>
          </div>
        </motion.section>
      </main>
    </div>
  );
}
