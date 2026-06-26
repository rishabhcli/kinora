// The three social sign-in buttons. Shape/skin is Agent 8's button look (the
// .auth-social class); this owns layout, labels and the <Icon> usage.
import AuthIcon, { type AuthIconName } from "./AuthIcon";

const PROVIDERS: { name: string; icon: AuthIconName }[] = [
  { name: "Google", icon: "google" },
  { name: "Apple", icon: "apple" },
  { name: "GitHub", icon: "github" },
];

export default function SocialRow({
  onProvider,
  disabled,
}: {
  onProvider: (name: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="auth-social-row">
      {PROVIDERS.map((p) => (
        <button
          key={p.name}
          type="button"
          className="auth-social"
          disabled={disabled}
          onClick={() => onProvider(p.name)}
          aria-label={`Continue with ${p.name}`}
        >
          <AuthIcon name={p.icon} size={18} />
        </button>
      ))}
    </div>
  );
}
