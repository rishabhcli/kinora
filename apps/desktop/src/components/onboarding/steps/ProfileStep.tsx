// Onboarding · Profile — capture a display name (optional). Persists through the
// account adapter so the chosen name is saved; the live monogram previews it.
import { useState } from "react";
import Field from "../../auth/Field";
import { Avatar } from "../../account/primitives";
import { emptyProfile, validateProfile } from "../../../lib/account";
import { updateProfile } from "../../../lib/api/account";

export function ProfileStep({ email = "you@kinora.local" }: { email?: string }) {
  const [name, setName] = useState("");
  const base = emptyProfile("me", email);
  const preview = { ...base, displayName: name };
  const error = validateProfile({ displayName: name }).displayName;

  // Persist on blur — non-blocking; the flow advances regardless.
  function persist() {
    if (name.trim() && !error) void updateProfile(base, { displayName: name.trim() });
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
        <Avatar profile={preview} size={56} />
        <div>
          <div className="acct-row-title">{name || email.split("@")[0]}</div>
          <div className="acct-row-meta">{email}</div>
        </div>
      </div>
      <Field
        id="onb-name"
        label="What should we call you?"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={persist}
        placeholder="Your name"
        error={error}
        showError={Boolean(error)}
        maxLength={60}
      />
    </div>
  );
}
