// Account preferences (account domain) — notification, privacy, and email-cadence
// settings, distinct from the *reading* settings owned by lib/settings.ts. Same
// shape as that module: a typed defaults object, a tolerant merge that drops
// unknown/garbage keys, and an injectable-store-backed reactive container.
// Pure + DOM-free; the API adapter mirrors it to the backend when one exists.
import { type KeyValueStore, readJson, resolveStore, writeJson } from "./store";

// ---- Schema --------------------------------------------------------------- //

export type EmailCadence = "off" | "weekly" | "monthly";
export type ProfileVisibility = "private" | "friends" | "public";

export interface AccountPreferences {
  /** Email notification toggles. */
  email: {
    /** Product news + feature announcements. */
    product: boolean;
    /** "Your film is ready" + render notices. */
    render: boolean;
    /** Security alerts (new sign-in, password change) — recommended on. */
    security: boolean;
    /** Periodic reading-recap digest cadence. */
    digest: EmailCadence;
  };
  /** In-app push/desktop notification toggles. */
  push: {
    renderComplete: boolean;
    directorReplies: boolean;
    weeklyStreak: boolean;
  };
  /** Privacy controls. */
  privacy: {
    visibility: ProfileVisibility;
    /** Allow Kinora to use reading data to tune recommendations. */
    personalization: boolean;
    /** Share anonymous usage analytics. */
    analytics: boolean;
    /** Show reading activity on a shared profile. */
    showActivity: boolean;
  };
}

export const DEFAULT_PREFERENCES: AccountPreferences = {
  email: { product: true, render: true, security: true, digest: "weekly" },
  push: { renderComplete: true, directorReplies: true, weeklyStreak: false },
  privacy: {
    visibility: "private",
    personalization: true,
    analytics: true,
    showActivity: false,
  },
};

const STORAGE_KEY = "kinora.account.preferences.v1";

// ---- Tolerant merge ------------------------------------------------------- //

function bool(v: unknown, fallback: boolean): boolean {
  return typeof v === "boolean" ? v : fallback;
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[], fallback: T): T {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : fallback;
}

/** Merge a partial/untrusted blob over the defaults, coercing each field. Never
 *  throws; unknown keys are ignored. Deep-merges the three groups. */
export function mergePreferences(raw: unknown): AccountPreferences {
  const d = DEFAULT_PREFERENCES;
  if (typeof raw !== "object" || raw === null) return clone(d);
  const r = raw as Record<string, unknown>;
  const email = (r.email ?? {}) as Record<string, unknown>;
  const push = (r.push ?? {}) as Record<string, unknown>;
  const privacy = (r.privacy ?? {}) as Record<string, unknown>;
  return {
    email: {
      product: bool(email.product, d.email.product),
      render: bool(email.render, d.email.render),
      security: bool(email.security, d.email.security),
      digest: oneOf<EmailCadence>(email.digest, ["off", "weekly", "monthly"], d.email.digest),
    },
    push: {
      renderComplete: bool(push.renderComplete, d.push.renderComplete),
      directorReplies: bool(push.directorReplies, d.push.directorReplies),
      weeklyStreak: bool(push.weeklyStreak, d.push.weeklyStreak),
    },
    privacy: {
      visibility: oneOf<ProfileVisibility>(
        privacy.visibility,
        ["private", "friends", "public"],
        d.privacy.visibility,
      ),
      personalization: bool(privacy.personalization, d.privacy.personalization),
      analytics: bool(privacy.analytics, d.privacy.analytics),
      showActivity: bool(privacy.showActivity, d.privacy.showActivity),
    },
  };
}

function clone(p: AccountPreferences): AccountPreferences {
  return {
    email: { ...p.email },
    push: { ...p.push },
    privacy: { ...p.privacy },
  };
}

// ---- Reactive store ------------------------------------------------------- //

export interface PreferencesStore {
  get(): AccountPreferences;
  /** Patch a single group; returns the new value. */
  patch(patch: DeepPartial<AccountPreferences>): AccountPreferences;
  /** Replace wholesale (validated through merge). */
  set(next: AccountPreferences): void;
  reset(): void;
  subscribe(fn: () => void): () => void;
}

type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };

function applyPatch(base: AccountPreferences, patch: DeepPartial<AccountPreferences>): AccountPreferences {
  return mergePreferences({
    email: { ...base.email, ...(patch.email ?? {}) },
    push: { ...base.push, ...(patch.push ?? {}) },
    privacy: { ...base.privacy, ...(patch.privacy ?? {}) },
  });
}

export function createPreferencesStore(backing?: KeyValueStore | null): PreferencesStore {
  const store = resolveStore(backing);
  let prefs = mergePreferences(readJson<unknown>(store, STORAGE_KEY, null));
  const subs = new Set<() => void>();

  const commit = (next: AccountPreferences) => {
    prefs = next;
    writeJson(store, STORAGE_KEY, prefs);
    subs.forEach((fn) => fn());
  };

  return {
    get: () => prefs,
    patch(patch) {
      const next = applyPatch(prefs, patch);
      commit(next);
      return next;
    },
    set: (next) => commit(mergePreferences(next)),
    reset: () => commit(clone(DEFAULT_PREFERENCES)),
    subscribe(fn) {
      subs.add(fn);
      return () => void subs.delete(fn);
    },
  };
}

export const PREFERENCES_STORAGE_KEY = STORAGE_KEY;
