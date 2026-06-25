import { useState } from "react";
import HomePage from "./components/HomePage";
import LoginPage from "./components/LoginPage";

export default function App() {
  const [entered, setEntered] = useState(false);

  if (!entered) {
    return <LoginPage onEnter={() => setEntered(true)} />;
  }

  return <HomePage />;
}
