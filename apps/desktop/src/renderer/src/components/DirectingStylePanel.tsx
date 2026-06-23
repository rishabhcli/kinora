import { type DirectingPriorView, queryKeys } from "@kinora/core";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "../lib/api";
import { queryClient } from "../lib/queryClient";

/** Bias is clamped to ±1.5 on the backend; the meter fills relative to that. */
const MAX_BIAS = 1.5;

/** One learned prior: its plain-language label, an applied/leaning chip, a meter. */
function PriorRow({ prior }: { prior: DirectingPriorView }) {
  const pct = Math.min(1, Math.abs(prior.bias) / MAX_BIAS);
  const positive = prior.bias >= 0;
  return (
    <li className="flex flex-col gap-2 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className={`text-[13px] leading-snug ${prior.applied ? "text-white" : "text-white/60"}`}>
            {prior.label}
          </p>
          <p className="mt-0.5 text-[11px] capitalize text-white/40">{prior.detail}</p>
        </div>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${
            prior.applied
              ? "bg-ember-glow/20 text-ember-glow"
              : "bg-white/10 text-white/45"
          }`}
        >
          {prior.applied ? (prior.applied_value ?? "Applied") : "Leaning"}
        </span>
      </div>
      {/* A center-anchored meter: fills left for negative bias, right for positive. */}
      <div className="relative h-1 rounded-full bg-white/10">
        <span className="absolute left-1/2 top-1/2 h-2 w-px -translate-y-1/2 bg-white/25" />
        <span
          className={`absolute top-0 h-1 rounded-full ${prior.applied ? "bg-ember-glow" : "bg-white/35"}`}
          style={{
            width: `${(pct * 50).toFixed(1)}%`,
            left: positive ? "50%" : undefined,
            right: positive ? undefined : "50%",
          }}
        />
      </div>
    </li>
  );
}

/**
 * "Your directing style" — the §8.6 cross-session preference panel. Reads the
 * priors the Director's notes have taught (pacing / palette / framing) and shows
 * them in plain language, so the personalization the Cinematographer applies on
 * the next session is visible and resettable. Defaults to the reader's global
 * style; when opened with a `bookId` it can also show what that one book learned.
 * A frosted popover that closes on Escape or an outside click, like ThemePopover.
 */
export function DirectingStylePanel({
  onClose,
  bookId,
}: {
  onClose: () => void;
  bookId?: string;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [scope, setScope] = useState<"user" | "book">(bookId ? "book" : "user");
  const [confirming, setConfirming] = useState(false);
  const activeBookId = scope === "book" ? bookId : undefined;

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    const onPointer = (event: MouseEvent): void => {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener("keydown", onKey);
    const id = window.setTimeout(() => document.addEventListener("mousedown", onPointer), 0);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
      window.clearTimeout(id);
    };
  }, [onClose]);

  const styleQuery = useQuery({
    queryKey: queryKeys.directingStyle(activeBookId),
    queryFn: async () => {
      const result = activeBookId
        ? await api.GET("/api/books/{book_id}/prefs", {
            params: { path: { book_id: activeBookId } },
          })
        : await api.GET("/api/me/prefs");
      if (result.error || !result.data) throw new Error("Failed to load directing style");
      return result.data;
    },
  });

  const reset = useMutation({
    mutationFn: async () => {
      const result = activeBookId
        ? await api.DELETE("/api/books/{book_id}/prefs", {
            params: { path: { book_id: activeBookId } },
          })
        : await api.DELETE("/api/me/prefs");
      if (result.error) throw new Error("Failed to reset directing style");
    },
    onSuccess: () => {
      setConfirming(false);
      // A reset ripples across scopes: a book's signals feed the global rollup.
      void queryClient.invalidateQueries({ queryKey: ["prefs"] });
    },
  });

  const priors = styleQuery.data?.priors ?? [];

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Your directing style"
      className="popover no-drag absolute right-0 top-[calc(100%+12px)] z-50 w-[340px] origin-top p-4 text-white"
    >
      <span className="popover-arrow right-6 -top-[9px]" aria-hidden />

      <p className="px-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-white/45">
        Your directing style
      </p>
      <p className="mt-1 px-1 text-[12px] leading-relaxed text-white/55">
        Learned from your director notes. The Cinematographer applies these as
        defaults on the next session.
      </p>

      {bookId && (
        <div className="mt-3 flex rounded-lg bg-white/[0.06] p-0.5">
          {(["book", "user"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setScope(value)}
              aria-pressed={scope === value}
              className={`flex-1 rounded-[7px] px-3 py-1.5 text-[12px] transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow ${
                scope === value ? "bg-white text-walnut-deep" : "text-white/70 hover:bg-white/10"
              }`}
            >
              {value === "book" ? "This book" : "All books"}
            </button>
          ))}
        </div>
      )}

      <div className="my-3 h-px bg-white/10" />

      {styleQuery.isLoading ? (
        <div className="flex items-center gap-2 px-1 py-6 text-[12px] text-white/45">
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-[1.5px] border-white/30 border-t-white motion-reduce:animate-none" />
          Loading your style…
        </div>
      ) : styleQuery.isError ? (
        <p className="px-1 py-5 text-[12px] text-white/55">Couldn’t load your directing style.</p>
      ) : priors.length === 0 ? (
        <div className="px-1 py-4">
          <p className="text-[13px] text-white/70">No directing style learned yet.</p>
          <p className="mt-1.5 text-[12px] leading-relaxed text-white/45">
            Leave notes in the director bar — “slower”, “warmer”, “pull back
            wider” — and your taste becomes the default over time.
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-white/[0.07] px-1">
          {priors.map((prior) => (
            <PriorRow key={prior.kind} prior={prior} />
          ))}
        </ul>
      )}

      {priors.length > 0 && (
        <>
          <div className="my-2 h-px bg-white/10" />
          <div className="flex items-center justify-between px-1">
            <span className="text-[11px] text-white/40">
              {confirming ? "This can’t be undone." : "Start fresh"}
            </span>
            <button
              type="button"
              onClick={() => (confirming ? reset.mutate() : setConfirming(true))}
              disabled={reset.isPending}
              className={`rounded-lg px-3 py-1.5 text-[12px] font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow disabled:opacity-50 ${
                confirming
                  ? "bg-red-500/85 text-white hover:bg-red-500"
                  : "bg-white/10 text-white/80 hover:bg-white/20"
              }`}
            >
              {reset.isPending
                ? "Resetting…"
                : confirming
                  ? "Confirm reset"
                  : scope === "book"
                    ? "Reset this book"
                    : "Reset"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
