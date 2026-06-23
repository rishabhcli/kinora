import { ccsDelta, type ShotResponse, shortShotId, summarizeQa } from "@kinora/core";
import { useEffect, useRef } from "react";

import type { ShotUpdate, ShotUpdateMap } from "../../director/shots";

interface DependentShotsStripProps {
  /** The dependent shots an edit invalidated (the §8.7 blast radius), in order. */
  shotIds: string[];
  /** The fetched shot list, for each tile's old clip / QA fallback. */
  shots: ShotResponse[] | undefined;
  /** The shared per-shot render map (rendering → ready) — the same one the
   *  Director timeline reads, so both stay in lockstep. */
  updates: ShotUpdateMap;
}

/**
 * Character-consistency before → after the edit (§9.5) — the proof a surgical
 * re-render kept the look. Shows "0.88 → 0.91" (green if it held, amber if it
 * dropped) when both are known, else the single post-render score.
 */
function CcsBadge({
  beforeQa,
  afterQa,
}: {
  beforeQa: Record<string, unknown> | null | undefined;
  afterQa: Record<string, unknown> | null | undefined;
}) {
  const { before, after, held } = ccsDelta(beforeQa, afterQa);
  const passed = summarizeQa(afterQa ?? null)?.passed ?? null;
  if (after === null && before === null) {
    return <span className="text-[10px] text-white/35">No QA</span>;
  }
  if (before !== null && after !== null) {
    return (
      <span
        className={`inline-flex items-center gap-1 text-[10px] font-semibold tabular-nums ${held ? "text-emerald-300" : "text-amber-300"}`}
        title={`Character consistency ${before.toFixed(2)} → ${after.toFixed(2)} — ${held ? "held" : "dropped"} across the edit`}
      >
        CCS {before.toFixed(2)}
        <span className="text-white/40">→</span>
        {after.toFixed(2)}
      </span>
    );
  }
  const value = (after ?? before)!.toFixed(2);
  return (
    <span
      className={`inline-flex items-center gap-1 text-[10px] font-semibold ${passed === false ? "text-rose-300" : "text-emerald-300"}`}
      title={`Character consistency ${value}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${passed === false ? "bg-rose-400" : "bg-emerald-400"}`} />
      CCS {value}
    </span>
  );
}

function ShotTile({
  shotId,
  shot,
  update,
}: {
  shotId: string;
  shot: ShotResponse | undefined;
  update: ShotUpdate | undefined;
}) {
  // No update yet = just invalidated by the edit, regen not started → rendering.
  const ready = update?.status === "ready";
  const clipUrl = update?.clipUrl ?? shot?.clip_url ?? null;
  const videoRef = useRef<HTMLVideoElement | null>(null);

  // Nudge the poster frame: a bare <video> paints black until it decodes one.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !clipUrl) return;
    const onData = (): void => {
      if (v.currentTime < 0.02) v.currentTime = 0.05;
    };
    v.addEventListener("loadeddata", onData);
    return () => v.removeEventListener("loadeddata", onData);
  }, [clipUrl]);

  return (
    <div
      className={`relative flex w-[148px] shrink-0 snap-start flex-col overflow-hidden rounded-xl transition ${
        ready ? "ring-2 ring-emerald-400/70" : "ring-1 ring-sky-400/40"
      }`}
    >
      <div className="relative aspect-video w-full bg-black">
        {clipUrl ? (
          <video
            ref={videoRef}
            src={clipUrl}
            preload="metadata"
            muted
            playsInline
            className={`h-full w-full object-cover transition ${ready ? "" : "opacity-40 saturate-50"}`}
          />
        ) : (
          <div className="shimmer absolute inset-0 bg-walnut-deep/80" />
        )}

        {!ready && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 bg-black/50 backdrop-blur-[1px]">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-sky-300/30 border-t-sky-300 motion-reduce:animate-none" />
            <span className="text-[9.5px] font-semibold uppercase tracking-[0.12em] text-sky-200">
              Re-rendering
            </span>
          </div>
        )}
        {ready && (
          <span className="absolute right-1 top-1 rounded-full bg-emerald-500/85 p-0.5 text-walnut-deep">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <path d="m5 13 4 4L19 7" />
            </svg>
          </span>
        )}
      </div>

      <div className="flex items-center justify-between gap-1 bg-walnut-deep/75 px-2 py-1.5">
        <span className="truncate font-mono text-[10px] text-white/55" title={shotId}>
          {shortShotId(shotId)}
        </span>
        {ready ? (
          <CcsBadge beforeQa={shot?.qa} afterQa={update?.qa} />
        ) : (
          <span className="text-[10px] text-sky-300/80">queued</span>
        )}
      </div>
    </div>
  );
}

/**
 * The dependent-shots filmstrip for a Director canon edit (§5.4 / §8.7): exactly
 * the shots whose reference set cited the edited entity, each invalidated and
 * shown re-rendering (stale frame dimmed under a spinner), then flipping to its
 * fresh clip with a before → after CCS badge as `regen_done` lands. Reads the
 * same `shotUpdates` the Director timeline does; everything *not* shown here
 * stayed a cache hit (untouched).
 */
export function DependentShotsStrip({ shotIds, shots, updates }: DependentShotsStripProps) {
  if (shotIds.length === 0) return null;
  const byId = new Map((shots ?? []).map((s) => [s.shot_id, s]));
  return (
    <div className="flex snap-x gap-2.5 overflow-x-auto pb-1 [scrollbar-width:thin]">
      {shotIds.map((id) => (
        <ShotTile key={id} shotId={id} shot={byId.get(id)} update={updates[id]} />
      ))}
    </div>
  );
}
