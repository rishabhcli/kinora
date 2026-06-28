// Security activity log (account domain) — the model + pure derivations behind
// "Recent security activity" in the security panel (sign-ins, password changes,
// MFA changes, new devices). The backend is authoritative; this parses its rows,
// classifies them for an icon/label, groups them by day, and flags anything that
// warrants attention (a failed sign-in, a sign-in from a new location). Pure +
// DOM-free.

export type SecurityEventKind =
  | "login"
  | "login_failed"
  | "logout"
  | "password_changed"
  | "mfa_enabled"
  | "mfa_disabled"
  | "passkey_added"
  | "passkey_removed"
  | "email_changed"
  | "new_device"
  | "recovery_used"
  | "unknown";

export interface SecurityEvent {
  id: string;
  kind: SecurityEventKind;
  /** Epoch ms. */
  at: number;
  /** Coarse location label, if known. */
  location?: string;
  /** Device/client label. */
  device?: string;
  ip?: string;
}

const KINDS = new Set<SecurityEventKind>([
  "login", "login_failed", "logout", "password_changed", "mfa_enabled",
  "mfa_disabled", "passkey_added", "passkey_removed", "email_changed",
  "new_device", "recovery_used", "unknown",
]);

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length ? v : undefined;
}
function asMs(v: unknown, fallback: number): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const t = Date.parse(v);
    if (!Number.isNaN(t)) return t;
  }
  return fallback;
}

export function parseSecurityEvent(row: unknown): SecurityEvent | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const id = str(r.id);
  if (!id) return null;
  const kindRaw = str(r.kind ?? r.type ?? r.event) ?? "unknown";
  const kind = KINDS.has(kindRaw as SecurityEventKind) ? (kindRaw as SecurityEventKind) : "unknown";
  return {
    id,
    kind,
    at: asMs(r.at ?? r.created_at ?? r.timestamp, Date.now()),
    location: str(r.location),
    device: str(r.device ?? r.client),
    ip: str(r.ip),
  };
}

export function parseSecurityEvents(rows: unknown): SecurityEvent[] {
  if (!Array.isArray(rows)) return [];
  return rows
    .map(parseSecurityEvent)
    .filter((e): e is SecurityEvent => e !== null)
    .sort((a, b) => b.at - a.at);
}

const LABELS: Record<SecurityEventKind, string> = {
  login: "Signed in",
  login_failed: "Failed sign-in attempt",
  logout: "Signed out",
  password_changed: "Password changed",
  mfa_enabled: "Two-factor enabled",
  mfa_disabled: "Two-factor disabled",
  passkey_added: "Passkey added",
  passkey_removed: "Passkey removed",
  email_changed: "Email changed",
  new_device: "New device signed in",
  recovery_used: "Recovery code used",
  unknown: "Account activity",
};

export function securityEventLabel(e: SecurityEvent): string {
  return LABELS[e.kind];
}

/** Events that should draw the reader's eye (potential compromise signals). */
const ALERTING = new Set<SecurityEventKind>(["login_failed", "new_device", "recovery_used", "mfa_disabled"]);

export function isAlerting(e: SecurityEvent): boolean {
  return ALERTING.has(e.kind);
}

/** Count of alerting events within the last `days`. Drives a "review your
 *  activity" nudge. */
export function recentAlertCount(events: SecurityEvent[], days = 30, now: number = Date.now()): number {
  const cutoff = now - days * 86_400_000;
  return events.filter((e) => isAlerting(e) && e.at >= cutoff).length;
}

export interface DayGroup {
  /** Epoch-day index (UTC). */
  day: number;
  events: SecurityEvent[];
}

/** Group events by UTC day, newest day first, events within a day newest-first. */
export function groupByDay(events: SecurityEvent[]): DayGroup[] {
  const byDay = new Map<number, SecurityEvent[]>();
  for (const e of events) {
    const day = Math.floor(e.at / 86_400_000);
    const arr = byDay.get(day) ?? [];
    arr.push(e);
    byDay.set(day, arr);
  }
  return [...byDay.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([day, list]) => ({ day, events: list.sort((a, b) => b.at - a.at) }));
}
