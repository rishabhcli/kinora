// FacetSidebar — the faceted-filter rail for the library workbench. Renders
// genre / era / reading-state facets with live counts (from summarizeFacets),
// plus a "live films only" toggle. Selecting a facet value toggles it in the
// FacetQuery; counts come from the FULL library so a facet's count reflects how
// many books it would surface, not the already-filtered set.
import {
  summarizeFacets,
  readingState,
  type FacetQuery,
  type ReadingState,
} from "../../lib/api/collections";
import type { LibraryBook } from "../../lib/api/library";

interface FacetSidebarProps {
  books: LibraryBook[];
  query: FacetQuery;
  onChange: (next: FacetQuery) => void;
}

const STATE_LABELS: Record<ReadingState, string> = {
  unread: "Unread",
  in_progress: "In progress",
  finished: "Finished",
};
const STATE_ORDER: ReadingState[] = ["in_progress", "unread", "finished"];

function toggle<T>(list: T[] | undefined, value: T): T[] {
  const set = new Set(list ?? []);
  if (set.has(value)) set.delete(value);
  else set.add(value);
  return [...set];
}

function FacetGroup<T extends string>({
  title,
  values,
  selected,
  counts,
  onToggle,
  labelOf,
}: {
  title: string;
  values: T[];
  selected: T[];
  counts: Record<string, number>;
  onToggle: (v: T) => void;
  labelOf?: (v: T) => string;
}) {
  if (values.length === 0) return null;
  return (
    <div className="mb-5">
      <p className="text-[10px] uppercase tracking-wide text-kinora-muted mb-2">{title}</p>
      <ul className="flex flex-col gap-1">
        {values.map((v) => {
          const active = selected.includes(v);
          const count = counts[labelOf ? labelOf(v) : v] ?? 0;
          return (
            <li key={v}>
              <button
                type="button"
                onClick={() => onToggle(v)}
                aria-pressed={active}
                className="flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-[11.5px] transition-colors"
                style={{
                  background: active ? "rgba(212,164,78,0.16)" : "transparent",
                  color: active ? "rgba(236,231,223,0.98)" : "rgba(236,231,223,0.72)",
                  border: `1px solid ${active ? "rgba(212,164,78,0.3)" : "transparent"}`,
                }}
              >
                <span className="truncate">{labelOf ? labelOf(v) : v}</span>
                <span className="text-[10px] text-kinora-muted tabular-nums ml-2">{count}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function FacetSidebar({ books, query, onChange }: FacetSidebarProps) {
  const facets = summarizeFacets(books);
  const genres = facets.genres.map((f) => f.value);
  const eras = facets.eras.map((f) => f.value);
  const genreCounts = Object.fromEntries(facets.genres.map((f) => [f.value, f.count]));
  const eraCounts = Object.fromEntries(facets.eras.map((f) => [f.value, f.count]));

  // reading-state counts keyed by the same human label summarizeFacets emits.
  const stateCounts: Record<string, number> = {};
  for (const b of books) {
    const label = STATE_LABELS[readingState(b)];
    stateCounts[label] = (stateCounts[label] ?? 0) + 1;
  }

  const active =
    (query.genres?.length ?? 0) +
    (query.eras?.length ?? 0) +
    (query.states?.length ?? 0) +
    (query.liveOnly ? 1 : 0);

  return (
    <aside className="w-[200px] shrink-0">
      <div className="flex items-center justify-between mb-3">
        <p className="text-[12px] font-semibold text-kinora-text">Filters</p>
        {active > 0 && (
          <button
            type="button"
            onClick={() => onChange({ text: query.text })}
            className="text-[10px] text-kinora-muted hover:text-kinora-text transition-colors"
          >
            Clear ({active})
          </button>
        )}
      </div>

      <FacetGroup
        title="Reading"
        values={STATE_ORDER}
        selected={query.states ?? []}
        counts={stateCounts}
        labelOf={(s) => STATE_LABELS[s]}
        onToggle={(s) => onChange({ ...query, states: toggle(query.states, s) })}
      />
      <FacetGroup
        title="Genre"
        values={genres}
        selected={query.genres ?? []}
        counts={genreCounts}
        onToggle={(g) => onChange({ ...query, genres: toggle(query.genres, g) })}
      />
      <FacetGroup
        title="Era"
        values={eras}
        selected={query.eras ?? []}
        counts={eraCounts}
        onToggle={(e) => onChange({ ...query, eras: toggle(query.eras, e) })}
      />

      <button
        type="button"
        onClick={() => onChange({ ...query, liveOnly: !query.liveOnly })}
        aria-pressed={Boolean(query.liveOnly)}
        className="flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-[11.5px] transition-colors"
        style={{
          background: query.liveOnly ? "rgba(52,211,153,0.14)" : "transparent",
          color: query.liveOnly ? "#34d399" : "rgba(236,231,223,0.72)",
          border: `1px solid ${query.liveOnly ? "rgba(52,211,153,0.3)" : "rgba(255,255,255,0.08)"}`,
        }}
      >
        <span>Live films only</span>
        <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: query.liveOnly ? "#34d399" : "rgba(255,255,255,0.2)" }} />
      </button>
    </aside>
  );
}
