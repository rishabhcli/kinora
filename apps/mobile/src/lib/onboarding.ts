import * as SecureStore from "expo-secure-store";

/**
 * First-run onboarding persistence via expo-secure-store (Keychain on iOS,
 * Keystore on Android) — mirrors the token persistence in `auth.ts`.
 *
 * We only ever store a single sentinel, so the flag is "set" iff the key exists.
 * Reads/writes are best-effort: if secure storage is unavailable we simply treat
 * the user as not-yet-onboarded and show the intro again next launch.
 */
const ONBOARDED_KEY = "kinora_onboarded";
const ONBOARDED_VALUE = "1";

export async function loadHasOnboarded(): Promise<boolean> {
  try {
    return (await SecureStore.getItemAsync(ONBOARDED_KEY)) === ONBOARDED_VALUE;
  } catch {
    return false;
  }
}

export async function persistHasOnboarded(): Promise<void> {
  try {
    await SecureStore.setItemAsync(ONBOARDED_KEY, ONBOARDED_VALUE);
  } catch {
    // Best-effort; a failure just means the intro shows again next launch.
  }
}
