// Minimal warm-up state. It reports the real pipeline without substituting
// preview footage or covering the room in feature chrome.
import { motion } from "framer-motion";
import type { MachineState } from "./machine";
import type { FilmSession } from "./useFilmSession";
import { warmupHeadline } from "./warmupModel";

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
  const latestCrew = session.crew[session.crew.length - 1];
  const subline =
    session.live && latestCrew
      ? `${latestCrew.agent}: ${latestCrew.message}`
      : state.error
        ? state.error
        : "Rendering the next scene.";

  return (
    <motion.div
      data-warmup
      className="absolute inset-0 z-20 grid place-items-center"
      initial={reduce ? { opacity: 1 } : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: reduce ? 0 : 0.4, ease: [0.22, 1, 0.36, 1] }}
      style={{ background: "rgba(10,9,8,0.9)" }}
      aria-live="polite"
    >
      <div className="w-[min(80vw,360px)] text-center">
        <div className="mb-5 flex items-center justify-center gap-1.5" aria-hidden>
          {[0, 1, 2].map((index) => (
            <motion.span
              key={index}
              className="block h-2 w-2 rounded-[2px]"
              style={{ background: "rgba(212,164,78,0.8)" }}
              animate={reduce ? undefined : { opacity: [0.25, 0.9, 0.25] }}
              transition={reduce ? undefined : { duration: 1.2, repeat: Infinity, delay: index * 0.18 }}
            />
          ))}
        </div>
        <p className="mb-2 text-[10px] uppercase text-kinora-muted">{bookTitle}</p>
        <h2 className="mb-2 font-serif text-xl font-semibold text-kinora-text">{warmupHeadline(state)}</h2>
        <p className="mx-auto max-w-[34ch] text-[12px] leading-relaxed text-kinora-muted">{subline}</p>
      </div>
    </motion.div>
  );
}
