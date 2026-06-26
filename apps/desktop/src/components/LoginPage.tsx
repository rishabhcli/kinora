import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AnimatePresence, motion, useReducedMotion, type Variants } from "framer-motion";
import AmbientBackdrop from "./auth/AmbientBackdrop";
import BrandLockup from "./auth/BrandLockup";
import Field from "./auth/Field";
import PasswordField from "./auth/PasswordField";
import SocialRow from "./auth/SocialRow";
import AuthIcon from "./auth/AuthIcon";
import { validateEmail, validatePassword, type AuthMode } from "./auth/validation";
import { pickBackdropVariant } from "./auth/backdrop";
import { warmLibraryCovers } from "./auth/coverCache";
import { api, ApiError } from "../lib/api";

const DEMO = { email: "demo@kinora.local", password: "demo-password-123" } as const;
const INTRO_KEY = "kinora.auth.introPlayed";
const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];

type Status = "idle" | "submitting" | "success" | "error";
type Phase = "ready" | "leaving";

export default function LoginPage({ onEnter }: { onEnter: () => void }) {
  const prefersReduced = useReducedMotion() ?? false;

  const [mode, setMode] = useState<AuthMode>("login");
  const [email, setEmail] = useState<string>(DEMO.email);
  const [password, setPassword] = useState<string>(DEMO.password);
  const [emailTouched, setEmailTouched] = useState(false);
  const [pwTouched, setPwTouched] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [remember, setRemember] = useState(true);
  const [status, setStatus] = useState<Status>("idle");
  const [formMsg, setFormMsg] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [announce, setAnnounce] = useState("");
  const [phase, setPhase] = useState<Phase>("ready");

  const emailRef = useRef<HTMLInputElement>(null);
  const pwRef = useRef<HTMLInputElement>(null);

  // A different "hour of the evening" per launch (deterministic for the session).
  const variant = useMemo(() => {
    const seed =
      typeof sessionStorage !== "undefined" && sessionStorage.getItem(INTRO_KEY)
        ? "kinora-return"
        : Date.now();
    return pickBackdropVariant(seed);
  }, []);

  // Cold-launch warm-up plays once per app launch; returning to login (after
  // logout) appears instantly. Reduced motion always appears instantly.
  const playIntro = useRef(
    !prefersReduced &&
      typeof sessionStorage !== "undefined" &&
      !sessionStorage.getItem(INTRO_KEY),
  ).current;

  useEffect(() => {
    if (typeof sessionStorage !== "undefined") sessionStorage.setItem(INTRO_KEY, "1");
    const t = setTimeout(() => emailRef.current?.focus(), playIntro ? 1050 : 120);
    return () => clearTimeout(t);
  }, [playIntro]);

  const emailError = validateEmail(email);
  const pwError = validatePassword(password, mode);
  const busy = status === "submitting" || status === "success" || phase === "leaving";

  function leave(message: string) {
    setStatus("success");
    setAnnounce(message);
    void warmLibraryCovers(); // warm covers + offline cache; never throws
    if (prefersReduced) {
      onEnter();
      return;
    }
    // brief success beat, then the card recedes and the library opens behind it
    setTimeout(() => setPhase("leaving"), 460);
  }

  async function runAuth(validate: boolean, creds?: { email: string; password: string }) {
    if (busy) return;
    // Resolve creds explicitly so demo/social entry never races React state.
    const useEmail = (creds?.email ?? email).trim();
    const usePassword = creds?.password ?? password;
    if (validate) {
      setSubmitted(true);
      if (emailError || pwError) {
        setStatus("error");
        const first = emailError ? "email" : "password";
        setAnnounce(emailError ?? pwError ?? "");
        (first === "email" ? emailRef : pwRef).current?.focus();
        return;
      }
    }
    setStatus("submitting");
    setFormMsg(null);
    setAnnounce(mode === "login" ? "Signing you in…" : "Creating your account…");
    try {
      await api.loginOrRegister(useEmail, usePassword);
      leave(mode === "login" ? "Welcome back. Opening your library…" : "Welcome to Kinora. Opening your library…");
    } catch (e) {
      if (e instanceof ApiError) {
        // Backend reachable but refused — real, recoverable feedback.
        setStatus("error");
        const text =
          e.status === 429
            ? "Too many attempts. Give it a moment and try again."
            : "We couldn't sign you in. Check your email and password.";
        setFormMsg({ kind: "error", text });
        setAnnounce(text);
        pwRef.current?.focus();
      } else {
        // Network unreachable — continue into the offline demo library.
        leave("You're offline — opening the demo library…");
      }
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void runAuth(true);
  }

  function exploreDemo() {
    setEmail(DEMO.email);
    setPassword(DEMO.password);
    setFormMsg(null);
    void runAuth(false, { email: DEMO.email, password: DEMO.password });
  }

  function onForgot() {
    const text = "Reset isn't wired up in the demo — the demo password is prefilled.";
    setFormMsg({ kind: "info", text });
    setAnnounce(text);
  }

  function switchMode() {
    setMode((m) => (m === "login" ? "register" : "login"));
    setFormMsg(null);
    setSubmitted(false);
    setStatus("idle");
  }

  // ---- motion ---------------------------------------------------------------
  const animateState = phase === "leaving" ? "leaving" : "ready";
  const initial = playIntro ? "hidden" : false;

  const rootV: Variants = {
    hidden: {},
    ready: { transition: { staggerChildren: 0.12, delayChildren: 0.15 } },
    leaving: {},
  };
  const fade = (y: number): Variants =>
    prefersReduced
      ? { hidden: { opacity: 0 }, ready: { opacity: 1 }, leaving: { opacity: 0 } }
      : {
          hidden: { opacity: 0, y },
          ready: { opacity: 1, y: 0, transition: { duration: 0.7, ease: EASE } },
          leaving: { opacity: 0, y: -8, transition: { duration: 0.4, ease: EASE } },
        };
  const backdropV: Variants = prefersReduced
    ? { hidden: { opacity: 0 }, ready: { opacity: 1 }, leaving: { opacity: 1 } }
    : {
        hidden: { opacity: 0, scale: 1.06 },
        ready: { opacity: 1, scale: 1, transition: { duration: 1.4, ease: EASE } },
        leaving: { scale: 1.05, transition: { duration: 0.6, ease: EASE } },
      };
  const cardV: Variants = prefersReduced
    ? { hidden: { opacity: 0 }, ready: { opacity: 1 }, leaving: { opacity: 0 } }
    : {
        hidden: { opacity: 0, y: 20, scale: 0.985 },
        ready: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.7, ease: EASE } },
        leaving: { opacity: 0, scale: 1.07, y: -14, transition: { duration: 0.5, ease: EASE } },
      };
  const bloomV: Variants = {
    hidden: { opacity: 0 },
    ready: { opacity: 0 },
    leaving: { opacity: prefersReduced ? 0 : 1, transition: { duration: 0.55, ease: EASE } },
  };

  const headingId = "auth-heading";

  return (
    <motion.div className="auth-root" variants={rootV} initial={initial} animate={animateState}>
      <motion.div variants={backdropV} className="auth-backdrop-layer">
        <AmbientBackdrop variant={variant} reducedMotion={prefersReduced} rows={5} />
      </motion.div>
      <motion.div variants={bloomV} className="auth-bloom" aria-hidden="true" />

      <div className="auth-layout">
        {/* Brand rail — the first 5 seconds. */}
        <motion.aside className="auth-brand" variants={fade(16)}>
          <BrandLockup size="md" className="auth-brand-lockup" />
          <div className="auth-brand-copy">
            <p className="auth-eyebrow">
              <span className="auth-eyebrow-rule" />
              Now showing
            </p>
            <h1 className="auth-tagline">
              Where stories
              <br />
              come to life.
            </h1>
            <p className="auth-sub">
              Your library, reimagined as cinema. Books become films, pages become scenes —
              watched in the quiet of an evening.
            </p>
          </div>
          <p className="auth-footnote">Reading, rewritten</p>
        </motion.aside>

        {/* The card. */}
        <motion.div
          className="auth-card-wrap"
          variants={cardV}
          onAnimationComplete={(d) => {
            if (typeof d === "string" && d === "leaving") onEnter();
          }}
        >
          <section className="auth-card" role="form" aria-labelledby={headingId}>
            <header className="auth-card-head">
              <BrandLockup size="sm" className="auth-card-mark" />
              <h2 id={headingId} className="auth-card-title">
                {mode === "login" ? "Sign in" : "Create your account"}
              </h2>
              <p className="auth-card-sub">
                {mode === "login"
                  ? "Welcome back to your library."
                  : "Start watching books as films."}
              </p>
            </header>

            <form onSubmit={onSubmit} noValidate className="auth-form">
              <Field
                ref={emailRef}
                id="auth-email"
                label="Email address"
                icon="mail"
                type="email"
                inputMode="email"
                autoComplete="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onBlur={() => setEmailTouched(true)}
                error={emailError}
                showError={emailTouched || submitted}
                disabled={busy}
              />
              <PasswordField
                ref={pwRef}
                id="auth-password"
                label="Password"
                value={password}
                onChange={setPassword}
                onBlur={() => setPwTouched(true)}
                error={pwError}
                showError={pwTouched || submitted}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                meter={mode === "register"}
              />

              <AnimatePresence initial={false}>
                {mode === "login" && (
                  <motion.div
                    key="row"
                    className="auth-row"
                    initial={prefersReduced ? false : { opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: "auto" }}
                    exit={prefersReduced ? { opacity: 0 } : { opacity: 0, height: 0 }}
                    transition={{ duration: 0.25, ease: EASE }}
                  >
                    <label className="auth-check">
                      <input
                        type="checkbox"
                        checked={remember}
                        onChange={(e) => setRemember(e.target.checked)}
                      />
                      <span className="auth-check-box" aria-hidden="true">
                        <AuthIcon name="check" size={12} brand={false} />
                      </span>
                      Remember me
                    </label>
                    <button type="button" className="auth-link" onClick={onForgot}>
                      Forgot password?
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>

              {formMsg && (
                <div className={`auth-formmsg auth-formmsg--${formMsg.kind}`}>
                  <AuthIcon name={formMsg.kind === "error" ? "alert" : "check"} size={15} brand={false} />
                  <span>{formMsg.text}</span>
                </div>
              )}

              <button type="submit" className="auth-submit" disabled={busy}>
                <span className="auth-submit-label">
                  {status === "submitting" ? (
                    <>
                      <AuthIcon name="loader" size={17} brand={false} className="auth-spin" />
                      {mode === "login" ? "Signing in…" : "Creating account…"}
                    </>
                  ) : status === "success" ? (
                    <>
                      <AuthIcon name="check" size={17} brand={false} />
                      Welcome
                    </>
                  ) : (
                    <>
                      {mode === "login" ? "Sign in" : "Create account"}
                      <AuthIcon name="arrow-right" size={16} brand={false} className="auth-submit-arrow" />
                    </>
                  )}
                </span>
              </button>
            </form>

            <div className="auth-divider">
              <span>or continue with</span>
            </div>

            <SocialRow onProvider={() => exploreDemo()} disabled={busy} />

            <button type="button" className="auth-demo" onClick={exploreDemo} disabled={busy}>
              Explore the demo library
              <AuthIcon name="arrow-right" size={14} brand={false} />
            </button>

            <p className="auth-switch">
              {mode === "login" ? "New to Kinora?" : "Already have an account?"}{" "}
              <button type="button" className="auth-link auth-link--strong" onClick={switchMode}>
                {mode === "login" ? "Create one" : "Sign in"}
              </button>
            </p>
          </section>
        </motion.div>
      </div>

      {/* Single live region: progress is polite, errors are assertive. */}
      <div
        className="sr-only"
        role="status"
        aria-live={status === "error" ? "assertive" : "polite"}
      >
        {announce}
      </div>
    </motion.div>
  );
}
