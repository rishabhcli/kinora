import { useState, lazy, Suspense } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import LoginPage from "./components/LoginPage";
import { api } from "./lib/api";

const HomePage = lazy(() => import("./components/HomePage"));

const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];

export default function App() {
  const [entered, setEntered] = useState(false);
  const prefersReduced = useReducedMotion() ?? false;

  // Crossing the threshold into the app. LoginPage runs its own cinematic exit
  // (the card recedes, the wall blooms) and then calls onEnter; here we keep the
  // *home* wrapper opacity-only on purpose — a transform/filter/backdrop-filter on
  // it would become the containing block for HomePage's `position: fixed` navbar
  // and break its anchor. A brief warm flash carries the "library opens" beat over.
  return (
    <>
      <AnimatePresence mode="wait">
        {!entered ? (
          <motion.div
            key="login"
            exit={{ opacity: 0 }}
            transition={{ duration: prefersReduced ? 0.2 : 0.34, ease: EASE }}
          >
            <LoginPage onEnter={() => setEntered(true)} />
          </motion.div>
        ) : (
          <motion.div
            key="home"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: prefersReduced ? 0.2 : 0.6, ease: EASE }}
          >
            <Suspense fallback={<div className="kinora-bg min-h-screen" />}>
              <HomePage
                onLogout={() => {
                  api.logout(); // clear the Bearer token
                  setEntered(false); // back to the login screen
                }}
              />
            </Suspense>
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {entered && !prefersReduced && (
          <motion.div
            key="flash"
            className="auth-enter-flash"
            initial={{ opacity: 0.85 }}
            animate={{ opacity: 0 }}
            transition={{ duration: 0.95, ease: EASE }}
            aria-hidden="true"
          />
        )}
      </AnimatePresence>
    </>
  );
}
