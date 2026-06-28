// Multi-factor auth (account domain) — the model + pure logic behind TOTP
// enrollment, recovery codes, and the MFA challenge step (kinora.md §6). This
// module does NO cryptography: the backend mints the shared secret and verifies
// codes. Here we model the *enrollment state machine*, format the otpauth:// URI
// (for the QR + manual-entry fallback), validate code/secret *shapes*, and
// generate display-grouped recovery codes from an injectable RNG.
//
// Keeping it crypto-free keeps it pure + deterministic in tests, and matches
// the project's "thin client, smart backend" split.
import { type RandomBytes, insecureRandomBytes } from "./store";

// ---- TOTP secret (base32, RFC 4648 alphabet) ------------------------------ //

const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
const BASE32_RE = /^[A-Z2-7]+=*$/;

/** Encode bytes to unpadded base32 (RFC 4648). Used to render a secret for the
 *  manual-entry fallback when a generated secret is shown locally. */
export function toBase32(bytes: Uint8Array): string {
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += BASE32_ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += BASE32_ALPHABET[(value << (5 - bits)) & 31];
  return out;
}

/** Validate a TOTP secret shape: base32, non-trivially long. The backend is
 *  authoritative; this only catches a fat-fingered manual entry early. */
export function isValidSecret(secret: string): boolean {
  const s = secret.replace(/\s+/g, "").toUpperCase();
  return s.length >= 16 && BASE32_RE.test(s);
}

/** Group a base32 secret into 4-char chunks for legible manual entry. */
export function formatSecret(secret: string): string {
  return (secret.replace(/\s+/g, "").toUpperCase().match(/.{1,4}/g) ?? []).join(" ");
}

// ---- otpauth:// provisioning URI ------------------------------------------ //

export interface OtpAuthParams {
  /** The account label, typically the user's email. */
  account: string;
  /** Base32 shared secret (from the backend). */
  secret: string;
  /** Issuer shown in the authenticator app. */
  issuer?: string;
  digits?: 6 | 8;
  period?: number; // seconds
  algorithm?: "SHA1" | "SHA256" | "SHA512";
}

/** Build the otpauth://totp/ URI an authenticator app reads from the QR code
 *  (or that the user types in). Follows the Key Uri Format. */
export function otpAuthUri(p: OtpAuthParams): string {
  const issuer = p.issuer ?? "Kinora";
  const label = `${issuer}:${p.account}`;
  const params = new URLSearchParams({
    secret: p.secret.replace(/\s+/g, "").toUpperCase(),
    issuer,
    digits: String(p.digits ?? 6),
    period: String(p.period ?? 30),
    algorithm: p.algorithm ?? "SHA1",
  });
  return `otpauth://totp/${encodeURIComponent(label)}?${params.toString()}`;
}

// ---- TOTP code validation (shape only) ------------------------------------ //

/** Normalise a typed code: strip spaces/dashes. Authenticator apps often
 *  display "123 456". */
export function normalizeCode(code: string): string {
  return code.replace(/[\s-]/g, "");
}

/** A 6-or-8-digit code shape. Verification is the backend's job. */
export function isValidCodeShape(code: string, digits: 6 | 8 = 6): boolean {
  const c = normalizeCode(code);
  return new RegExp(`^\\d{${digits}}$`).test(c);
}

// ---- Recovery (backup) codes ---------------------------------------------- //

export interface RecoveryCodeSet {
  codes: string[];
  /** Epoch ms generated. */
  generatedAt: number;
}

const RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no I/O/0/1 ambiguity

/** Generate `count` recovery codes of the form "XXXX-XXXX" from an injectable
 *  byte source. Each code is 8 chars of the unambiguous alphabet. */
export function generateRecoveryCodes(
  count = 10,
  rand: RandomBytes = insecureRandomBytes,
  now: number = Date.now(),
): RecoveryCodeSet {
  const codes: string[] = [];
  for (let i = 0; i < count; i++) {
    const bytes = rand(8);
    let s = "";
    for (const b of bytes) s += RECOVERY_ALPHABET[b % RECOVERY_ALPHABET.length];
    codes.push(`${s.slice(0, 4)}-${s.slice(4, 8)}`);
  }
  return { codes, generatedAt: now };
}

/** Normalise a recovery code for comparison: upper-case, hyphenated 4-4. */
export function normalizeRecoveryCode(code: string): string {
  const s = code.replace(/[\s-]/g, "").toUpperCase();
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 8)}` : s;
}

export function isValidRecoveryCodeShape(code: string): boolean {
  return /^[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(normalizeRecoveryCode(code));
}

/** Render recovery codes as the copy/download blob shown after enrollment. */
export function recoveryCodesText(set: RecoveryCodeSet, issuer = "Kinora"): string {
  const header = `${issuer} recovery codes — keep these somewhere safe.\n` +
    `Each code can be used once if you lose your authenticator.\n\n`;
  return header + set.codes.join("\n") + "\n";
}

// ---- Enrollment state machine --------------------------------------------- //

/** The MFA enrollment flow: pick a method → (TOTP) scan the secret + confirm a
 *  code → store recovery codes → done. Backup/recovery is shared by both. */
export type MfaMethod = "totp" | "passkey";

export type MfaEnrollStep =
  | "idle"
  | "method" // choose TOTP vs passkey
  | "scan" // TOTP: show QR + secret
  | "verify" // TOTP: enter a code to confirm
  | "recovery" // show recovery codes (must acknowledge)
  | "done";

export interface MfaEnrollState {
  step: MfaEnrollStep;
  method: MfaMethod | null;
  /** Whether the verify code was accepted (advances scan→verify→recovery). */
  verified: boolean;
  /** Whether the user acknowledged saving recovery codes. */
  recoverySaved: boolean;
}

export const initialEnrollState: MfaEnrollState = {
  step: "idle",
  method: null,
  verified: false,
  recoverySaved: false,
};

export type MfaEnrollEvent =
  | { type: "start" }
  | { type: "choose"; method: MfaMethod }
  | { type: "secretShown" } // QR rendered, advance scan→verify
  | { type: "verified" }
  | { type: "recoverySaved" }
  | { type: "back" }
  | { type: "cancel" };

/** Pure reducer for the enrollment flow. The component dispatches events; the
 *  network calls (mint secret, confirm code) happen in the API adapter and feed
 *  `verified`/`secretShown` back in. */
export function enrollReducer(state: MfaEnrollState, event: MfaEnrollEvent): MfaEnrollState {
  switch (event.type) {
    case "start":
      return { ...initialEnrollState, step: "method" };
    case "choose":
      // Passkey enrollment hands off to the WebAuthn flow (no scan/verify here).
      return event.method === "passkey"
        ? { ...state, method: "passkey", step: "recovery" }
        : { ...state, method: "totp", step: "scan" };
    case "secretShown":
      return state.step === "scan" ? { ...state, step: "verify" } : state;
    case "verified":
      return state.step === "verify"
        ? { ...state, verified: true, step: "recovery" }
        : state;
    case "recoverySaved":
      return state.step === "recovery"
        ? { ...state, recoverySaved: true, step: "done" }
        : state;
    case "back": {
      const order: MfaEnrollStep[] = ["method", "scan", "verify", "recovery", "done"];
      const i = order.indexOf(state.step);
      if (i <= 0) return { ...initialEnrollState, step: "method" };
      return { ...state, step: order[i - 1] };
    }
    case "cancel":
      return initialEnrollState;
    default:
      return state;
  }
}

/** Progress fraction (0..1) through the active method's steps — drives a
 *  stepper indicator. */
export function enrollProgress(state: MfaEnrollState): number {
  if (state.method === "passkey") {
    const steps: MfaEnrollStep[] = ["method", "recovery", "done"];
    const i = steps.indexOf(state.step);
    return i < 0 ? 0 : i / (steps.length - 1);
  }
  const steps: MfaEnrollStep[] = ["method", "scan", "verify", "recovery", "done"];
  const i = steps.indexOf(state.step);
  return i < 0 ? 0 : i / (steps.length - 1);
}
