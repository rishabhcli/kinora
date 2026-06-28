// RecentActivityCard — "Recent security activity": a compact list of sign-ins
// and account changes from the audit log (lib/account/audit), with alerting
// events called out. Reads from the graceful adapter; renders nothing extra
// when there's no history.
import { useEffect, useState } from "react";
import { LogIn, LogOut, KeyRound, ShieldAlert, ShieldCheck, Mail, Smartphone, Activity } from "lucide-react";
import {
  type SecurityEvent,
  type SecurityEventKind,
  securityEventLabel,
  isAlerting,
  recentAlertCount,
  relativeTime,
} from "../../lib/account";
import { listSecurityEvents } from "../../lib/api/account";

const ICONS: Record<SecurityEventKind, typeof LogIn> = {
  login: LogIn,
  login_failed: ShieldAlert,
  logout: LogOut,
  password_changed: KeyRound,
  mfa_enabled: ShieldCheck,
  mfa_disabled: ShieldAlert,
  passkey_added: KeyRound,
  passkey_removed: KeyRound,
  email_changed: Mail,
  new_device: Smartphone,
  recovery_used: ShieldAlert,
  unknown: Activity,
};

export default function RecentActivityCard() {
  const [events, setEvents] = useState<SecurityEvent[]>([]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const list = await listSecurityEvents();
      if (alive) setEvents(list);
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (events.length === 0) return null;

  const alerts = recentAlertCount(events);

  return (
    <div className="acct-card">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <h3 className="acct-card-title">Recent activity</h3>
        {alerts > 0 && (
          <span className="acct-badge acct-badge--warn">
            {alerts} to review
          </span>
        )}
      </div>
      <div style={{ marginTop: 6 }}>
        {events.slice(0, 8).map((e) => {
          const Icon = ICONS[e.kind];
          return (
            <div className="acct-row" key={e.id}>
              <div className="acct-row-main" style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <Icon
                  size={17}
                  strokeWidth={1.6}
                  aria-hidden="true"
                  style={{ color: isAlerting(e) ? "var(--auth-danger)" : "var(--auth-subtle)" }}
                />
                <div>
                  <div className="acct-row-title">{securityEventLabel(e)}</div>
                  <div className="acct-row-meta">
                    {[e.device, e.location, relativeTime(e.at)].filter(Boolean).join(" · ")}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
