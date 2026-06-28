// SceneTimeline — the §5.4 scene/shot timeline. Loads the book's shots, groups
// them into per-scene lanes (reading order), and lets the Director pick a shot
// to inspect/re-roll. Each shot tile shows its render state + a duration-scaled
// width so the timeline reads like a cut. Live regen state (set by the parent
// from SSE) overlays a "re-rendering" shimmer on the affected tile.
import { useMemo } from "react";
import {
  buildSceneLanes,
  isShotRenderable,
  type DirectorShot,
} from "../../lib/api/director";

interface SceneTimelineProps {
  shots: DirectorShot[];
  selectedShotId: string | null;
  onSelect: (shot: DirectorShot) => void;
  /** shot ids currently re-rendering (from SSE regen events). */
  reRendering: ReadonlySet<string>;
  /** thread counts per shot id, for the note badge. */
  noteCounts?: Record<string, number>;
}

function fmtDur(s: number): string {
  if (s <= 0) return "—";
  if (s < 60) return `${s.toFixed(0)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

function tileColor(shot: DirectorShot, reRendering: boolean): string {
  if (reRendering) return "#d4a44e";
  if (isShotRenderable(shot)) return "#34d399";
  if (shot.status?.toLowerCase().includes("fail") || shot.status?.toLowerCase().includes("error"))
    return "#f87171";
  return "#6b7280";
}

export default function SceneTimeline({
  shots,
  selectedShotId,
  onSelect,
  reRendering,
  noteCounts = {},
}: SceneTimelineProps) {
  const lanes = useMemo(() => buildSceneLanes(shots), [shots]);
  const totalDuration = useMemo(() => lanes.reduce((s, l) => s + l.duration_s, 0), [lanes]);

  if (shots.length === 0) {
    return (
      <div className="py-12 text-center">
        <p className="text-[12px] text-kinora-muted">
          No shots yet — this book hasn't been broken into shots.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-kinora-muted">
          {shots.length} shots · {lanes.length} scenes · {fmtDur(totalDuration)} total
        </p>
      </div>

      {lanes.map((lane) => (
        <section key={lane.scene_id}>
          <div className="flex items-center gap-2 mb-2">
            <span className="w-1 h-3.5 rounded-full" style={{ background: "linear-gradient(180deg, rgba(212,164,78,0.8), rgba(212,164,78,0.3))" }} />
            <h4 className="text-[12px] font-semibold text-kinora-text">
              {lane.scene_id === "(unscened)" ? "Unscened" : `Scene ${lane.scene_id.slice(0, 8)}`}
            </h4>
            <span className="text-[10px] text-kinora-muted">
              {lane.shots.length} shots · {fmtDur(lane.duration_s)}
              {lane.word_start !== null && lane.word_end !== null ? ` · words ${lane.word_start}–${lane.word_end}` : ""}
            </span>
          </div>

          <div
            className="flex gap-1.5 overflow-x-auto hide-scrollbar pb-2"
            role="listbox"
            aria-label={`Shots in ${lane.scene_id}`}
          >
            {lane.shots.map((shot) => {
              const isRe = reRendering.has(shot.shot_id);
              const color = tileColor(shot, isRe);
              const selected = shot.shot_id === selectedShotId;
              const notes = noteCounts[shot.shot_id] ?? 0;
              // width scaled by duration (min 56px, max 160px) so the cut reads.
              const width = Math.max(56, Math.min(160, (shot.duration_s ?? 5) * 14));
              return (
                <button
                  key={shot.shot_id}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => onSelect(shot)}
                  className="relative shrink-0 rounded-lg p-2 text-left transition-all"
                  style={{
                    width,
                    height: 64,
                    background: selected ? "rgba(212,164,78,0.14)" : "rgba(255,255,255,0.04)",
                    border: `1px solid ${selected ? "rgba(212,164,78,0.5)" : "rgba(255,255,255,0.08)"}`,
                    outline: isRe ? "1px dashed rgba(212,164,78,0.6)" : "none",
                  }}
                >
                  <span className="absolute top-1.5 left-1.5 inline-block h-1.5 w-1.5 rounded-full" style={{ background: color, boxShadow: isRe ? `0 0 6px ${color}` : "none" }} />
                  {notes > 0 && (
                    <span
                      className="absolute top-1 right-1.5 inline-flex items-center justify-center rounded-full text-[8px] font-bold"
                      style={{ minWidth: 14, height: 14, padding: "0 3px", background: "rgba(212,164,78,0.85)", color: "#1a1408" }}
                    >
                      {notes}
                    </span>
                  )}
                  <span className="absolute bottom-1.5 left-1.5 right-1.5 text-[9px] text-kinora-muted truncate">
                    {shot.shot_id.slice(0, 6)} · {fmtDur(shot.duration_s ?? 0)}
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
