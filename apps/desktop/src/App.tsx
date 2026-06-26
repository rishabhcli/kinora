import { useState, lazy, Suspense } from "react";
import { AnimatePresence, motion } from "framer-motion";
import LoginPage from "./components/LoginPage";
import { api } from "./lib/api";

const HomePage = lazy(() => import("./components/HomePage"));

const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];

export default function App() {
  const [entered, setEntered] = useState(false);

  // Crossing the threshold: the login dissolves and the app rises into view.
  // Opacity-only on purpose — a transform here would re-anchor the fixed navbar.
  return (
    <AnimatePresence mode="wait">
      {!entered ? (
        <motion.div
          key="login"
          initial={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.4, ease: EASE }}
        >
          <LoginPage onEnter={() => setEntered(true)} />
        </motion.div>
      ) : (
        <motion.div
          key="home"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.6, ease: EASE }}
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
  );
}
