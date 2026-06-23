import { useCallback, useState } from "react";

import type { EditResult } from "@kinora/core";

export interface CanonRegenState {
  /** The most recently applied canon edit — drives the panel's result banner +
   *  which dependent shots its filmstrip shows. Persists at the reading-room
   *  scope so it survives the panel closing/reopening. */
  lastEdit: EditResult | null;
  registerEdit: (result: EditResult) => void;
}

/**
 * Remember the last canon edit (§5.4 / §8.7). The per-shot render *status* lives
 * in the shared `shotUpdates` map (so the Director timeline and the canon panel
 * agree); this only holds the edit's identity + blast radius for the banner.
 */
export function useCanonRegen(): CanonRegenState {
  const [lastEdit, setLastEdit] = useState<EditResult | null>(null);
  const registerEdit = useCallback((result: EditResult) => setLastEdit(result), []);
  return { lastEdit, registerEdit };
}
