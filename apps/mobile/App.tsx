import { QueryClientProvider } from "@tanstack/react-query";
import { StatusBar } from "expo-status-bar";
import { useEffect, useState } from "react";
import { ActivityIndicator, View } from "react-native";

import { AmbientBackdrop, Wordmark } from "./src/components/ui";
import { useAuth } from "./src/hooks/useAuth";
import { api } from "./src/lib/api";
import { authStore, loadPersistedToken, persistToken } from "./src/lib/auth";
import { queryClient } from "./src/lib/queryClient";
import { LoginScreen } from "./src/screens/LoginScreen";
import { ReadingScreen } from "./src/screens/ReadingScreen";
import { ShelfScreen } from "./src/screens/ShelfScreen";
import { palette } from "./src/theme/tokens";

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
  const [bookId, setBookId] = useState<string | null>(null);

  if (status === "unknown" || status === "authenticating") {
    return (
      <AmbientBackdrop>
        <View style={{ flex: 1, justifyContent: "center", alignItems: "center" }}>
          <Wordmark withMark withTagline />
          <ActivityIndicator color={palette.emberGlow} style={{ marginTop: 28 }} />
        </View>
      </AmbientBackdrop>
    );
  }
  if (status !== "authenticated") return <LoginScreen />;
  if (bookId) return <ReadingScreen bookId={bookId} onBack={() => setBookId(null)} />;
  return <ShelfScreen onOpen={setBookId} />;
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
