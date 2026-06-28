// RegionCommentBar — the §5.4 Director region-comment input. A note typed here
// POSTs to `POST /api/sessions/{id}/comment` (REST), which CLASSIFIES the note
// and ENQUEUES a targeted regen of the shot. (The WS comment message only
// classifies — it does not regenerate; that is why this bar uses REST.)
//
// On success it surfaces how the note routed (which agent, what aspect) and any
// directing prior it taught (§8.6), so the Director sees the loop close.
import { useCallback, useId, useState } from "react";
import { ApiError } from "../../lib/api";
import { director, type CommentResponse } from "../../lib/api/director";

interface RegionCommentBarProps {
  sessionId: string | null;
  shotId: string;
  /** Fired when a comment successfully enqueues a regen, so the parent can mark
   *  the shot "re-rendering" and listen for the SSE `regen_done`/`agent_activity`. */
  onCommented?: (res: CommentResponse) => void;
  /** Quick-fill chips for common directing notes (faster than typing). */
  presets?: string[];
}

const DEFAULT_PRESETS = [
  "Slower, more lingering",
  "Wider establishing shot",
  "Warmer light",
  "Closer on the character's face",
  "Less motion, hold the frame",
];

export default function RegionCommentBar({
  sessionId,
  shotId,
  onCommented,
  presets = DEFAULT_PRESETS,
}: RegionCommentBarProps) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CommentResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();

  const submit = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || !sessionId || busy) return;
      setBusy(true);
      setError(null);
      setResult(null);
      try {
        const res = await director.comment(sessionId, { shot_id: shotId, note: trimmed });
        setResult(res);
        setNote("");
        onCommented?.(res);
      } catch (e) {
        setError(
          e instanceof ApiError
            ? e.status === 404
              ? "That shot is no longer part of this session."
              : `Comment failed (${e.status}).`
            : "Comment failed — please try again.",
        );
      } finally {
        setBusy(false);
      }
    },
    [sessionId, shotId, busy, onCommented],
  );

  const disabled = !sessionId;

  return (
    <div className="flex flex-col gap-2">
      {disabled && (
        <p className="text-[11px] text-kinora-muted">
          Start a session to direct this shot — comments regenerate the take.
        </p>
      )}

      <div className="flex flex-wrap gap-1.5">
        {presets.map((p) => (
          <button
            key={p}
            type="button"
            disabled={disabled || busy}
            onClick={() => void submit(p)}
            className="rounded-full px-2.5 py-1 text-[10.5px] font-medium transition-colors disabled:opacity-40"
            style={{
              background: "rgba(212,164,78,0.12)",
              color: "rgba(236,231,223,0.92)",
              border: "1px solid rgba(212,164,78,0.22)",
            }}
          >
            {p}
          </button>
        ))}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit(note);
        }}
        className="flex items-end gap-2"
      >
        <label htmlFor={inputId} className="sr-only">
          Direct this shot — a note re-renders the take
        </label>
        <textarea
          id={inputId}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={disabled || busy}
          rows={2}
          maxLength={2000}
          placeholder="Direct this shot — e.g. 'pull back, let it breathe'…"
          className="flex-1 resize-none rounded-xl px-3 py-2 text-[12px] text-kinora-text outline-none transition-all disabled:opacity-50"
          style={{
            background: "rgba(255,255,255,0.045)",
            border: "1px solid rgba(255,255,255,0.08)",
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit(note);
            }
          }}
        />
        <button
          type="submit"
          disabled={disabled || busy || !note.trim()}
          className="rounded-xl px-3.5 py-2 text-[11.5px] font-semibold transition-all disabled:opacity-40"
          style={{
            background: "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)",
            color: "#1a1408",
          }}
        >
          {busy ? "Sending…" : "Re-render"}
        </button>
      </form>

      {result && (
        <div
          className="rounded-xl px-3 py-2 text-[11px]"
          style={{ background: "rgba(52,211,153,0.08)", border: "1px solid rgba(52,211,153,0.2)" }}
        >
          <p className="text-kinora-text">
            Routed to <span className="font-semibold">{prettyAgent(result.agent)}</span> ({result.aspect}).{" "}
            {result.message}
          </p>
          {result.job_id && <p className="text-kinora-muted mt-1">Regen queued · job {result.job_id.slice(0, 8)}</p>}
          {result.learned.length > 0 && (
            <p className="text-kinora-muted mt-1">
              Noted — {result.learned.map((l) => l.label.toLowerCase()).join(", ")} will be the default.
            </p>
          )}
        </div>
      )}

      {error && (
        <p className="text-[11px]" style={{ color: "#f87171" }} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function prettyAgent(agent: string): string {
  return agent
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
