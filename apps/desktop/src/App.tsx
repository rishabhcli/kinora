import { useState, lazy, Suspense } from "react";
import LoginPage from "./components/LoginPage";
import { api } from "./lib/api";

const HomePage = lazy(() => import("./components/HomePage"));

export default function App() {
  const [entered, setEntered] = useState(false);

  if (!entered) {
    return <LoginPage onEnter={() => setEntered(true)} />;
  }

  return (
    <Suspense fallback={<div className="kinora-bg min-h-screen" />}>
      <HomePage
        onLogout={() => {
          api.logout(); // clear the Bearer token
          setEntered(false); // back to the login screen
        }}
      />
    </Suspense>
  );
}
