// Pure, framework-free validation for the auth form. Friendly copy (the form
// announces these via an aria-live region), and login is deliberately lenient so
// returning readers are never nagged about a password they already have.
// Tested by apps/desktop/tests/auth/validation.test.ts (node --test).

export type AuthMode = "login" | "register";

// Pragmatic email shape: local-part @ label(.label)+ with a 2+ char TLD. Not
// RFC-exhaustive on purpose — it should pass real addresses and reject obvious
// typos, not litigate edge cases the backend will re-check anyway.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

/** @returns a friendly error string, or null when the email is acceptable. */
export function validateEmail(value: string): string | null {
  const email = value.trim();
  if (!email) return "Enter your email address.";
  if (!EMAIL_RE.test(email)) return "Enter a valid email address.";
  return null;
}

/** @returns a friendly error string, or null. Register enforces a minimum
 *  length; login only requires a non-empty value. */
export function validatePassword(value: string, mode: AuthMode): string | null {
  if (!value) return "Enter your password.";
  if (mode === "register" && value.length < 8) return "Use at least 8 characters.";
  return null;
}

export interface PasswordStrength {
  /** 0 (none) … 4 (strong) */
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
}

const STRENGTH_LABELS = ["", "Weak", "Fair", "Good", "Strong"] as const;

function labelFor(score: number): string {
  const i = Math.max(0, Math.min(4, Math.round(score)));
  return STRENGTH_LABELS[i];
}

/** A cheap, deterministic strength estimate driven by length + character
 *  variety. Pure (no zxcvbn dependency) — it's a hint for the meter, not a gate. */
export function passwordStrength(value: string): PasswordStrength {
  if (!value) return { score: 0, label: "" };

  let variety = 0;
  if (/[a-z]/.test(value)) variety++;
  if (/[A-Z]/.test(value)) variety++;
  if (/\d/.test(value)) variety++;
  if (/[^A-Za-z0-9]/.test(value)) variety++;

  const lengthPoints = value.length >= 12 ? 2 : value.length >= 8 ? 1 : 0;

  // Variety (0..4) dominates; length nudges. Clamp to 1..4 for any non-empty pw.
  let raw = variety + lengthPoints;
  if (value.length < 6) raw = Math.min(raw, 1); // very short can't be more than Weak
  const score = Math.max(1, Math.min(4, raw)) as 1 | 2 | 3 | 4;

  return { score, label: labelFor(score) };
}

// Expose the label map for callers (and tests) that need it without a password.
passwordStrength.labelFor = labelFor;
