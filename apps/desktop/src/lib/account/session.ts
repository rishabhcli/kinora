// Session & device management (account domain) — the model + pure derivations
// behind the "Where you're signed in" surface (kinora.md §6 security). Each
// active session is a signed token bound to a device; this module parses,
// labels, sorts, and reasons about revocation. All logic is PURE; a local
// cache of the session list lives in an injectable KV so the device list shows
// instantly (offline-first) before the backend refreshes it.
//
// The actual revoke/list network calls live in lib/api/sessions.ts; this file
// is the offline-deterministic brain it composes against.
import { type KeyValueStore, readJson, resolveStore, writeJson } from "./store";

// ---- Model ---------------------------------------------------------------- //

export type DeviceKind = "desktop" | "mobile" | "tablet" | "web" | "unknown";

/** One authenticated session, as the backend would return it. The renderer's
 *  *current* session is flagged so the UI can disable "revoke this device". */
export interface DeviceSession {
  id: string;
  /** Human label, e.g. "MacBook Pro · Chrome". Derived if absent. */
  label?: string;
  kind: DeviceKind;
  /** OS / platform string (raw UA platform or a friendly name). */
  platform?: string;
  /** App/browser client, e.g. "Kinora Desktop 0.0.1" or "Safari". */
  client?: string;
  /** Coarse location label, e.g. "San Francisco, US". Never precise. */
  location?: string;
  /** IP, masked for display. */
  ip?: string;
  /** Epoch ms of first sign-in for this session. */
  createdAt: number;
  /** Epoch ms of the most recent activity. */
  lastSeenAt: number;
  /** True for the session this renderer is using right now. */
  current?: boolean;
}

const CACHE_KEY = "kinora.account.sessions.v1";

// ---- Parsing -------------------------------------------------------------- //

const DEVICE_KINDS: DeviceKind[] = ["desktop", "mobile", "tablet", "web", "unknown"];

function asDeviceKind(v: unknown): DeviceKind {
  return typeof v === "string" && (DEVICE_KINDS as string[]).includes(v)
    ? (v as DeviceKind)
    : "unknown";
}

function asMs(v: unknown, fallback: number): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  // Tolerate ISO strings (the backend serialises timestamps as ISO).
  if (typeof v === "string") {
    const t = Date.parse(v);
    if (!Number.isNaN(t)) return t;
  }
  return fallback;
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length ? v : undefined;
}

/** Parse one backend/raw row into a DeviceSession, dropping nothing (an id is
 *  the only hard requirement). Unknown fields are ignored. */
export function parseSession(row: unknown): DeviceSession | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const id = str(r.id);
  if (!id) return null;
  const createdAt = asMs(r.createdAt ?? r.created_at, Date.now());
  return {
    id,
    label: str(r.label),
    kind: asDeviceKind(r.kind ?? r.device_kind),
    platform: str(r.platform),
    client: str(r.client),
    location: str(r.location),
    ip: str(r.ip),
    createdAt,
    lastSeenAt: asMs(r.lastSeenAt ?? r.last_seen_at ?? r.last_active, createdAt),
    current: r.current === true || r.is_current === true,
  };
}

/** Parse a list of raw rows, dropping malformed entries. */
export function parseSessions(rows: unknown): DeviceSession[] {
  if (!Array.isArray(rows)) return [];
  const out: DeviceSession[] = [];
  for (const row of rows) {
    const s = parseSession(row);
    if (s) out.push(s);
  }
  return out;
}

// ---- Device label derivation --------------------------------------------- //

const KIND_NOUN: Record<DeviceKind, string> = {
  desktop: "Desktop",
  mobile: "Phone",
  tablet: "Tablet",
  web: "Browser",
  unknown: "Device",
};

/** A display label for a session: its explicit label, else platform+client,
 *  else the device-kind noun. Never empty. */
export function sessionLabel(s: DeviceSession): string {
  if (s.label) return s.label;
  const parts = [s.platform, s.client].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  return KIND_NOUN[s.kind];
}

/** Classify a navigator UA string into a device kind. Pure (takes the string).
 *  Coarse on purpose — it only drives an icon + noun, never a security gate. */
export function deviceKindFromUA(ua: string): DeviceKind {
  const s = ua.toLowerCase();
  if (/ipad|tablet/.test(s)) return "tablet";
  if (/mobi|iphone|android.*mobile/.test(s)) return "mobile";
  if (/electron|kinora/.test(s)) return "desktop";
  if (/mozilla|safari|chrome|firefox|edg/.test(s)) return "web";
  return "unknown";
}

// ---- Ordering & queries --------------------------------------------------- //

/** Sort sessions for the device list: the current device pinned to the top,
 *  then most-recently-active first. Stable. */
export function sortSessions(sessions: DeviceSession[]): DeviceSession[] {
  return [...sessions].sort((a, b) => {
    if (Boolean(a.current) !== Boolean(b.current)) return a.current ? -1 : 1;
    return b.lastSeenAt - a.lastSeenAt;
  });
}

/** The current session, if the list flags one. */
export function currentSession(sessions: DeviceSession[]): DeviceSession | undefined {
  return sessions.find((s) => s.current);
}

/** Sessions that can be revoked (everything except the current one — you can't
 *  revoke the device you're using; that's "sign out" instead). */
export function revocableSessions(sessions: DeviceSession[]): DeviceSession[] {
  return sessions.filter((s) => !s.current);
}

/** Count of *other* devices — what "Sign out everywhere else" acts on. */
export function otherDeviceCount(sessions: DeviceSession[]): number {
  return revocableSessions(sessions).length;
}

/** A session is "stale" if it hasn't been seen in `days` days. The device list
 *  can surface these for a quick clean-up nudge. */
export function isStale(s: DeviceSession, days = 30, now: number = Date.now()): boolean {
  return now - s.lastSeenAt > days * 86_400_000;
}

// ---- Relative-time formatting (clock-free, deterministic) ----------------- //

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "just now", "5 min ago", "3 hr ago", "2 days ago", "3 weeks ago", else a
 *  date. Pure (takes `now`) so it's testable without mocking the clock. */
export function relativeTime(at: number, now: number = Date.now()): string {
  const delta = Math.max(0, now - at);
  if (delta < MINUTE) return "just now";
  if (delta < HOUR) {
    const m = Math.floor(delta / MINUTE);
    return `${m} min ago`;
  }
  if (delta < DAY) {
    const h = Math.floor(delta / HOUR);
    return `${h} hr ago`;
  }
  const days = Math.floor(delta / DAY);
  if (days < 7) return days === 1 ? "yesterday" : `${days} days ago`;
  if (days < 30) {
    const w = Math.floor(days / 7);
    return w === 1 ? "1 week ago" : `${w} weeks ago`;
  }
  if (days < 365) {
    const mo = Math.floor(days / 30);
    return mo === 1 ? "1 month ago" : `${mo} months ago`;
  }
  const y = Math.floor(days / 365);
  return y === 1 ? "1 year ago" : `${y} years ago`;
}

// ---- Apply a local revoke (optimistic) ------------------------------------ //

/** Remove a session by id (optimistic UI before the network confirms). */
export function removeSession(sessions: DeviceSession[], id: string): DeviceSession[] {
  return sessions.filter((s) => s.id !== id);
}

/** Keep only the current session (what "sign out everywhere else" yields). */
export function keepOnlyCurrent(sessions: DeviceSession[]): DeviceSession[] {
  return sessions.filter((s) => s.current);
}

// ---- Offline cache store -------------------------------------------------- //

export interface SessionCache {
  /** The cached session list (parsed, sorted). */
  list(): DeviceSession[];
  /** Replace the cache (e.g. after a backend refresh). */
  set(sessions: DeviceSession[]): void;
  /** Optimistically remove one. */
  revoke(id: string): void;
  /** Optimistically keep only the current device. */
  revokeOthers(): void;
  subscribe(fn: () => void): () => void;
}

/** A small persisted cache of the device list so the Sessions surface paints
 *  instantly. The API adapter refreshes it; the UI reads it. */
export function createSessionCache(backing?: KeyValueStore | null): SessionCache {
  const store = resolveStore(backing);
  let sessions = sortSessions(parseSessions(readJson<unknown[]>(store, CACHE_KEY, [])));
  const subs = new Set<() => void>();

  const commit = (next: DeviceSession[]) => {
    sessions = sortSessions(next);
    writeJson(store, CACHE_KEY, sessions);
    subs.forEach((fn) => fn());
  };

  return {
    list: () => sessions,
    set: (next) => commit(next),
    revoke: (id) => commit(removeSession(sessions, id)),
    revokeOthers: () => commit(keepOnlyCurrent(sessions)),
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

export const SESSION_CACHE_KEY = CACHE_KEY;
