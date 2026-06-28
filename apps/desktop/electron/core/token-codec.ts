/**
 * Auth-token encoding & validation — pure, Electron-free.
 *
 * The Electron-bound secure store encrypts with `safeStorage` and writes to
 * disk, but the *format* of the on-disk envelope, the base64 framing, and the
 * "is this a plausible token" check live here so they are unit-testable. The
 * envelope records whether the payload was OS-encrypted; if `safeStorage` was
 * unavailable at write time we mark it `plain` so a later read knows not to try
 * decryption (and can warn).
 */

export interface TokenEnvelope {
  v: 1;
  /** "enc" = safeStorage ciphertext (base64); "plain" = obfuscated fallback. */
  mode: "enc" | "plain";
  /** Base64 payload. */
  payload: string;
  /** When it was written (ms epoch) — informational. */
  ts: number;
}

/** Loose structural check that a decoded value is a usable bearer token. */
export function isPlausibleToken(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const t = value.trim();
  // Reject empty, whitespace, and absurdly long blobs; allow JWT/opaque tokens.
  return t.length >= 8 && t.length <= 8192 && !/\s/.test(t);
}

/** Build the on-disk envelope from an (already encrypted-or-not) base64 payload. */
export function makeEnvelope(payload: string, mode: TokenEnvelope["mode"], now = Date.now()): TokenEnvelope {
  return { v: 1, mode, payload, ts: now };
}

export function isEnvelope(value: unknown): value is TokenEnvelope {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as TokenEnvelope).v === 1 &&
    ((value as TokenEnvelope).mode === "enc" || (value as TokenEnvelope).mode === "plain") &&
    typeof (value as TokenEnvelope).payload === "string"
  );
}

export function parseEnvelope(text: string | null | undefined): TokenEnvelope | null {
  if (!text) return null;
  try {
    const parsed = JSON.parse(text);
    return isEnvelope(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * A reversible obfuscation used ONLY when `safeStorage` is unavailable (some
 * Linux setups without a keyring). This is NOT encryption — it merely stops the
 * token sitting in cleartext in a JSON file. The envelope is marked `plain` so
 * the security posture is explicit and auditable.
 */
export function obfuscate(token: string): string {
  return Buffer.from(token, "utf8").toString("base64");
}

export function deobfuscate(payload: string): string | null {
  try {
    return Buffer.from(payload, "base64").toString("utf8");
  } catch {
    return null;
  }
}
