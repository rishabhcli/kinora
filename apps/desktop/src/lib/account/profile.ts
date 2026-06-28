// User profile (account domain) — the model + pure validation/derivation behind
// the profile editor and the avatar shown across the app (navbar, account
// page). No network here; the API adapter (lib/api/account.ts) persists it.
// Avatar colour + initials are deterministic from the user id/name so a user
// without an uploaded image still gets a stable, pleasant monogram — the same
// trick lib/api.ts uses for book covers.

// ---- Model ---------------------------------------------------------------- //

export interface Profile {
  /** Stable user id (from the backend). */
  id: string;
  email: string;
  /** Public display name, e.g. "Ada Lovelace". May be empty (falls back to the
   *  email local-part). */
  displayName: string;
  /** Optional @handle, lowercase, used in shares. */
  handle?: string;
  /** Short bio (≤ 280). */
  bio?: string;
  /** Avatar image URL (uploaded). Absent → monogram. */
  avatarUrl?: string;
  /** Pronouns string the user opts into showing. */
  pronouns?: string;
  /** Epoch ms of account creation. */
  createdAt?: number;
}

/** An empty profile for a given id/email — what the editor starts from before a
 *  fetch resolves (so the UI never sees undefined). */
export function emptyProfile(id: string, email: string): Profile {
  return { id, email, displayName: "" };
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length ? v : undefined;
}

function asMs(v: unknown): number | undefined {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const t = Date.parse(v);
    if (!Number.isNaN(t)) return t;
  }
  return undefined;
}

/** Parse a backend user/profile row into a Profile (tolerant of snake_case). */
export function parseProfile(row: unknown): Profile | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const id = str(r.id);
  const email = str(r.email);
  if (!id || !email) return null;
  return {
    id,
    email,
    displayName: str(r.displayName ?? r.display_name ?? r.name) ?? "",
    handle: str(r.handle),
    bio: str(r.bio),
    avatarUrl: str(r.avatarUrl ?? r.avatar_url),
    pronouns: str(r.pronouns),
    createdAt: asMs(r.createdAt ?? r.created_at),
  };
}

// ---- Display derivation --------------------------------------------------- //

/** The name to show: explicit display name, else the email local-part. */
export function displayNameOf(p: Pick<Profile, "displayName" | "email">): string {
  const dn = p.displayName.trim();
  if (dn) return dn;
  const local = p.email.split("@")[0] ?? p.email;
  return local || "Reader";
}

/** Up to two initials for the monogram avatar. "Ada Lovelace" → "AL";
 *  "ada@x.com" → "A". */
export function initialsOf(p: Pick<Profile, "displayName" | "email">): string {
  const name = displayNameOf(p).trim();
  const words = name.split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return "?";
}

// A small, pleasant palette for monogram avatars — gradient + readable text.
const AVATAR_PALETTE: Array<{ from: string; to: string; text: string }> = [
  { from: "#6366f1", to: "#8b5cf6", text: "#ffffff" }, // indigo→violet
  { from: "#0ea5e9", to: "#2563eb", text: "#ffffff" }, // sky→blue
  { from: "#10b981", to: "#059669", text: "#ffffff" }, // emerald
  { from: "#f59e0b", to: "#d97706", text: "#1a1206" }, // amber
  { from: "#ef4444", to: "#b91c1c", text: "#ffffff" }, // red
  { from: "#ec4899", to: "#be185d", text: "#ffffff" }, // pink
  { from: "#14b8a6", to: "#0d9488", text: "#04201d" }, // teal
  { from: "#a855f7", to: "#7c3aed", text: "#ffffff" }, // purple
];

/** A stable avatar gradient derived from the user id (or name). Deterministic
 *  so the same user always gets the same colour everywhere. */
export function avatarColor(seed: string): { from: string; to: string; text: string; gradient: string } {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  const p = AVATAR_PALETTE[h % AVATAR_PALETTE.length];
  return { ...p, gradient: `linear-gradient(135deg, ${p.from} 0%, ${p.to} 100%)` };
}

// ---- Validation ----------------------------------------------------------- //

const HANDLE_RE = /^[a-z0-9_]{3,24}$/;
const MAX_DISPLAY = 60;
const MAX_BIO = 280;

export interface ProfileErrors {
  displayName?: string;
  handle?: string;
  bio?: string;
}

/** Validate the editable profile fields. Empty display name is allowed (we fall
 *  back to the email); an over-long one is not. */
export function validateProfile(p: Partial<Profile>): ProfileErrors {
  const errors: ProfileErrors = {};
  if (p.displayName && p.displayName.trim().length > MAX_DISPLAY) {
    errors.displayName = `Keep it under ${MAX_DISPLAY} characters.`;
  }
  if (p.handle != null && p.handle.length > 0) {
    const h = normalizeHandle(p.handle);
    if (!HANDLE_RE.test(h)) {
      errors.handle = "3–24 letters, numbers or underscores.";
    }
  }
  if (p.bio && p.bio.length > MAX_BIO) {
    errors.bio = `Bio is at most ${MAX_BIO} characters.`;
  }
  return errors;
}

export function isProfileValid(errors: ProfileErrors): boolean {
  return Object.keys(errors).length === 0;
}

/** Normalise a typed @handle: drop a leading @, lowercase, strip spaces. */
export function normalizeHandle(handle: string): string {
  return handle.trim().replace(/^@+/, "").toLowerCase().replace(/\s+/g, "");
}

/** Remaining-character budget helpers for the editor's live counters. */
export function bioRemaining(bio: string): number {
  return MAX_BIO - bio.length;
}

export const PROFILE_LIMITS = { displayName: MAX_DISPLAY, bio: MAX_BIO, handle: 24 } as const;
