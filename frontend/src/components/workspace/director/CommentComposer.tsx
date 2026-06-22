import { type FormEvent, useState } from "react";

import { CloseIcon, Spinner } from "../../common/icons";

interface CommentComposerProps {
  shotId: string | null;
  regionDataUrl: string | null;
  onClear: () => void;
  /** Posts the note (+ region) to the backend; returns when done. */
  onSubmit: (note: string) => Promise<void>;
}

/**
 * Attaches a natural-language note to a captured region (kinora.md §5.4). The
 * note + region pair is routed by the backend's intent classifier to the right
 * agent ("make her coat red" → Cinematographer + Continuity).
 */
export function CommentComposer({
  shotId,
  regionDataUrl,
  onClear,
  onSubmit,
}: CommentComposerProps) {
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!note.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(note.trim());
      setNote("");
      setDone(true);
      window.setTimeout(() => setDone(false), 2400);
      onClear();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send the note.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="glass rounded-2xl p-3">
      <div className="flex items-start gap-3">
        {regionDataUrl ? (
          <div className="relative shrink-0">
            <img
              src={regionDataUrl}
              alt="Selected region"
              className="h-16 w-24 rounded-lg object-cover ring-1 ring-white/15"
            />
            <button
              type="button"
              onClick={onClear}
              aria-label="Clear region"
              className="absolute -right-2 -top-2 flex h-5 w-5 items-center justify-center rounded-full bg-kinora-ink text-kinora-muted ring-1 ring-kinora-line hover:text-kinora-mist"
            >
              <CloseIcon className="h-3 w-3" />
            </button>
          </div>
        ) : (
          <div className="flex h-16 w-24 shrink-0 items-center justify-center rounded-lg border border-dashed border-kinora-line text-center text-[0.65rem] text-kinora-muted">
            Drag on the frame
          </div>
        )}
        <div className="min-w-0 flex-1">
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder={
              shotId ? "e.g. “Make her coat crimson”, “too fast”, “wrong room”" : "Select a shot first"
            }
            className="w-full resize-none rounded-lg border border-kinora-line bg-kinora-ink/60 px-3 py-2 text-sm text-kinora-mist outline-none placeholder:text-kinora-muted/60 focus:border-kinora-iris/70"
          />
          <div className="mt-2 flex items-center justify-between gap-2">
            <span className="truncate text-[0.7rem] text-kinora-muted">
              {shotId ? `Routing to the crew · shot ${shotId}` : "No shot targeted"}
            </span>
            <button
              type="submit"
              disabled={submitting || !note.trim() || !shotId}
              className="inline-flex items-center gap-1.5 rounded-full bg-[#6d28d9] px-3.5 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-[#7c5cff] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? <Spinner className="h-3.5 w-3.5" /> : null}
              Send note
            </button>
          </div>
          {error ? <p className="mt-1 text-xs text-kinora-danger">{error}</p> : null}
          {done ? <p className="mt-1 text-xs text-kinora-ok">Sent to the crew.</p> : null}
        </div>
      </div>
    </form>
  );
}
