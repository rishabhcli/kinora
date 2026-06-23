import { QueryClientProvider } from "@tanstack/react-query";
import { StatusBar } from "expo-status-bar";

import { useAuth } from "./src/hooks/useAuth";
import { queryClient } from "./src/lib/queryClient";
import { LoginScreen } from "./src/screens/LoginScreen";
import { ShelfScreen } from "./src/screens/ShelfScreen";

function Root() {
  const status = useAuth((state) => state.status);
  return status === "authenticated" ? <ShelfScreen /> : <LoginScreen />;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Root />
      <StatusBar style="light" />
    </QueryClientProvider>
  );
}
