import { QueryClientProvider } from "@tanstack/react-query";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { ActivityIndicator, View } from "react-native";

import { useAuth } from "./src/hooks/useAuth";
import { api } from "./src/lib/api";
import { authStore, loadPersistedToken, persistToken } from "./src/lib/auth";
import { queryClient } from "./src/lib/queryClient";
import { LoginScreen } from "./src/screens/LoginScreen";
import { ShelfScreen } from "./src/screens/ShelfScreen";

/** Restore a persisted session from secure storage and validate it via /me. */
function useBootstrap(): void {
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const token = await loadPersistedToken();
      if (cancelled) return;
      if (!token) {
        authStore.getState().setAnonymous();
        return;
      }
      authStore.getState().setToken(token);
      authStore.getState().setAuthenticating();
      const { data } = await api.GET("/api/auth/me");
      if (cancelled) return;
      if (data) {
        authStore.getState().setSession(token, data);
      } else {
        persistToken(null);
        authStore.getState().setAnonymous();
      }
    })().catch(() => {
      persistToken(null);
      authStore.getState().setAnonymous();
    });
    return () => {
      cancelled = true;
    };
  }, []);
}

function Root() {
  const status = useAuth((state) => state.status);
  if (status === "unknown" || status === "authenticating") {
    return (
      <View style={{ flex: 1, justifyContent: "center", alignItems: "center", backgroundColor: "#0a0a0a" }}>
        <ActivityIndicator color="#fff" />
      </View>
    );
  }
  return status === "authenticated" ? <ShelfScreen /> : <LoginScreen />;
}

export default function App() {
  useBootstrap();
  return (
    <QueryClientProvider client={queryClient}>
      <Root />
      <StatusBar style="light" />
    </QueryClientProvider>
  );
}
