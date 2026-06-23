import { createAuthStore } from "@kinora/core";
import * as SecureStore from "expo-secure-store";

/**
 * The mobile auth store + token persistence via expo-secure-store
 * (Keychain on iOS, Keystore on Android).
 */
const TOKEN_KEY = "kinora_token";

export const authStore = createAuthStore();

export async function loadPersistedToken(): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function persistToken(token: string | null): void {
  void (async () => {
    try {
      if (token) await SecureStore.setItemAsync(TOKEN_KEY, token);
      else await SecureStore.deleteItemAsync(TOKEN_KEY);
    } catch {
      // Best-effort; a failure just means the user re-logs in next launch.
    }
  })();
}
