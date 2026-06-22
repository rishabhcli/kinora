import clsx from "clsx";

import type { Shot } from "../../../api/types";
import { coverGradient } from "../../../lib/cover";
import { useEventsStore } from "../../../stores/eventsStore";

interface ShotTimelineProps {
  shots: Shot[];
  currentShotId: string | null;
  onSelect: (shot: Shot) => void;
}

function QaBadge({ shot }: { shot: Shot }) {
  if (shot.qa) {
    const pass = shot.qa.verdict === "pass";
    return (
      <span
        className={clsx(
          "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[0.6rem] font-semibold",
          pass ? "bg-kinora-ok/15 text-kinora-ok" : "bg-kinora-danger/15 text-kinora-danger",
        )}
        title={shot.qa.reason ?? (pass ? "QA passed" : "QA failed")}
      >
        CCS {shot.qa.ccs.toFixed(2)}
      </span>
    );
  }
  const labels: Record<string, string> = {
    planned: "planned",
    keyframed: "keyframe",
    rendering: "rendering",
    accepted: "ready",
    degraded: "degraded",
    failed: "failed",
  };
  return (
    <span className="rounded-full bg-white/10 px-1.5 py-0.5 text-[0.6rem] font-medium text-kinora-muted">
      {labels[shot.status] ?? shot.status}
    </span>
  );
}

export function ShotTimeline({ shots, currentShotId, onSelect }: ShotTimelineProps) {
  const keyframesByShot = useEventsStore((s) => s.keyframesByShot);
  const clips = useEventsStore((s) => s.clips);

  if (shots.length === 0) {
    return <p className="text-sm text-kinora-muted">No shots planned yet.</p>;
  }

  return (
    <div className="scrollbar-thin flex gap-3 overflow-x-auto pb-2">
      {shots.map((shot) => {
        const thumb = keyframesByShot[shot.shot_id] ?? shot.keyframe_url;
        const ready = Boolean(clips[shot.shot_id]?.oss_url ?? shot.clip_url);
        const isCurrent = shot.shot_id === currentShotId;
        return (
          <button
            key={shot.shot_id}
            type="button"
            onClick={() => onSelect(shot)}
            className={clsx(
              "group relative w-32 shrink-0 overflow-hidden rounded-xl text-left ring-1 transition-all",
              isCurrent
                ? "ring-2 ring-kinora-iris"
                : "ring-white/10 hover:ring-kinora-iris/50",
            )}
          >
            <div
              className="relative aspect-video w-full"
              style={thumb ? undefined : { background: coverGradient(shot.shot_id) }}
            >
              {thumb ? (
                <img src={thumb} alt="" className="h-full w-full object-cover" loading="lazy" />
              ) : null}
              {ready ? (
                <span className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-kinora-ok shadow" />
              ) : null}
            </div>
            <div className="flex items-center justify-between gap-1 bg-kinora-ink/80 px-2 py-1.5">
              <span className="truncate text-[0.65rem] text-kinora-muted">
                {shot.shot_id.replace(/^shot_?/, "#")}
              </span>
              <QaBadge shot={shot} />
            </div>
          </button>
        );
      })}
    </div>
  );
}
