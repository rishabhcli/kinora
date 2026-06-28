// ProfileSection — edit display name, @handle, pronouns, and bio. Live
// validation via lib/account/profile, a monogram avatar preview, and a Save
// that persists through lib/api/account (graceful/optimistic offline).
import { useMemo, useState } from "react";
import { Section, Avatar } from "./primitives";
import Field from "../auth/Field";
import {
  type Profile,
  validateProfile,
  isProfileValid,
  normalizeHandle,
  bioRemaining,
  PROFILE_LIMITS,
} from "../../lib/account";
import { updateProfile, type ProfileUpdate } from "../../lib/api/account";

interface Props {
  profile: Profile;
  onSaved: (p: Profile) => void;
}

export default function ProfileSection({ profile, onSaved }: Props) {
  const [displayName, setDisplayName] = useState(profile.displayName);
  const [handle, setHandle] = useState(profile.handle ?? "");
  const [pronouns, setPronouns] = useState(profile.pronouns ?? "");
  const [bio, setBio] = useState(profile.bio ?? "");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const patch: ProfileUpdate = {
    displayName,
    handle: handle ? normalizeHandle(handle) : "",
    pronouns,
    bio,
  };
  const errors = validateProfile(patch);
  const dirty = useMemo(
    () =>
      displayName !== profile.displayName ||
      (handle ? normalizeHandle(handle) : "") !== (profile.handle ?? "") ||
      pronouns !== (profile.pronouns ?? "") ||
      bio !== (profile.bio ?? ""),
    [displayName, handle, pronouns, bio, profile],
  );

  const preview: Profile = { ...profile, displayName, handle: normalizeHandle(handle), avatarUrl: profile.avatarUrl };

  async function save() {
    setTouched(true);
    if (!isProfileValid(errors) || !dirty) return;
    setBusy(true);
    const next = await updateProfile(profile, patch);
    setBusy(false);
    setSaved(true);
    onSaved(next);
    setTimeout(() => setSaved(false), 2400);
  }

  return (
    <Section title="Profile" sub="How you appear across Kinora.">
      <div className="acct-card">
        <div className="acct-row" style={{ paddingTop: 0 }}>
          <div className="acct-row-main" style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <Avatar profile={preview} size={56} />
            <div>
              <div className="acct-row-title">{displayName || profile.email.split("@")[0]}</div>
              <div className="acct-row-meta">{profile.email}</div>
            </div>
          </div>
        </div>

        <Field
          id="profile-name"
          label="Display name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          onBlur={() => setTouched(true)}
          maxLength={PROFILE_LIMITS.displayName}
          placeholder="Your name"
          error={errors.displayName}
          showError={touched}
        />

        <Field
          id="profile-handle"
          label="Handle"
          value={handle}
          onChange={(e) => setHandle(e.target.value)}
          onBlur={() => setTouched(true)}
          placeholder="yourhandle"
          error={errors.handle}
          showError={touched}
        />

        <Field
          id="profile-pronouns"
          label="Pronouns (optional)"
          value={pronouns}
          onChange={(e) => setPronouns(e.target.value)}
          placeholder="they/them"
        />

        <div className="auth-field">
          <label htmlFor="profile-bio" className="auth-field-label">
            Bio
          </label>
          <textarea
            id="profile-bio"
            className="auth-input"
            style={{ height: 80, paddingTop: 10, resize: "none" }}
            value={bio}
            maxLength={PROFILE_LIMITS.bio}
            onChange={(e) => setBio(e.target.value)}
            placeholder="A line about you and the books you love."
          />
          <p className="auth-field-error" aria-hidden="true" style={{ color: "var(--auth-subtle)" }}>
            {bioRemaining(bio)} characters left
          </p>
        </div>

        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8 }}>
          <button
            type="button"
            className="acct-btn acct-btn--primary"
            disabled={busy || !dirty || !isProfileValid(errors)}
            onClick={save}
          >
            {busy ? "Saving…" : "Save changes"}
          </button>
          {saved && (
            <span className="acct-badge acct-badge--good" role="status">
              Saved
            </span>
          )}
        </div>
      </div>
    </Section>
  );
}
