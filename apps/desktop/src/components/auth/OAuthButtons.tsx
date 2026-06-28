// OAuth / SSO sign-in buttons, driven by the provider registry in
// lib/account/oauth.ts. On click we mint a PKCE attempt, persist it (for the
// callback's CSRF check), and hand the provider id up to the caller — which
// opens the backend's `/api/auth/oauth/{id}/start` redirect. When SSO isn't
// wired (no backend), the caller falls back to demo entry.
import AuthIcon, { type AuthIconName } from "./AuthIcon";
import { OAUTH_PROVIDERS, createAttempt, createAttemptStore, type OAuthProviderId } from "../../lib/account";

interface Props {
  /** Called with the chosen provider once an attempt is minted + persisted. */
  onProvider: (provider: OAuthProviderId) => void;
  disabled?: boolean;
  /** Where to return after sign-in (in-app route). */
  returnTo?: string;
  /** Layout: a 3-up icon row (default) or a stacked labelled list. */
  variant?: "row" | "stacked";
}

export default function OAuthButtons({ onProvider, disabled, returnTo, variant = "row" }: Props) {
  const store = createAttemptStore();

  function choose(id: OAuthProviderId) {
    // Persist the attempt so the OAuth callback can validate `state`.
    store.save(createAttempt(id, { returnTo }));
    onProvider(id);
  }

  if (variant === "stacked") {
    return (
      <div className="acct-oauth-stack">
        {OAUTH_PROVIDERS.map((p) => (
          <button
            key={p.id}
            type="button"
            className="acct-oauth-btn"
            disabled={disabled}
            onClick={() => choose(p.id)}
          >
            <AuthIcon name={p.icon as AuthIconName} size={18} />
            <span>Continue with {p.name}</span>
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="auth-social-row">
      {OAUTH_PROVIDERS.slice(0, 3).map((p) => (
        <button
          key={p.id}
          type="button"
          className="auth-social"
          disabled={disabled}
          onClick={() => choose(p.id)}
          aria-label={`Continue with ${p.name}`}
        >
          <AuthIcon name={p.icon as AuthIconName} size={18} />
        </button>
      ))}
    </div>
  );
}
