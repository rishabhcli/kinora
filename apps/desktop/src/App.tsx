import { useState, lazy, Suspense } from "react";
import LoginPage from "./components/LoginPage";

const HomePage = lazy(() => import("./components/HomePage"));

export default function App() {
  const [entered, setEntered] = useState(false);

  if (!entered) {
    return <LoginPage onEnter={() => setEntered(true)} />;
  }

  return (
    <Suspense fallback={<div className="kinora-bg min-h-screen" />}>
      <HomePage />
    </Suspense>
  );
}
