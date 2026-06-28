// Account API client (Account domain) — the typed surface for profile,
// preferences, password, MFA, and account-lifecycle calls. Built ONLY on the
// shared `http` primitive exported from `lib/api.ts` (the cross-domain seam);
// this module never edits that file.
//
// The backend today exposes `/api/auth/{register,login,me}` (see
// backend/app/api/routes/auth.py). The richer account endpoints below
// (`/api/account/...`, `/api/auth/mfa/...`) are the shaped-ahead contract; until
// they land, every method degrades gracefully — a 404/network error resolves to
// a local/demo value so the account UI is never blocked. This mirrors how
// LoginPage.enter() continues in demo mode when the backend is down.
import { http, ApiError } from "../api";
import {
  type Profile,
  parseProfile,
  type AccountPreferences,
  mergePreferences,
  type RecoveryCodeSet,
  generateRecoveryCodes,
  webRandomBytes,
  otpAuthUri,
  toBase32,
  type SecurityEvent,
  parseSecurityEvents,
} from "../account";

// ---- Small helpers -------------------------------------------------------- //

/** Run a backend call, returning `fallback` for a "not implemented yet"
 *  (404/405/501) or transport error, and rethrowing anything else (e.g. a 401
 *  the caller must handle). Keeps the account UI working offline/pre-backend. */
async function softCall<T>(fn: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 404 || e.status === 405 || e.status === 501 || e.status === 408) {
        return fallback;
      }
      throw e;
    }
    // Network/transport error → degrade.
    return fallback;
  }
}

// ---- Profile -------------------------------------------------------------- //

/** Fetch the authenticated user's profile. Falls back to the `/auth/me` shape
 *  (id+email) the current backend returns. */
export async function getProfile(): Promise<Profile | null> {
  const enriched = await softCall<Profile | null>(
    async () => parseProfile(await http("/api/account/profile")),
    null,
  );
  if (enriched) return enriched;
  // Fall back to the existing /auth/me endpoint.
  return softCall<Profile | null>(async () => parseProfile(await http("/api/auth/me")), null);
}

export interface ProfileUpdate {
  displayName?: string;
  handle?: string;
  bio?: string;
  pronouns?: string;
  avatarUrl?: string;
}

/** Persist profile changes. Returns the saved profile (or the optimistic merge
 *  when the endpoint isn't live yet). */
export async function updateProfile(current: Profile, patch: ProfileUpdate): Promise<Profile> {
  const optimistic: Profile = { ...current, ...stripUndefined<ProfileUpdate>(patch) };
  return softCall<Profile>(
    async () => {
      const body = toSnake(patch);
      const saved = parseProfile(
        await http("/api/account/profile", { method: "PATCH", body: JSON.stringify(body) }),
      );
      return saved ?? optimistic;
    },
    optimistic,
  );
}

// ---- Preferences ---------------------------------------------------------- //

export async function getPreferences(): Promise<AccountPreferences | null> {
  return softCall<AccountPreferences | null>(
    async () => mergePreferences(await http("/api/account/preferences")),
    null,
  );
}

export async function updatePreferences(prefs: AccountPreferences): Promise<AccountPreferences> {
  return softCall<AccountPreferences>(
    async () =>
      mergePreferences(
        await http("/api/account/preferences", { method: "PUT", body: JSON.stringify(prefs) }),
      ),
    prefs,
  );
}

// ---- Password & email ----------------------------------------------------- //

export interface PasswordChange {
  current_password: string;
  new_password: string;
}

/** Change the password. Returns true on success; a 401 (wrong current) rethrows
 *  for the form to surface. */
export async function changePassword(change: PasswordChange): Promise<boolean> {
  try {
    await http("/api/account/password", { method: "POST", body: JSON.stringify(change) });
    return true;
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 501)) return true; // demo
    throw e;
  }
}

/** Request a password-reset email. Always resolves (no account enumeration). */
export async function requestPasswordReset(email: string): Promise<void> {
  await softCall<void>(
    async () => {
      await http("/api/auth/password/reset", { method: "POST", body: JSON.stringify({ email }) });
    },
    undefined,
  );
}

/** Start an email-change (sends a confirmation to the new address). */
export async function requestEmailChange(newEmail: string): Promise<void> {
  await softCall<void>(
    async () => {
      await http("/api/account/email", { method: "POST", body: JSON.stringify({ email: newEmail }) });
    },
    undefined,
  );
}

// ---- Account lifecycle ---------------------------------------------------- //

/** Schedule account deletion. Returns the effective deletion date (epoch ms) if
 *  the backend reports one, else a 30-day local default. */
export async function deleteAccount(confirmEmail: string): Promise<{ deletedAt: number }> {
  const fallback = { deletedAt: Date.now() + 30 * 86_400_000 };
  return softCall(
    async () => {
      const res = await http<{ deleted_at?: string } | null>("/api/account", {
        method: "DELETE",
        body: JSON.stringify({ confirm_email: confirmEmail }),
      });
      const at = res?.deleted_at ? Date.parse(res.deleted_at) : NaN;
      return { deletedAt: Number.isNaN(at) ? fallback.deletedAt : at };
    },
    fallback,
  );
}

/** Export the user's data (returns a download URL or a job id when async). */
export async function requestDataExport(): Promise<{ url?: string; jobId?: string }> {
  return softCall<{ url?: string; jobId?: string }>(
    async () => {
      const res = await http<{ url?: string; job_id?: string }>("/api/account/export", {
        method: "POST",
      });
      return { url: res?.url, jobId: res?.job_id };
    },
    {},
  );
}

// ---- MFA / TOTP ----------------------------------------------------------- //

export interface TotpEnrollment {
  /** Base32 shared secret. */
  secret: string;
  /** otpauth:// URI for the QR. */
  otpauthUri: string;
}

/** Begin TOTP enrollment: the backend mints the secret. When it isn't live, we
 *  generate a *local demo* secret so the enrollment UI can be exercised — this
 *  is never a real second factor, just a wired-through flow for the showcase. */
export async function beginTotpEnrollment(account: string): Promise<TotpEnrollment> {
  return softCall<TotpEnrollment>(
    async () => {
      const res = await http<{ secret?: string; otpauth_uri?: string }>("/api/auth/mfa/totp/begin", {
        method: "POST",
      });
      const secret = res?.secret ?? localDemoSecret();
      return { secret, otpauthUri: res?.otpauth_uri ?? otpAuthUri({ account, secret }) };
    },
    (() => {
      const secret = localDemoSecret();
      return { secret, otpauthUri: otpAuthUri({ account, secret }) };
    })(),
  );
}

/** Confirm TOTP enrollment by submitting a code. Returns whether it verified
 *  plus the recovery codes the backend issued (or locally generated in demo). */
export async function confirmTotpEnrollment(code: string): Promise<{
  verified: boolean;
  recovery: RecoveryCodeSet;
}> {
  const demoRecovery = generateRecoveryCodes(10, webRandomBytes());
  return softCall(
    async () => {
      const res = await http<{ verified?: boolean; recovery_codes?: string[] }>(
        "/api/auth/mfa/totp/confirm",
        { method: "POST", body: JSON.stringify({ code }) },
      );
      return {
        verified: res?.verified ?? true,
        recovery: res?.recovery_codes
          ? { codes: res.recovery_codes, generatedAt: Date.now() }
          : demoRecovery,
      };
    },
    { verified: true, recovery: demoRecovery },
  );
}

/** Disable MFA (requires a fresh password or code, enforced backend-side). */
export async function disableMfa(): Promise<boolean> {
  return softCall(async () => {
    await http("/api/auth/mfa", { method: "DELETE" });
    return true;
  }, true);
}

// ---- Security activity ---------------------------------------------------- //

/** Recent security events (sign-ins, password/MFA changes). Empty when the
 *  endpoint isn't live. */
export async function listSecurityEvents(): Promise<SecurityEvent[]> {
  return softCall<SecurityEvent[]>(
    async () => parseSecurityEvents(await http("/api/account/security/events")),
    [],
  );
}

/** Regenerate recovery codes. */
export async function regenerateRecoveryCodes(): Promise<RecoveryCodeSet> {
  const demo = generateRecoveryCodes(10, webRandomBytes());
  return softCall(
    async () => {
      const res = await http<{ recovery_codes?: string[] }>("/api/auth/mfa/recovery", {
        method: "POST",
      });
      return res?.recovery_codes
        ? { codes: res.recovery_codes, generatedAt: Date.now() }
        : demo;
    },
    demo,
  );
}

// ---- internals ------------------------------------------------------------ //

function localDemoSecret(): string {
  return toBase32(webRandomBytes()(20)); // 160-bit secret, RFC 6238 recommended
}

function stripUndefined<T extends object>(o: T): Partial<T> {
  const out: Partial<T> = {};
  for (const [k, v] of Object.entries(o)) {
    if (v !== undefined) (out as Record<string, unknown>)[k] = v;
  }
  return out;
}

/** Map our camelCase patch keys to the backend's snake_case. */
function toSnake(patch: ProfileUpdate): Record<string, unknown> {
  const map: Record<keyof ProfileUpdate, string> = {
    displayName: "display_name",
    handle: "handle",
    bio: "bio",
    pronouns: "pronouns",
    avatarUrl: "avatar_url",
  };
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(patch)) {
    if (v !== undefined) out[map[k as keyof ProfileUpdate]] = v;
  }
  return out;
}
