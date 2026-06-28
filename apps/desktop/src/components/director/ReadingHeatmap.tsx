// ReadingHeatmap — a GitHub-style calendar grid of daily reading activity over
// the last N weeks. Intensity auto-scales to the busiest day. Pure data from
// `readingHeatmap`; this is a presentational SVG-free grid of divs.
import { readingHeatmap, type ReadingEvent } from "../../lib/api/analytics";

interface ReadingHeatmapProps {
  events: ReadingEvent[];
  weeks?: number;
}

const LEVEL_COLORS: Record<number, string> = {
  0: "rgba(255,255,255,0.05)",
  1: "rgba(212,164,78,0.30)",
  2: "rgba(212,164,78,0.50)",
  3: "rgba(212,164,78,0.72)",
  4: "rgba(212,164,78,0.95)",
};

export default function ReadingHeatmap({ events, weeks = 12 }: ReadingHeatmapProps) {
  const columns = readingHeatmap(events, weeks);
  const activeDays = columns.flat().filter((c) => c.level > 0).length;

  return (
    <div className="rounded-xl p-3.5" style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.06)" }}>
      <div className="flex items-center justify-between mb-2">
        <p className="text-[11px] font-medium text-kinora-text">Reading activity</p>
        <p className="text-[10px] text-kinora-muted">{activeDays} active days · {weeks} weeks</p>
      </div>
      <div className="flex gap-1 overflow-x-auto hide-scrollbar" role="img" aria-label={`Reading activity heatmap: ${activeDays} active days over ${weeks} weeks`}>
        {columns.map((col, w) => (
          <div key={w} className="flex flex-col gap-1">
            {col.map((cell) => (
              <div
                key={cell.day}
                title={cell.minutes > 0 ? `${cell.minutes} min` : "no reading"}
                className="rounded-sm"
                style={{ width: 11, height: 11, background: LEVEL_COLORS[cell.level] }}
              />
            ))}
          </div>
        ))}
      </div>
      <div className="flex items-center gap-1 mt-2 text-[9px] text-kinora-muted">
        <span>Less</span>
        {[0, 1, 2, 3, 4].map((l) => (
          <span key={l} className="rounded-sm" style={{ width: 9, height: 9, background: LEVEL_COLORS[l] }} />
        ))}
        <span>More</span>
      </div>
    </div>
  );
}
