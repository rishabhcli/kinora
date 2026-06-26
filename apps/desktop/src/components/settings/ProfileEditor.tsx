import { useState } from "react";
import { GeometricAvatar } from "../Navbar";
import { Icon } from "../icons";

// Profile is account data (not an app preference), so it persists under its own
// key rather than in the settings store.
const PROFILE_KEY = "kinora.profile";

export interface Profile {
  displayName: string;
  email: string;
  bio: string;
  genre: string;
  goal: number;
}

const DEFAULTS: Profile = {
  displayName: "Reader",
  email: "you@kinora.app",
  bio: "",
  genre: "Fiction",
  goal: 50,
};

export function loadProfile(): Profile {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(PROFILE_KEY) || "{}") as Partial<Profile>) };
  } catch {
    return DEFAULTS;
  }
}

const GENRES = ["Fiction", "Non-Fiction", "Mystery", "Sci-Fi", "Biography", "Poetry", "History"];

export default function ProfileEditor({ compact = false }: { compact?: boolean }) {
  const [profile, setProfile] = useState<Profile>(loadProfile);
  const [saved, setSaved] = useState(false);

  const set = <K extends keyof Profile>(k: K, v: Profile[K]) => {
    setProfile((p) => ({ ...p, [k]: v }));
    setSaved(false);
  };

  const save = () => {
    try {
      localStorage.setItem(PROFILE_KEY, JSON.stringify(profile));
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2200);
    } catch {
      /* storage blocked */
    }
  };

  const labelCls = "block text-[11px] font-medium text-kinora-muted mb-1.5";
  const inputCls = "glass-input w-full px-3.5 py-2.5 rounded-xl text-[13px] text-kinora-text";

  return (
    <div>
      <div className="flex items-center gap-4 mb-5">
        <GeometricAvatar size={compact ? 48 : 56} />
        <div className="min-w-0">
          <p className="text-[14px] font-semibold text-kinora-text truncate">{profile.displayName}</p>
          <p className="text-[11px] text-kinora-muted truncate">{profile.email}</p>
        </div>
        <button className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-kinora-muted hover:text-kinora-text transition-colors">
          <Icon name="photo" size={14} />
          Change avatar
        </button>
      </div>

      <div className={`grid grid-cols-1 ${compact ? "" : "sm:grid-cols-2"} gap-4 mb-4`}>
        <div>
          <label htmlFor="pf-name" className={labelCls}>
            Display Name
          </label>
          <input
            id="pf-name"
            type="text"
            value={profile.displayName}
            onChange={(e) => set("displayName", e.target.value)}
            className={inputCls}
          />
        </div>
        <div>
          <label htmlFor="pf-email" className={labelCls}>
            Email
          </label>
          <input
            id="pf-email"
            type="email"
            value={profile.email}
            onChange={(e) => set("email", e.target.value)}
            className={inputCls}
          />
        </div>
      </div>

      <div className="mb-4">
        <label htmlFor="pf-bio" className={labelCls}>
          Bio
        </label>
        <textarea
          id="pf-bio"
          rows={3}
          value={profile.bio}
          placeholder="Tell us about the stories you love…"
          onChange={(e) => set("bio", e.target.value)}
          className={`${inputCls} resize-none`}
        />
      </div>

      <div className={`grid grid-cols-1 ${compact ? "" : "sm:grid-cols-2"} gap-4 mb-6`}>
        <div>
          <label htmlFor="pf-genre" className={labelCls}>
            Favorite Genre
          </label>
          <select
            id="pf-genre"
            value={profile.genre}
            onChange={(e) => set("genre", e.target.value)}
            className={inputCls}
          >
            {GENRES.map((g) => (
              <option key={g} style={{ background: "#161410" }}>
                {g}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="pf-goal" className={labelCls}>
            Reading Goal (books/year)
          </label>
          <input
            id="pf-goal"
            type="number"
            min={1}
            max={500}
            value={profile.goal}
            onChange={(e) => set("goal", Number(e.target.value))}
            className={inputCls}
          />
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={save}
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl text-[13px] font-semibold text-[#1a1512]"
          style={{ background: "#d4a44e" }}
        >
          <Icon name={saved ? "checkmark" : "checkmark.circle.fill"} size={15} weight="semibold" />
          {saved ? "Saved" : "Save Changes"}
        </button>
        <button
          onClick={() => setProfile(loadProfile())}
          className="px-5 py-2.5 rounded-xl text-[13px] font-medium text-kinora-muted hover:text-kinora-text transition-colors"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
