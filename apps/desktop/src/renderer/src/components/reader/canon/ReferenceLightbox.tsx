import type { RefDraft } from "@kinora/core";
import { useEffect } from "react";

interface ReferenceLightboxProps {
  reference: RefDraft;
  onClose: () => void;
}

/**
 * A full-size view of one locked reference image (§8.1) — the identity frame the
 * Critic checks every dependent shot against. Inspect-only; Escape or a click
 * outside dismisses. Sits above the canon slide-over.
 */
export function ReferenceLightbox({ reference, onClose }: ReferenceLightboxProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-label="Reference image"
      className="fixed inset-0 z-[60] flex flex-col items-center justify-center bg-walnut-deep/85 p-10 backdrop-blur-sm"
      onClick={onClose}
    >
      <img
        src={reference.ossUrl}
        alt={reference.pose ?? "reference"}
        onClick={(e) => e.stopPropagation()}
        className="max-h-[80vh] max-w-[80vw] rounded-xl object-contain shadow-[0_30px_90px_-20px_rgba(0,0,0,0.8)] ring-1 ring-white/15"
      />
      <div className="mt-4 flex items-center gap-2.5 text-[12px] text-white/70">
        {reference.pose ? (
          <span className="rounded-full bg-white/10 px-2.5 py-1 font-medium text-white/85">{reference.pose}</span>
        ) : null}
        <span
          className={`rounded-full px-2.5 py-1 font-semibold ${
            reference.locked ? "bg-ember-glow/90 text-walnut-deep" : "bg-white/10 text-white/60"
          }`}
        >
          {reference.locked ? "Locked — canonical" : "Unlocked"}
        </span>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="Close reference"
        className="toolbar-btn no-drag absolute right-5 top-5"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
          <path d="m6 6 12 12M18 6 6 18" />
        </svg>
      </button>
    </div>
  );
}
