import { QueryClientProvider } from "@tanstack/react-query";
import { StatusBar } from "expo-status-bar";
import { useEffect, useState } from "react";
import { ActivityIndicator, View } from "react-native";

import { AmbientBackdrop, Wordmark } from "./src/components/ui";
import { useAuth } from "./src/hooks/useAuth";
import { api } from "./src/lib/api";
import { authStore, loadPersistedToken, persistToken } from "./src/lib/auth";
import { loadHasOnboarded, persistHasOnboarded } from "./src/lib/onboarding";
import { loadPersistedPreferences } from "./src/lib/preferences";
import { queryClient } from "./src/lib/queryClient";
import { LoginScreen } from "./src/screens/LoginScreen";
import { OnboardingScreen } from "./src/screens/OnboardingScreen";
import { ReadingScreen } from "./src/screens/ReadingScreen";
import { ShelfScreen } from "./src/screens/ShelfScreen";
import { palette } from "./src/theme/tokens";

/** Whether the first-run intro has been seen: unknown while we read storage. */
type OnboardingState = "unknown" | "needed" | "done";

/**
 * Restore a persisted session from secure storage and validate it via /me, and
 * in parallel read the first-run "has onboarded" flag. Both feed the boot gate
 * in <Root/>; the intro is independent of auth, so it loads alongside the token.
 */
function useBootstrap(): OnboardingState {
  const [onboarding, setOnboarding] = useState<OnboardingState>("unknown");

  useEffect(() => {
    let cancelled = false;
    // Warm persisted preferences (reduce-motion override, autoplay) into the
    // store; independent of the boot gate — defaults apply until it resolves.
    void loadPersistedPreferences();

    void (async () => {
      const seen = await loadHasOnboarded();
      if (!cancelled) setOnboarding(seen ? "done" : "needed");
    })().catch(() => {
      if (!cancelled) setOnboarding("needed");
    });

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

  return onboarding;
}

function Root({
  onboarding,
  onOnboarded,
}: {
  onboarding: OnboardingState;
  onOnboarded: () => void;
}) {
  const status = useAuth((state) => state.status);
  const [bookId, setBookId] = useState<string | null>(null);

  // Hold the splash until both the session and the onboarding flag are resolved,
  // so a returning user never sees the intro flash before the library.
  if (status === "unknown" || status === "authenticating" || onboarding === "unknown") {
    return (
      <AmbientBackdrop>
        <View style={{ flex: 1, justifyContent: "center", alignItems: "center" }}>
          <Wordmark withMark withTagline />
          <ActivityIndicator color={palette.emberGlow} style={{ marginTop: 28 }} />
        </View>
      </AmbientBackdrop>
    );
  }
  if (onboarding === "needed") return <OnboardingScreen onDone={onOnboarded} />;
  if (status !== "authenticated") return <LoginScreen />;
  if (bookId) return <ReadingScreen bookId={bookId} onBack={() => setBookId(null)} />;
  return <ShelfScreen onOpen={setBookId} />;
}

export default function App() {
  const bootOnboarding = useBootstrap();
  // Once the user finishes the intro we both persist the flag and optimistically
  // advance the gate, so the transition to login/library is immediate.
  const [onboarded, setOnboarded] = useState(false);
  const onboarding: OnboardingState = onboarded ? "done" : bootOnboarding;

  function completeOnboarding() {
    setOnboarded(true);
    void persistHasOnboarded();
  }

  return (
    <QueryClientProvider client={queryClient}>
      <Root onboarding={onboarding} onOnboarded={completeOnboarding} />
      <StatusBar style="light" />
    </QueryClientProvider>
  );
}
