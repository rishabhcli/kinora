import type { AuthState } from "@kinora/core";
import { useStore } from "zustand";

import { authStore } from "../lib/auth";

/** Bind the framework-agnostic vanilla auth store into React. */
export function useAuth<T>(selector: (state: AuthState) => T): T {
  return useStore(authStore, selector);
}
