import { QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useEffect } from "react";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./hooks/useAuth";
import { api } from "./lib/api";
import { authStore, loadPersistedToken, persistToken } from "./lib/auth";
import { hasOnboarded } from "./lib/onboarding";
import { queryClient } from "./lib/queryClient";
import LoginPage from "./routes/LoginPage";
import OnboardingPage from "./routes/OnboardingPage";
import ShelfPage from "./routes/ShelfPage";
import WorkspacePage from "./routes/WorkspacePage";

/** On launch, restore a persisted session from secure storage and validate via /me. */
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

function RequireAuth({ children }: { children: ReactNode }) {
  const status = useAuth((state) => state.status);
  if (status === "unknown" || status === "authenticating") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-neutral-950 text-sm text-neutral-400">
        Restoring your session…
      </div>
    );
  }
  if (status !== "authenticated") {
    return <Navigate to={hasOnboarded() ? "/login" : "/onboarding"} replace />;
  }
  return <>{children}</>;
}

export default function App() {
  useBootstrap();
  return (
    <QueryClientProvider client={queryClient}>
      <HashRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/onboarding" element={<OnboardingPage />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <ShelfPage />
              </RequireAuth>
            }
          />
          <Route
            path="/book/:id"
            element={
              <RequireAuth>
                <WorkspacePage />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </HashRouter>
    </QueryClientProvider>
  );
}
