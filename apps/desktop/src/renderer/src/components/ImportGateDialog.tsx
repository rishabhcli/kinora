import { bookIsOpenable, importGateMessage, type BookResponse } from "@kinora/core";
import { useEffect, useRef } from "react";

/** Modal shown when the reader taps a book that is still importing or failed. */
export function ImportGateDialog({
  book,
  onClose,
}: {
  book: BookResponse;
  onClose: () => void;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const { title, body } = importGateMessage(book);
  const failed = book.status === "failed";

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-6 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-gate-title"
        className="glass max-w-md rounded-glass px-7 py-6 shadow-[0_24px_80px_-20px_rgba(0,0,0,0.75)]"
      >
        <div
          className={`mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-full ${
            failed ? "bg-rose-500/15 text-rose-300" : "bg-ember/15 text-ember-glow"
          }`}
        >
          {failed ? (
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 8v4M12 16h.01" />
            </svg>
          ) : (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none" />
          )}
        </div>
        <h2 id="import-gate-title" className="font-display text-center text-lg text-white">
          {title}
        </h2>
        <p className="mt-2 text-center text-sm leading-relaxed text-white/65">{body}</p>
        <p className="mt-3 text-center font-display text-sm text-white/85">{book.title}</p>
        <button
          type="button"
          onClick={onClose}
          className="mt-5 w-full rounded-xl bg-white/[0.12] px-4 py-2.5 text-sm font-medium text-white transition hover:bg-white/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow"
        >
          Back to the shelf
        </button>
      </div>
    </div>
  );
}
