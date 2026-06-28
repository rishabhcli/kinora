// AccountPage — the account-management surface shell. A left rail of sections
// (Profile · Security · Devices · Plan · Preferences) and the active panel.
// Loads the profile once (graceful) and passes it to ProfileSection; the other
// sections own their own data loads. Imports its CSS directly (the SettingsPage
// precedent) so the partial never touches the shared styles/index.css.
import { useEffect, useState } from "react";
import { User, ShieldCheck, MonitorSmartphone, CreditCard, SlidersHorizontal } from "lucide-react";
import "./account.css";
import { Avatar } from "./primitives";
import { type Profile, emptyProfile, displayNameOf } from "../../lib/account";
import { getProfile } from "../../lib/api/account";
import ProfileSection from "./ProfileSection";
import SecuritySection from "./SecuritySection";
import SessionsSection from "./SessionsSection";
import BillingSection from "./BillingSection";
import PreferencesSection from "./PreferencesSection";

type TabId = "profile" | "security" | "sessions" | "billing" | "preferences";

const TABS: { id: TabId; label: string; icon: typeof User }[] = [
  { id: "profile", label: "Profile", icon: User },
  { id: "security", label: "Security", icon: ShieldCheck },
  { id: "sessions", label: "Devices", icon: MonitorSmartphone },
  { id: "billing", label: "Plan & billing", icon: CreditCard },
  { id: "preferences", label: "Preferences", icon: SlidersHorizontal },
];

interface Props {
  /** Optional known email (e.g. from the auth session) for an instant header. */
  email?: string;
  initialTab?: TabId;
  /** Called once the user schedules account deletion (host signs out). */
  onAccountDeleted?: () => void;
}

export default function AccountPage({ email = "you@kinora.local", initialTab = "profile", onAccountDeleted }: Props) {
  const [tab, setTab] = useState<TabId>(initialTab);
  const [profile, setProfile] = useState<Profile>(() => emptyProfile("me", email));

  useEffect(() => {
    let alive = true;
    void (async () => {
      const p = await getProfile();
      if (alive && p) setProfile(p);
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className="acct-page">
      <nav className="acct-nav" aria-label="Account settings">
        <div className="acct-nav-head">
          <Avatar profile={profile} size={38} />
          <div style={{ minWidth: 0 }}>
            <div className="acct-row-title" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {displayNameOf(profile)}
            </div>
            <div className="acct-row-meta" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {profile.email}
            </div>
          </div>
        </div>
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.id}
              type="button"
              className={`acct-nav-item${tab === t.id ? " is-active" : ""}`}
              aria-current={tab === t.id ? "page" : undefined}
              onClick={() => setTab(t.id)}
            >
              <Icon size={17} strokeWidth={1.7} />
              {t.label}
            </button>
          );
        })}
      </nav>

      <main className="acct-main">
        {tab === "profile" && <ProfileSection profile={profile} onSaved={setProfile} />}
        {tab === "security" && <SecuritySection email={profile.email} onAccountDeleted={onAccountDeleted} />}
        {tab === "sessions" && <SessionsSection />}
        {tab === "billing" && <BillingSection />}
        {tab === "preferences" && <PreferencesSection />}
      </main>
    </div>
  );
}
