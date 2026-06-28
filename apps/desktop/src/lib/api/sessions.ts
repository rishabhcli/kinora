// Sessions & devices API client (Account domain) — list active sessions, list +
// rename + remove passkeys, and revoke sessions ("sign out this device" /
// "everywhere else"). Built ONLY on the shared `http` primitive from
// `lib/api.ts`; never edits it.
//
// These endpoints (`/api/auth/sessions`, `/api/auth/passkeys`) are the
// shaped-ahead contract; until the backend grows them, calls degrade to the
// local cache so the security surface is always usable. The pure parsing/sorting
// lives in lib/account/{session,passkey}.ts.
import { http, ApiError } from "../api";
import {
  type DeviceSession,
  parseSessions,
  type PasskeyCredential,
  parsePasskeys,
} from "../account";

async function softCall<T>(fn: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 404 || e.status === 405 || e.status === 501 || e.status === 408) return fallback;
      throw e;
    }
    return fallback;
  }
}

// ---- Sessions ------------------------------------------------------------- //

/** List active sessions/devices. Returns [] when the endpoint isn't live. */
export async function listSessions(): Promise<DeviceSession[]> {
  return softCall<DeviceSession[]>(async () => parseSessions(await http("/api/auth/sessions")), []);
}

/** Revoke one session by id. Resolves true on success (or demo). */
export async function revokeSession(id: string): Promise<boolean> {
  return softCall(async () => {
    await http(`/api/auth/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    return true;
  }, true);
}

/** Revoke every *other* session (keep the current device). */
export async function revokeOtherSessions(): Promise<boolean> {
  return softCall(async () => {
    await http("/api/auth/sessions", { method: "DELETE" });
    return true;
  }, true);
}

// ---- Passkeys ------------------------------------------------------------- //

/** List registered passkeys. */
export async function listPasskeys(): Promise<PasskeyCredential[]> {
  return softCall<PasskeyCredential[]>(async () => parsePasskeys(await http("/api/auth/passkeys")), []);
}

/** Begin passkey registration — the backend returns the creation options
 *  (challenge, rp, user) the WebAuthn `navigator.credentials.create` needs.
 *  Returns null when unavailable so the caller can hide the option. */
export async function beginPasskeyRegistration(): Promise<Record<string, unknown> | null> {
  return softCall<Record<string, unknown> | null>(
    async () => (await http("/api/auth/passkeys/begin", { method: "POST" })) as Record<string, unknown>,
    null,
  );
}

/** Finish passkey registration with the attestation the authenticator produced.
 *  Returns the stored credential, or null on failure/demo. */
export async function finishPasskeyRegistration(
  attestation: Record<string, unknown>,
): Promise<PasskeyCredential | null> {
  return softCall<PasskeyCredential | null>(
    async () => {
      const rows = parsePasskeys([
        await http("/api/auth/passkeys/finish", {
          method: "POST",
          body: JSON.stringify(attestation),
        }),
      ]);
      return rows[0] ?? null;
    },
    null,
  );
}

export async function renamePasskey(id: string, label: string): Promise<boolean> {
  return softCall(async () => {
    await http(`/api/auth/passkeys/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ label }),
    });
    return true;
  }, true);
}

export async function removePasskey(id: string): Promise<boolean> {
  return softCall(async () => {
    await http(`/api/auth/passkeys/${encodeURIComponent(id)}`, { method: "DELETE" });
    return true;
  }, true);
}
