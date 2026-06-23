import { createAuthStore } from "@kinora/core";

/**
 * The mobile auth store. In-memory for now; the mobile-native phase persists the
 * token with expo-secure-store (Keychain/Keystore).
 */
export const authStore = createAuthStore();
