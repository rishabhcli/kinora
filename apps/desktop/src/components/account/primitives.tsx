// Small presentational primitives shared across the account surface: a monogram
// avatar (deterministic colour from lib/account/profile), an accessible toggle
// switch, and a segmented control. Kept tiny + dependency-free so the section
// components stay readable.
import type { ReactNode } from "react";
import { avatarColor, initialsOf, type Profile } from "../../lib/account";

// ---- Avatar --------------------------------------------------------------- //

export function Avatar({
  profile,
  size = 40,
}: {
  profile: Pick<Profile, "id" | "displayName" | "email" | "avatarUrl">;
  size?: number;
}) {
  const color = avatarColor(profile.id || profile.email);
  const initials = initialsOf(profile);
  return (
    <span
      className="acct-avatar"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.4),
        background: profile.avatarUrl ? undefined : color.gradient,
        color: color.text,
      }}
      aria-hidden="true"
    >
      {profile.avatarUrl ? <img src={profile.avatarUrl} alt="" /> : initials}
    </span>
  );
}

// ---- Toggle switch -------------------------------------------------------- //

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className="acct-switch"
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

// ---- Segmented control ---------------------------------------------------- //

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (next: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="acct-seg" role="tablist" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={o.value === value}
          className={`acct-seg-btn${o.value === value ? " is-active" : ""}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ---- Section scaffold ----------------------------------------------------- //

export function Section({
  title,
  sub,
  children,
}: {
  title: string;
  sub?: string;
  children: ReactNode;
}) {
  return (
    <section className="acct-section">
      <h2 className="acct-section-title">{title}</h2>
      {sub && <p className="acct-section-sub">{sub}</p>}
      {children}
    </section>
  );
}
