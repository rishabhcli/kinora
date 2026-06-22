import { useEffect, useRef, useState } from "react";

import { sessions } from "../../../api/client";
import type { ConflictChoicePayload } from "../../../api/types";
import { useEventsStore } from "../../../stores/eventsStore";

interface AgentActivityFeedProps {
  sessionId: string;
}

const CONNECTION_LABEL: Record<string, string> = {
  idle: "idle",
  connecting: "connecting…",
  open: "live",
  closed: "closed",
  error: "reconnecting…",
};

function ConflictCard({
  conflict,
  sessionId,
}: {
  conflict: ConflictChoicePayload;
  sessionId: string;
}) {
  const resolveConflict = useEventsStore((s) => s.resolveConflict);
  const [busy, setBusy] = useState<string | null>(null);

  const choose = async (optionId: string) => {
    setBusy(optionId);
    try {
      await sessions.conflictChoice(sessionId, {
        conflict_id: conflict.conflict_id,
        option: optionId,
      });
      resolveConflict(conflict.conflict_id);
    } catch {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-xl border border-kinora-warn/40 bg-kinora-warn/10 p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-kinora-warn">
        Continuity conflict
      </p>
      {conflict.claim ? (
        <p className="mt-1 text-sm text-kinora-mist">{conflict.claim}</p>
      ) : null}
      {conflict.canon_fact ? (
        <p className="mt-1 text-xs text-kinora-muted">Canon says: {conflict.canon_fact}</p>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-2">
        {conflict.options.map((opt) => (
          <button
            key={opt.id}
            type="button"
            disabled={busy !== null}
            onClick={() => choose(opt.id)}
            className="rounded-full border border-kinora-line bg-kinora-ink/60 px-3 py-1.5 text-xs font-medium text-kinora-mist transition-colors hover:border-kinora-iris/60 disabled:opacity-50"
          >
            {opt.action}
            {typeof opt.cost_video_s === "number" ? (
              <span className="ml-1 text-kinora-muted">· {opt.cost_video_s}s</span>
            ) : null}
          </button>
        ))}
      </div>
    </div>
  );
}

export function AgentActivityFeed({ sessionId }: AgentActivityFeedProps) {
  const agentFeed = useEventsStore((s) => s.agentFeed);
  const conflicts = useEventsStore((s) => s.conflicts);
  const connection = useEventsStore((s) => s.connection);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [agentFeed.length]);

  return (
    <div className="flex h-full flex-col">
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-sm font-semibold text-kinora-mist">Agent activity</h4>
        <span className="inline-flex items-center gap-1.5 text-[0.7rem] text-kinora-muted">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              connection === "open"
                ? "bg-kinora-ok"
                : connection === "error"
                  ? "bg-kinora-warn"
                  : "bg-kinora-line"
            }`}
          />
          {CONNECTION_LABEL[connection] ?? connection}
        </span>
      </div>

      {conflicts.length > 0 ? (
        <div className="mb-3 space-y-2">
          {conflicts.map((c) => (
            <ConflictCard key={c.conflict_id} conflict={c} sessionId={sessionId} />
          ))}
        </div>
      ) : null}

      <div
        ref={logRef}
        className="scrollbar-thin min-h-[8rem] flex-1 space-y-1.5 overflow-y-auto rounded-xl bg-kinora-ink/40 p-3"
      >
        {agentFeed.length === 0 ? (
          <p className="text-sm text-kinora-muted">
            The crew is quiet. Agent messages and conflict resolutions stream here as the film
            generates.
          </p>
        ) : (
          agentFeed.map((evt) => {
            if (evt.type !== "agent_activity") return null;
            return (
              <div key={evt.id} className="animate-slide-in text-sm">
                <span className="font-medium text-kinora-iris">{evt.data.agent}</span>
                <span className="text-kinora-muted"> · </span>
                <span className="text-kinora-mist">{evt.data.message}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
