// Password policy (account domain) — a richer estimator than the auth screen's
// lightweight `passwordStrength` (components/auth/validation.ts). That one is a
// quick meter for the login form; this is the *policy* used for set/reset/change
// password: an explicit requirements checklist, a guarded score, common-password
// + sequence heuristics, and confirm-match. Pure, dependency-free (no zxcvbn).

// ---- Requirements checklist ----------------------------------------------- //

export interface PasswordRequirement {
  id: "length" | "lower" | "upper" | "digit" | "symbol";
  label: string;
  met: boolean;
}

export const MIN_LENGTH = 8;
const STRONG_LENGTH = 12;

/** The live checklist the set-password UI shows beneath the field. */
export function passwordRequirements(pw: string): PasswordRequirement[] {
  return [
    { id: "length", label: `At least ${MIN_LENGTH} characters`, met: pw.length >= MIN_LENGTH },
    { id: "lower", label: "A lowercase letter", met: /[a-z]/.test(pw) },
    { id: "upper", label: "An uppercase letter", met: /[A-Z]/.test(pw) },
    { id: "digit", label: "A number", met: /\d/.test(pw) },
    { id: "symbol", label: "A symbol", met: /[^A-Za-z0-9]/.test(pw) },
  ];
}

/** Policy gate for register/reset: minimum length + at least 3 of the 4 variety
 *  classes. (Login stays lenient — that's the auth/validation module.) */
export function meetsPolicy(pw: string): boolean {
  if (pw.length < MIN_LENGTH) return false;
  const variety = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(pw)).length;
  return variety >= 3;
}

// ---- Heuristics ----------------------------------------------------------- //

// A small embedded list of the most-abused passwords + obvious bases. This is a
// hint (we don't ship a megalist); the backend can do a real breach check.
const COMMON = new Set([
  "password", "123456", "12345678", "123456789", "qwerty", "abc123", "111111",
  "letmein", "welcome", "admin", "iloveyou", "monkey", "dragon", "football",
  "kinora", "password1", "qwerty123", "000000", "1234567890", "passw0rd",
]);

/** True if the password (lowercased, trimmed) is an obviously weak/common one
 *  or just the common base + a couple trailing digits. */
export function isCommonPassword(pw: string): boolean {
  const lower = pw.toLowerCase();
  if (COMMON.has(lower)) return true;
  const stripped = lower.replace(/[0-9!@#$%^&*]+$/, "");
  return stripped.length >= 4 && COMMON.has(stripped);
}

/** Detect a long straight run of sequential or repeated characters
 *  ("abcdef", "111111", "qwerty"). `run` = max allowed run length. */
export function hasObviousSequence(pw: string, run = 4): boolean {
  if (pw.length < run) return false;
  const lower = pw.toLowerCase();
  const keyboard = "qwertyuiopasdfghjklzxcvbnm";
  let asc = 1, desc = 1, rep = 1;
  for (let i = 1; i < lower.length; i++) {
    const a = lower.charCodeAt(i - 1);
    const b = lower.charCodeAt(i);
    asc = b === a + 1 ? asc + 1 : 1;
    desc = b === a - 1 ? desc + 1 : 1;
    rep = b === a ? rep + 1 : 1;
    if (asc >= run || desc >= run || rep >= run) return true;
  }
  // keyboard-walk run
  for (let i = 0; i + run <= keyboard.length; i++) {
    if (lower.includes(keyboard.slice(i, i + run))) return true;
  }
  return false;
}

// ---- Scoring -------------------------------------------------------------- //

export interface PasswordAssessment {
  /** 0 (empty) … 4 (strong). */
  score: 0 | 1 | 2 | 3 | 4;
  label: "" | "Very weak" | "Weak" | "Fair" | "Good" | "Strong";
  meetsPolicy: boolean;
  /** A single, actionable nudge (most important first). */
  warning?: string;
  requirements: PasswordRequirement[];
}

const LABELS = ["", "Very weak", "Weak", "Fair", "Good", "Strong"] as const;

/** A guarded score: variety + length, then knocked down for common/sequenced
 *  passwords. Deterministic and pure — drives the meter + the warning line. */
export function assessPassword(pw: string): PasswordAssessment {
  const requirements = passwordRequirements(pw);
  if (!pw) {
    return { score: 0, label: "", meetsPolicy: false, requirements };
  }

  const variety = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(pw)).length;
  const lengthPoints = pw.length >= STRONG_LENGTH ? 2 : pw.length >= MIN_LENGTH ? 1 : 0;
  let raw = variety + lengthPoints; // 0..6

  let warning: string | undefined;
  if (isCommonPassword(pw)) {
    raw = Math.min(raw, 1);
    warning = "This is a commonly used password.";
  } else if (hasObviousSequence(pw)) {
    raw = Math.min(raw, 2);
    warning = "Avoid runs like “1234” or “abcd”.";
  } else if (pw.length < MIN_LENGTH) {
    warning = `Use at least ${MIN_LENGTH} characters.`;
  } else if (variety < 3) {
    warning = "Mix in upper/lowercase, numbers, or symbols.";
  }

  // Map raw 0..6 → score 1..4 (anything non-empty is at least 1).
  const score = Math.max(1, Math.min(4, Math.round((raw / 6) * 4))) as 1 | 2 | 3 | 4;
  const labelIndex = pw.length < 4 ? 1 : score + 1;
  return {
    score,
    label: LABELS[Math.min(LABELS.length - 1, labelIndex)],
    meetsPolicy: meetsPolicy(pw),
    warning,
    requirements,
  };
}

// ---- Confirm + change ----------------------------------------------------- //

/** Validate a new-password + confirm pair for set/reset. Returns a friendly
 *  error or null. */
export function validateNewPassword(pw: string, confirm: string): string | null {
  if (!pw) return "Enter a new password.";
  if (!meetsPolicy(pw)) return `Use at least ${MIN_LENGTH} characters with some variety.`;
  if (isCommonPassword(pw)) return "Choose something less common.";
  if (confirm !== pw) return "Passwords don't match.";
  return null;
}

/** For "change password": the new one must differ from the current. */
export function validatePasswordChange(
  current: string,
  next: string,
  confirm: string,
): string | null {
  if (!current) return "Enter your current password.";
  if (current === next) return "Choose a password you haven't used here.";
  return validateNewPassword(next, confirm);
}
