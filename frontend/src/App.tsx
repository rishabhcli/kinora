import { type ReactNode, Suspense, lazy, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import { onUnauthorized } from "./api/client";
import { Spinner } from "./components/common/icons";
import { BrandMark } from "./components/common/BrandMark";
import { useAuthStore } from "./stores/authStore";

// Route-level code splitting keeps the initial bundle lean (the metrics panel
// pulls in Recharts, so it lands in the workspace chunk, not the shell).
const LoginPage = lazy(() => import("./routes/LoginPage"));
const ShelfPage = lazy(() => import("./routes/ShelfPage"));
const WorkspacePage = lazy(() => import("./routes/WorkspacePage"));

function BackgroundGlow() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none fixed inset-0 -z-10"
      style={{
        background:
          "radial-gradient(900px 600px at 12% -10%, rgba(124,92,255,0.16), transparent 60%), radial-gradient(720px 520px at 100% 110%, rgba(76,29,149,0.2), transparent 55%)",
      }}
    />
  );
}

function FullScreenLoader({ label }: { label: string }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-4 text-kinora-muted">
      <BrandMark className="h-10 w-10 motion-safe:animate-pulse-glow" />
      <span className="inline-flex items-center gap-2 text-sm">
        <Spinner className="h-4 w-4" />
        {label}
      </span>
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const status = useAuthStore((s) => s.status);
  const location = useLocation();

  if (status === "unknown" || status === "authenticating") {
    return <FullScreenLoader label="Restoring your session…" />;
  }
  if (status !== "authenticated") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

export default function App() {
  const bootstrap = useAuthStore((s) => s.bootstrap);
  const logout = useAuthStore((s) => s.logout);

  useEffect(() => {
    // A 401 from anywhere clears the session and bounces to /login.
    onUnauthorized(() => logout());
    void bootstrap();
  }, [bootstrap, logout]);

  return (
    <>
      <BackgroundGlow />
      <Suspense fallback={<FullScreenLoader label="Loading…" />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
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
      </Suspense>
    </>
  );
}
