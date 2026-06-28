// PreferencesSection — account-level notification, privacy, and email-cadence
// controls (distinct from reading settings). Backed by the reactive
// preferences store (lib/account/preferences) and mirrored to the backend via
// the API adapter; changes are optimistic + persisted locally.
import { useEffect, useState } from "react";
import { Section, Toggle, Segmented } from "./primitives";
import {
  type AccountPreferences,
  type EmailCadence,
  type ProfileVisibility,
  createPreferencesStore,
} from "../../lib/account";
import { updatePreferences } from "../../lib/api/account";

export default function PreferencesSection() {
  const [store] = useState(() => createPreferencesStore());
  const [prefs, setPrefs] = useState<AccountPreferences>(() => store.get());

  useEffect(() => store.subscribe(() => setPrefs(store.get())), [store]);

  // Debounced backend mirror — keep it simple: fire on each change, the adapter
  // is graceful when offline.
  function persist(next: AccountPreferences) {
    void updatePreferences(next);
  }

  function patch(p: Parameters<typeof store.patch>[0]) {
    persist(store.patch(p));
  }

  return (
    <Section title="Preferences" sub="Notifications, privacy, and how often we email you.">
      <div className="acct-card">
        <h3 className="acct-card-title">Email</h3>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Product news</div>
            <div className="acct-row-meta">Features, new titles, and the occasional note.</div>
          </div>
          <Toggle label="Product news" checked={prefs.email.product} onChange={(v) => patch({ email: { product: v } })} />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Render updates</div>
            <div className="acct-row-meta">"Your film is ready" and render notices.</div>
          </div>
          <Toggle label="Render updates" checked={prefs.email.render} onChange={(v) => patch({ email: { render: v } })} />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Security alerts</div>
            <div className="acct-row-meta">New sign-ins and password changes. Recommended.</div>
          </div>
          <Toggle label="Security alerts" checked={prefs.email.security} onChange={(v) => patch({ email: { security: v } })} />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Reading digest</div>
            <div className="acct-row-meta">A recap of what you've watched.</div>
          </div>
          <Segmented<EmailCadence>
            ariaLabel="Reading digest cadence"
            value={prefs.email.digest}
            onChange={(v) => patch({ email: { digest: v } })}
            options={[
              { value: "off", label: "Off" },
              { value: "weekly", label: "Weekly" },
              { value: "monthly", label: "Monthly" },
            ]}
          />
        </div>
      </div>

      <div className="acct-card">
        <h3 className="acct-card-title">Notifications</h3>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Render complete</div>
          </div>
          <Toggle label="Render complete" checked={prefs.push.renderComplete} onChange={(v) => patch({ push: { renderComplete: v } })} />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Director replies</div>
          </div>
          <Toggle label="Director replies" checked={prefs.push.directorReplies} onChange={(v) => patch({ push: { directorReplies: v } })} />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Weekly streak</div>
          </div>
          <Toggle label="Weekly streak" checked={prefs.push.weeklyStreak} onChange={(v) => patch({ push: { weeklyStreak: v } })} />
        </div>
      </div>

      <div className="acct-card">
        <h3 className="acct-card-title">Privacy</h3>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Profile visibility</div>
            <div className="acct-row-meta">Who can see your profile and activity.</div>
          </div>
          <Segmented<ProfileVisibility>
            ariaLabel="Profile visibility"
            value={prefs.privacy.visibility}
            onChange={(v) => patch({ privacy: { visibility: v } })}
            options={[
              { value: "private", label: "Private" },
              { value: "friends", label: "Friends" },
              { value: "public", label: "Public" },
            ]}
          />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Personalized recommendations</div>
            <div className="acct-row-meta">Use your reading to tune your shelf.</div>
          </div>
          <Toggle label="Personalization" checked={prefs.privacy.personalization} onChange={(v) => patch({ privacy: { personalization: v } })} />
        </div>
        <div className="acct-row">
          <div className="acct-row-main">
            <div className="acct-row-title">Anonymous usage analytics</div>
          </div>
          <Toggle label="Analytics" checked={prefs.privacy.analytics} onChange={(v) => patch({ privacy: { analytics: v } })} />
        </div>
      </div>
    </Section>
  );
}
