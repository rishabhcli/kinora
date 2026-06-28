// LoginPanel — the complete card-side auth experience, composing the auth forms
// with the useAuth controller and the OAuth/passkey/MFA/forgot sub-flows. It's a
// drop-in for the right pane of LoginPage (or the account-gate), independent of
// the surrounding cinematic backdrop. State-machine driven: sign-in / sign-up /
// mfa / forgot views, all demo-safe.
//
// This does NOT replace LoginPage.tsx (round-1 may own the page shell + routing).
// It's an additive, self-contained panel the page can adopt incrementally.
import { useState } from "react";
import { useAuthController } from "./useAuth";
import SignInForm from "./SignInForm";
import SignUpForm from "./SignUpForm";
import MfaChallenge from "./MfaChallenge";
import ForgotPassword from "./ForgotPassword";
import OAuthButtons from "./OAuthButtons";
import PasskeyButton from "./PasskeyButton";
import AuthIcon from "./AuthIcon";
import type { OAuthProviderId } from "../../lib/account";
import "../account/account.css";

type View = "signin" | "signup" | "forgot";

interface Props {
  /** Called once the user is authenticated (or chooses demo). */
  onEnter: () => void;
  /** Start on sign-up. */
  initialView?: View;
}

export default function LoginPanel({ onEnter, initialView = "signin" }: Props) {
  const auth = useAuthController();
  const [view, setView] = useState<View>(initialView);
  const [lastEmail, setLastEmail] = useState("");

  // When the controller reaches `authenticated`, cross into the app.
  if (auth.status === "authenticated") {
    // defer to a microtask so we don't setState during render of the parent
    queueMicrotask(onEnter);
  }

  const busy = auth.busy;

  // MFA challenge takes over the card.
  if (auth.status === "mfa_required") {
    return (
      <div className="auth-card-body">
        <Header title="Two-factor authentication" sub="One more step to keep your account safe." />
        <MfaChallenge
          busy={busy}
          error={auth.error}
          onSubmit={(code) => void auth.submitMfa(code)}
          onCancel={() => {
            auth.clearError();
            auth.signOut();
            setView("signin");
          }}
        />
      </div>
    );
  }

  if (view === "forgot") {
    return (
      <div className="auth-card-body">
        <Header title="Reset password" sub="We'll email you a link." />
        <ForgotPassword initialEmail={lastEmail} onBack={() => setView("signin")} />
      </div>
    );
  }

  const isSignup = view === "signup";

  return (
    <div className="auth-card-body">
      <Header
        title={isSignup ? "Create your account" : "Welcome back"}
        sub={isSignup ? "Start watching books as films." : "Sign in to your library."}
      />

      {isSignup ? (
        <SignUpForm
          busy={busy}
          error={auth.error}
          initialEmail={lastEmail}
          onSubmit={(email, pw) => {
            setLastEmail(email);
            void auth.signUp(email, pw);
          }}
        />
      ) : (
        <SignInForm
          busy={busy}
          error={auth.error}
          initialEmail={lastEmail}
          onForgot={() => setView("forgot")}
          onSubmit={(email, pw) => {
            setLastEmail(email);
            void auth.signIn(email, pw);
          }}
        />
      )}

      <div className="auth-divider">or</div>

      <OAuthButtons disabled={busy} onProvider={(p: OAuthProviderId) => void p /* redirect handled by host */} />

      <div style={{ marginTop: 10 }}>
        <PasskeyButton disabled={busy} onUse={() => auth.enterDemo()} />
      </div>

      <button type="button" className="auth-demo" onClick={() => auth.enterDemo()}>
        Explore the demo library
        <AuthIcon name="arrow-right" size={14} brand={false} />
      </button>

      <p className="auth-switch">
        {isSignup ? "Already have an account? " : "New to Kinora? "}
        <button
          type="button"
          className="auth-link auth-link--strong"
          onClick={() => {
            auth.clearError();
            setView(isSignup ? "signin" : "signup");
          }}
        >
          {isSignup ? "Sign in" : "Create one"}
        </button>
      </p>
    </div>
  );
}

function Header({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="auth-card-head">
      <h2 className="auth-card-title">{title}</h2>
      <p className="auth-card-sub">{sub}</p>
    </div>
  );
}
