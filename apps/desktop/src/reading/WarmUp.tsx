// WS3 — the warm-up affordance. While the film warms (especially with live video
// OFF, where clips are Ken-Burns/fallback) this presents an honest "preparing
// your film" state: phase-aware headline, a real step checklist, the live crew
// feed + buffered-ahead, and a skeleton — never a spinner-of-doom or dead air. It
// fades out the instant the film is revealed, resolving seamlessly into playback.
import { motion } from "framer-motion";
import { SkeletonShimmer } from "../components/SkeletonShimmer";
import { warmupHeadline, warmupSteps } from "./warmupModel";
import type { MachineState } from "./machine";
import type { FilmSession } from "./useFilmSession";

export function WarmUp({
  state,
  session,
  bookTitle,
  reduce,
}: {
  state: MachineState;
  session: FilmSession;
  bookTitle: string;
  reduce: boolean;
}) {
  const steps = warmupSteps(state, session.live);
  const latestCrew = session.crew[session.crew.length - 1];
  const subline =
    session.live && latestCrew
      ? `${latestCrew.agent}: ${latestCrew.message}`
      : state.error
        ? state.error
        : "Composing a vertical short film, a few seconds ahead of you.";

  return (
    <motion.div
      className="absolute inset-0 z-20 grid place-items-center"
      initial={reduce ? { opacity: 1 } : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: reduce ? 0 : 0.5, ease: [0.22, 1, 0.36, 1] }}
      style={{ background: "rgba(10,9,8,0.55)", backdropFilter: "blur(8px)", WebkitBackdropFilter: "blur(8px)" }}
      aria-live="polite"
    >
      <div className="glass-card w-[min(86vw,420px)] rounded-2xl p-7 text-center" style={{ background: "rgba(20,18,16,0.92)" }}>
        {/* Pulsing emblem — three film-cells breathing, not a doom spinner */}
        <div className="mb-5 flex items-center justify-center gap-1.5" aria-hidden>
          {[0, 1, 2].map((i) => (
            <motion.span
              key={i}
              className="block h-2.5 w-2.5 rounded-[3px]"
              style={{ background: "rgba(212,164,78,0.9)" }}
              animate={reduce ? undefined : { opacity: [0.3, 1, 0.3], scale: [0.9, 1.05, 0.9] }}
              transition={reduce ? undefined : { duration: 1.2, repeat: Infinity, delay: i * 0.18, ease: "easeInOut" }}
            />
          ))}
        </div>

        <p className="mb-1 text-[10px] uppercase tracking-[0.2em] text-kinora-muted">{bookTitle}</p>
        <h2 className="mb-1.5 font-serif text-xl font-semibold text-kinora-text">{warmupHeadline(state)}</h2>
        <p className="mx-auto mb-5 max-w-[34ch] text-[12px] leading-relaxed text-kinora-muted">{subline}</p>

        {/* Honest step checklist */}
        <div className="mb-5 space-y-2 text-left">
          {steps.map((s) => (
            <div key={s.label} className="flex items-center gap-2.5">
              <span
                className="grid h-4 w-4 flex-shrink-0 place-items-center rounded-full text-[9px]"
                style={{
                  background: s.done ? "rgba(52,211,153,0.9)" : "rgba(255,255,255,0.08)",
                  color: s.done ? "#06281c" : "transparent",
                  border: s.done ? "none" : "1px solid rgba(255,255,255,0.14)",
                }}
              >
                ✓
              </span>
              <span className="text-[12px]" style={{ color: s.done ? "rgba(232,226,216,0.95)" : "rgba(232,226,216,0.5)" }}>
                {s.label}
              </span>
            </div>
          ))}
        </div>

        {/* Live buffer / crew stats */}
        {session.live && (
          <div className="mb-4 flex items-center justify-center gap-3 text-[10px] text-kinora-muted">
            <span className="inline-flex items-center gap-1.5">
              <span
                className="inline-flex h-1.5 w-1.5 rounded-full"
                style={{ background: session.bursting ? "#fbbf24" : "#34d399", boxShadow: `0 0 6px ${session.bursting ? "#fbbf24" : "#34d399"}` }}
              />
              Buffered {Math.round(session.bufferAhead ?? 0)}s ahead
            </span>
            {session.inflight && <span>· {session.inflight.committed + session.inflight.speculative} rendering</span>}
            {session.zone && <span>· {session.zone}</span>}
          </div>
        )}

        {/* Texture — a shimmering line that reads as "in progress" */}
        <SkeletonShimmer className="mx-auto h-1.5 w-3/4 rounded-full" />
      </div>
    </motion.div>
  );
}
