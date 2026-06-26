import { useSyncExternalStore } from "react";

// A reusable "OS media-query preference with an in-app override" store, used for
// reduced-motion, reduced-transparency and high-contrast. Resolved value =
// override (if set) ELSE the OS media-query state. Override persists.

export type PrefOverride = boolean | null; // null = follow the OS

export interface MediaPrefOptions {
  media: string;
  storageKey: string;
  onValue?: string; // persisted token for "force on" (default "on")
  offValue?: string; // persisted token for "force off" (default "off")
}

export interface MediaPref {
  /** React hook — resolved boolean, re-renders on OS change or setOverride. */
  use(): boolean;
  getSnapshot(): boolean;
  getOverride(): PrefOverride;
  setOverride(value: PrefOverride): void;
  subscribe(cb: () => void): () => void;
}

export function createMediaPref(options: MediaPrefOptions): MediaPref {
  const { media, storageKey, onValue = "on", offValue = "off" } = options;

  function load(): PrefOverride {
    try {
      const v = localStorage.getItem(storageKey);
      if (v === onValue) return true;
      if (v === offValue) return false;
    } catch {
      /* storage blocked */
    }
    return null;
  }

  function persist(v: PrefOverride): void {
    try {
      if (v === null) localStorage.removeItem(storageKey);
      else localStorage.setItem(storageKey, v ? onValue : offValue);
    } catch {
      /* storage blocked */
    }
  }

  let override: PrefOverride = load();
  const listeners = new Set<() => void>();

  function osMatches(): boolean {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
    return window.matchMedia(media).matches;
  }

  function resolve(): boolean {
    return override === null ? osMatches() : override;
  }

  function subscribe(cb: () => void): () => void {
    listeners.add(cb);
    let mql: MediaQueryList | undefined;
    if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
      mql = window.matchMedia(media);
      if (mql.addEventListener) mql.addEventListener("change", cb);
      else mql.addListener(cb);
    }
    return () => {
      listeners.delete(cb);
      if (mql) {
        if (mql.removeEventListener) mql.removeEventListener("change", cb);
        else mql.removeListener(cb);
      }
    };
  }

  return {
    use: () => useSyncExternalStore(subscribe, resolve, resolve),
    getSnapshot: resolve,
    getOverride: () => override,
    setOverride(value: PrefOverride) {
      override = value;
      persist(value);
      listeners.forEach((l) => l());
    },
    subscribe,
  };
}
