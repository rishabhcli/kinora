import type { RefDraft } from "@kinora/core";

interface ReferenceImageGridProps {
  references: RefDraft[];
  onToggleLock: (index: number) => void;
  /** Open the reference full-size (inspect-only); the tile body still toggles lock. */
  onView?: (index: number) => void;
}

/**
 * The locked reference set for an entity (§8.1): the images that pin its
 * canonical look. Each tile toggles between **locked** (part of the reference
 * set every dependent shot is conditioned on) and unlocked. Re-locking a
 * different pose *is* the "swap a locked reference image" edit (§5.4) — on save
 * it round-trips by the durable `oss_key` and re-renders only the shots that
 * cite this entity.
 */
export function ReferenceImageGrid({ references, onToggleLock, onView }: ReferenceImageGridProps) {
  if (references.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-white/12 px-3 py-4 text-center text-[11.5px] text-white/35">
        No reference images locked yet.
      </p>
    );
  }

  const lockedCount = references.filter((r) => r.locked).length;

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-3 gap-2">
        {references.map((ref, index) => {
          const locked = ref.locked;
          return (
            <div
              key={ref.ossKey ?? ref.ossUrl}
              className={`group relative aspect-square overflow-hidden rounded-lg transition ${
                locked ? "ring-2 ring-ember-glow" : "ring-1 ring-white/12 hover:ring-white/30"
              }`}
            >
              <img
                src={ref.ossUrl}
                alt={ref.pose ?? "reference"}
                className={`h-full w-full object-cover transition ${locked ? "" : "opacity-55 grayscale group-hover:opacity-80"}`}
              />
              {/* Full-area lock toggle (the swap). */}
              <button
                type="button"
                onClick={() => onToggleLock(index)}
                aria-pressed={locked}
                aria-label={`${locked ? "Unlock" : "Lock"} reference${ref.pose ? ` (${ref.pose})` : ""}`}
                className="absolute inset-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
              />
              {/* Lock chip, top-right (decorative; clicks fall through to the toggle). */}
              <span
                className={`pointer-events-none absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full ${
                  locked ? "bg-ember-glow text-walnut-deep" : "bg-black/55 text-white/70"
                }`}
              >
                {locked ? (
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="5" y="11" width="14" height="9" rx="1.5" fill="currentColor" stroke="none" />
                    <path d="M8 11V8a4 4 0 0 1 8 0v3" />
                  </svg>
                ) : (
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="5" y="11" width="14" height="9" rx="1.5" />
                    <path d="M8 11V8a4 4 0 0 1 7.5-2" />
                  </svg>
                )}
              </span>
              {/* View full-size (inspect) — above the toggle. */}
              {onView ? (
                <button
                  type="button"
                  onClick={() => onView(index)}
                  aria-label={`View reference${ref.pose ? ` (${ref.pose})` : ""} full size`}
                  className="absolute left-1 top-1 z-10 flex h-5 w-5 items-center justify-center rounded-full bg-black/55 text-white/80 opacity-0 transition hover:bg-black/75 hover:text-white focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow group-hover:opacity-100"
                >
                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="11" cy="11" r="7" />
                    <path d="m20 20-3.5-3.5" />
                  </svg>
                </button>
              ) : null}
              {ref.pose && (
                <span className="pointer-events-none absolute bottom-1 left-1 rounded bg-black/60 px-1.5 py-0.5 text-[9.5px] font-medium text-white/85">
                  {ref.pose}
                </span>
              )}
            </div>
          );
        })}
      </div>
      <p className="text-[11px] text-white/40">
        {lockedCount} of {references.length} locked — the look every dependent shot is conditioned on.
      </p>
    </div>
  );
}
