// ConflictPanel — the §7.2 Crew-dispute resolver. Lists the session's surfaced
// canon conflicts (from `GET /sessions/{id}/conflicts`) and lets the Director
// resolve each by POSTing `POST /sessions/{id}/conflict_choice`:
//   • honor_canon  — regenerate honouring the established canon
//   • evolve_canon — assert the new state, then regenerate
//   • surface_to_user — leave it surfaced (deferred)
// The Showrunner's arbitration reasoning streams back in the response (and on
// the SSE feed); resolved conflicts collapse to a one-line decision record.
import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../../lib/api";
import {
  director,
  type ConflictOption,
  type ConflictRecord,
} from "../../lib/api/director";

interface ConflictPanelProps {
  sessionId: string | null;
}

const OPTIONS: { id: ConflictOption; label: string; hint: string }[] = [
  { id: "honor_canon", label: "Honour canon", hint: "Regenerate without the contradiction" },
  { id: "evolve_canon", label: "Evolve canon", hint: "Assert the new state, then regenerate" },
  { id: "surface_to_user", label: "Defer", hint: "Leave it surfaced for now" },
];

export default function ConflictPanel({ sessionId }: ConflictPanelProps) {
  const [conflicts, setConflicts] = useState<ConflictRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    try {
      setConflicts(await director.getConflicts(sessionId));
      setError(null);
    } catch {
      setError("Couldn't load conflicts.");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const resolve = useCallback(
    async (conflictId: string, option: ConflictOption) => {
      if (!sessionId || busy) return;
      setBusy(conflictId);
      setError(null);
      try {
        const res = await director.resolveConflict(sessionId, { conflict_id: conflictId, option });
        // Optimistically reflect the decision; a reload confirms via history.
        setConflicts((prev) =>
          prev.map((c) =>
            c.conflict_id === conflictId
              ? { ...c, resolved: res.status !== "deferred", chosen_option: option, reasoning: res.reasoning }
              : c,
          ),
        );
        void load();
      } catch (e) {
        setError(e instanceof ApiError ? `Resolve failed (${e.status}).` : "Resolve failed.");
      } finally {
        setBusy(null);
      }
    },
    [sessionId, busy, load],
  );

  if (!sessionId) {
    return (
      <p className="text-[12px] text-kinora-muted py-8 text-center">
        Start a session to see and resolve continuity conflicts.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <p className="text-[11px] text-kinora-muted">
          {conflicts.length === 0 ? "No conflicts surfaced." : `${conflicts.filter((c) => !c.resolved).length} open`}
        </p>
        <button type="button" onClick={() => void load()} className="text-[10.5px] text-kinora-muted hover:text-kinora-text transition-colors">
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error && (
        <p className="text-[11px]" style={{ color: "#f87171" }} role="alert">
          {error}
        </p>
      )}

      <ul className="flex flex-col gap-3">
        {conflicts.map((c) => (
          <li
            key={c.conflict_id}
            className="rounded-xl p-3"
            style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${c.resolved ? "rgba(255,255,255,0.07)" : "rgba(248,113,113,0.3)"}` }}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-[12px] font-medium text-kinora-text">{c.claim ?? "Continuity conflict"}</p>
                {c.canon_fact && <p className="text-[10.5px] text-kinora-muted mt-0.5">Canon: {c.canon_fact}</p>}
                {c.raised_by && <p className="text-[9.5px] text-kinora-muted mt-0.5">Raised by {c.raised_by.replace(/_/g, " ")}</p>}
              </div>
              {c.resolved && (
                <span className="rounded-full px-2 py-0.5 text-[9px] font-medium" style={{ background: "rgba(52,211,153,0.16)", color: "#34d399", border: "1px solid rgba(52,211,153,0.3)" }}>
                  Resolved
                </span>
              )}
            </div>

            {c.resolved ? (
              c.reasoning && <p className="mt-2 text-[10.5px] text-kinora-text/85 italic">{c.reasoning}</p>
            ) : (
              <div className="mt-3 flex flex-wrap gap-2">
                {OPTIONS.map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    disabled={busy === c.conflict_id}
                    onClick={() => void resolve(c.conflict_id, o.id)}
                    title={o.hint}
                    className="rounded-lg px-2.5 py-1.5 text-[10.5px] font-medium transition-all disabled:opacity-40"
                    style={{
                      background: o.id === "honor_canon" ? "rgba(212,164,78,0.16)" : "rgba(255,255,255,0.05)",
                      color: "rgba(236,231,223,0.95)",
                      border: "1px solid rgba(255,255,255,0.12)",
                    }}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
