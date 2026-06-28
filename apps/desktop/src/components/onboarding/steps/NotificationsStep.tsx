// Onboarding · Notifications — opt into the two emails most worth getting.
// Writes to the shared account preferences store so the choice carries into the
// Preferences section.
import { useEffect, useState } from "react";
import { Toggle } from "../../account/primitives";
import { type AccountPreferences, createPreferencesStore } from "../../../lib/account";

export function NotificationsStep() {
  const [store] = useState(() => createPreferencesStore());
  const [prefs, setPrefs] = useState<AccountPreferences>(() => store.get());

  useEffect(() => store.subscribe(() => setPrefs(store.get())), [store]);

  return (
    <div className="acct-card" style={{ margin: 0 }}>
      <div className="acct-row" style={{ paddingTop: 0 }}>
        <div className="acct-row-main">
          <div className="acct-row-title">Tell me when a film is ready</div>
          <div className="acct-row-meta">A quiet note when a render finishes.</div>
        </div>
        <Toggle
          label="Render updates"
          checked={prefs.email.render}
          onChange={(v) => store.patch({ email: { render: v } })}
        />
      </div>
      <div className="acct-row">
        <div className="acct-row-main">
          <div className="acct-row-title">Weekly reading digest</div>
          <div className="acct-row-meta">A short recap of what you watched.</div>
        </div>
        <Toggle
          label="Weekly digest"
          checked={prefs.email.digest !== "off"}
          onChange={(v) => store.patch({ email: { digest: v ? "weekly" : "off" } })}
        />
      </div>
      <div className="acct-row" style={{ paddingBottom: 0 }}>
        <div className="acct-row-main">
          <div className="acct-row-title">Security alerts</div>
          <div className="acct-row-meta">New sign-ins and password changes. Recommended.</div>
        </div>
        <Toggle
          label="Security alerts"
          checked={prefs.email.security}
          onChange={(v) => store.patch({ email: { security: v } })}
        />
      </div>
    </div>
  );
}
