// Onboarding · Taste — pick favourite genres (seeds recommendations). Backed by
// the persisted taste store (lib/account/taste); selection is capped + live.
import { useEffect, useState } from "react";
import { TASTE_GENRES, TASTE_MAX_SELECTION, createTasteStore, type TasteGenre } from "../../../lib/account";

export function TasteStep() {
  const [store] = useState(() => createTasteStore());
  const [selected, setSelected] = useState<TasteGenre[]>(() => store.get());

  useEffect(() => store.subscribe(() => setSelected(store.get())), [store]);

  const atCap = selected.length >= TASTE_MAX_SELECTION;

  return (
    <div>
      <div className="onb-chips" role="group" aria-label="Favourite genres">
        {TASTE_GENRES.map((g) => {
          const on = selected.includes(g);
          return (
            <button
              key={g}
              type="button"
              className={`onb-chip${on ? " is-selected" : ""}`}
              aria-pressed={on}
              disabled={!on && atCap}
              onClick={() => store.toggle(g)}
            >
              {g}
            </button>
          );
        })}
      </div>
      <p className="acct-row-meta" style={{ marginTop: 12 }}>
        {selected.length === 0
          ? "Pick a few you love — or skip and we'll learn as you read."
          : `${selected.length} selected${atCap ? " (max)" : ""}`}
      </p>
    </div>
  );
}
