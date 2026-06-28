// ShotInspector — the detail pane for one shot: clip preview, render metadata,
// a one-click RE-ROLL ("another take"), the §5.4 region-comment bar, and the
// shot's annotation thread list. Re-roll and comment both go through the REST
// regen path (`POST /sessions/{id}/comment`); the inspector marks the shot
// "re-rendering" optimistically until the SSE `regen_done` arrives.
import { useCallback, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  director,
  isShotRenderable,
  shotClipUrl,
  type DirectorShot,
} from "../../lib/api/director";
import RegionCommentBar from "./RegionCommentBar";
import ThreadPanel from "./ThreadPanel";
import type { AnnotationStore } from "../../lib/api/annotations";

interface ShotInspectorProps {
  shot: DirectorShot;
  sessionId: string | null;
  bookId: string;
  annotations: AnnotationStore;
  author: string;
  /** True while a regen for this shot is in flight (parent tracks SSE). */
  reRendering?: boolean;
  /** Called when the inspector kicks off a regen (re-roll or comment). */
  onRegenStarted?: (shotId: string, jobId: string | null) => void;
}

function StatusPill({ shot, reRendering }: { shot: DirectorShot; reRendering?: boolean }) {
  const [label, color] = reRendering
    ? (["Re-rendering…", "#d4a44e"] as const)
    : isShotRenderable(shot)
      ? (["Live take", "#34d399"] as const)
      : ([shot.status || "pending", "#9aa3b2"] as const);
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-medium"
      style={{ background: `${color}1f`, color, border: `1px solid ${color}40` }}
    >
      <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  );
}

export default function ShotInspector({
  shot,
  sessionId,
  bookId,
  annotations,
  author,
  reRendering,
  onRegenStarted,
}: ShotInspectorProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const clipUrl = shotClipUrl(shot);

  const reroll = useCallback(async () => {
    if (!sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await director.reroll(sessionId, shot.shot_id);
      onRegenStarted?.(shot.shot_id, res.job_id);
    } catch (e) {
      setError(e instanceof ApiError ? `Re-roll failed (${e.status}).` : "Re-roll failed.");
    } finally {
      setBusy(false);
    }
  }, [sessionId, shot.shot_id, busy, onRegenStarted]);

  const span = shot.source_span?.word_range;

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-serif text-base font-semibold text-kinora-text">
            Shot {shot.shot_id.slice(0, 8)}
          </h3>
          <p className="text-[11px] text-kinora-muted mt-0.5">
            {shot.scene_id ? `Scene ${shot.scene_id.slice(0, 8)} · ` : ""}
            {shot.render_mode ?? "—"}
            {span ? ` · words ${span[0]}–${span[1]}` : ""}
            {shot.duration_s ? ` · ${shot.duration_s.toFixed(1)}s` : ""}
          </p>
        </div>
        <StatusPill shot={shot} reRendering={reRendering} />
      </div>

      {/* Clip preview */}
      <div
        className="relative w-full overflow-hidden rounded-xl"
        style={{ aspectRatio: "9 / 16", maxHeight: 320, background: "rgba(0,0,0,0.4)" }}
      >
        {clipUrl ? (
          <video
            key={clipUrl}
            src={clipUrl}
            controls
            loop
            playsInline
            className="h-full w-full object-contain"
          />
        ) : (
          <div className="flex h-full items-center justify-center text-[11px] text-kinora-muted">
            No clip yet — direct or re-roll to generate a take.
          </div>
        )}
      </div>

      {/* Re-roll */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={!sessionId || busy || reRendering}
          onClick={() => void reroll()}
          className="flex items-center gap-2 rounded-xl px-3.5 py-2 text-[11.5px] font-semibold transition-all disabled:opacity-40"
          style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.12)", color: "rgba(236,231,223,0.95)" }}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
            <path d="M3 3v5h5" />
          </svg>
          {busy ? "Re-rolling…" : "Another take"}
        </button>
        <span className="text-[10.5px] text-kinora-muted">Re-rolls the seed for a fresh variation.</span>
      </div>

      {error && (
        <p className="text-[11px]" style={{ color: "#f87171" }} role="alert">
          {error}
        </p>
      )}

      {/* Region comment (REST regen path) */}
      <div className="rounded-xl p-3" style={{ background: "rgba(255,255,255,0.025)", border: "1px solid rgba(255,255,255,0.06)" }}>
        <p className="text-[11px] font-medium text-kinora-text mb-2">Direct this shot</p>
        <RegionCommentBar
          sessionId={sessionId}
          shotId={shot.shot_id}
          onCommented={(res) => onRegenStarted?.(shot.shot_id, res.job_id)}
        />
      </div>

      {/* Annotation threads for this shot */}
      <ThreadPanel
        bookId={bookId}
        anchor={{ shot_id: shot.shot_id, scene_id: shot.scene_id ?? undefined }}
        annotations={annotations}
        author={author}
      />
    </div>
  );
}
