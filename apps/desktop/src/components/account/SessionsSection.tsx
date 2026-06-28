// SessionsSection — "Where you're signed in". Lists active devices/sessions
// (offline-first from the cache, refreshed from the backend), with per-device
// revoke and a "sign out everywhere else" action. Uses the pure session model
// (lib/account/session) + the API adapter (lib/api/sessions).
import { useEffect, useState } from "react";
import { Monitor, Smartphone, Tablet, Globe, HelpCircle } from "lucide-react";
import { Section } from "./primitives";
import {
  type DeviceSession,
  type DeviceKind,
  sessionLabel,
  relativeTime,
  otherDeviceCount,
  createSessionCache,
} from "../../lib/account";
import { listSessions, revokeSession, revokeOtherSessions } from "../../lib/api/sessions";

const KIND_ICON: Record<DeviceKind, typeof Monitor> = {
  desktop: Monitor,
  mobile: Smartphone,
  tablet: Tablet,
  web: Globe,
  unknown: HelpCircle,
};

export default function SessionsSection() {
  const [cache] = useState(() => createSessionCache());
  const [sessions, setSessions] = useState<DeviceSession[]>(() => cache.list());
  const [busyId, setBusyId] = useState<string | null>(null);
  const [revokingAll, setRevokingAll] = useState(false);

  useEffect(() => {
    const off = cache.subscribe(() => setSessions(cache.list()));
    let alive = true;
    void (async () => {
      const fresh = await listSessions();
      if (alive && fresh.length) cache.set(fresh);
    })();
    return () => {
      alive = false;
      off();
    };
  }, [cache]);

  async function revoke(id: string) {
    setBusyId(id);
    cache.revoke(id); // optimistic
    await revokeSession(id);
    setBusyId(null);
  }

  async function revokeAll() {
    setRevokingAll(true);
    cache.revokeOthers(); // optimistic
    await revokeOtherSessions();
    setRevokingAll(false);
  }

  const others = otherDeviceCount(sessions);

  return (
    <Section title="Devices & sessions" sub="Manage where your account is signed in.">
      <div className="acct-card">
        {sessions.length === 0 ? (
          <p className="acct-card-desc">No other active sessions.</p>
        ) : (
          sessions.map((s) => {
            const Icon = KIND_ICON[s.kind];
            return (
              <div className="acct-row" key={s.id}>
                <div className="acct-row-main" style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <Icon size={20} strokeWidth={1.6} aria-hidden="true" style={{ color: "var(--auth-subtle)" }} />
                  <div>
                    <div className="acct-row-title">
                      {sessionLabel(s)}{" "}
                      {s.current && (
                        <span className="acct-badge acct-badge--current">This device</span>
                      )}
                    </div>
                    <div className="acct-row-meta">
                      {[s.location, `Active ${relativeTime(s.lastSeenAt)}`].filter(Boolean).join(" · ")}
                    </div>
                  </div>
                </div>
                {!s.current && (
                  <button
                    type="button"
                    className="acct-btn acct-btn--danger"
                    disabled={busyId === s.id}
                    onClick={() => revoke(s.id)}
                  >
                    {busyId === s.id ? "Signing out…" : "Sign out"}
                  </button>
                )}
              </div>
            );
          })
        )}
      </div>

      {others > 0 && (
        <button type="button" className="acct-btn acct-btn--danger" disabled={revokingAll} onClick={revokeAll}>
          {revokingAll ? "Signing out…" : `Sign out everywhere else (${others})`}
        </button>
      )}
    </Section>
  );
}
