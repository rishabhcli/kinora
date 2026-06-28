// Built-in stand-in for Agent 4's <BookOpenTransition> (slot in slots.ts).
// The cover swings open on its spine and lifts away while the room dissolves in;
// closing reverses. Reduced motion → an elegant fade. Agent 12 swaps in the real
// motion primitive at integration.
//
// onOpened fires on a duration-matched timer (never hangs the reveal even if a
// frame callback is missed); onClosed fires when the exit animation completes.
import { motion, useIsPresent } from "framer-motion";
import { useEffect, useRef, type CSSProperties } from "react";
import type { BookOpenTransitionProps } from "../slots";

const SETTLE: [number, number, number, number] = [0.22, 1, 0.36, 1];
const HINGE: [number, number, number, number] = [0.66, 0, 0.2, 1];

const prefersReducedTransparency = () =>
  typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-transparency: reduce)").matches;

export function BuiltinBookOpenTransition({
  cover,
  reduce,
  onOpened,
  onClosed,
  children,
}: BookOpenTransitionProps) {
  const isPresent = useIsPresent();
  const openedRef = useRef(false);
  // Hold the latest callback in a ref so the open timer depends only on `reduce`
  // — otherwise an unstable onOpened identity (parent re-renders on every SSE
  // tick) would reset the timer and the reveal would never fire.
  const onOpenedRef = useRef(onOpened);
  onOpenedRef.current = onOpened;

  // The open choreography completes at a known time — reveal then, regardless of
  // whether any frame/animation callback fires. Re-arms on (re-)entry: if a rapid
  // same-book close→reopen interrupts the exit, AnimatePresence REUSES this
  // instance (isPresent flips back true) — without re-arming, onOpened/ANIM_READY
  // would never fire again and the room would freeze in the warm-up.
  useEffect(() => {
    if (!isPresent) return; // exiting — don't arm
    openedRef.current = false; // (re-)arm on entry / re-entry
    const ms = reduce ? 260 : 1050;
    const t = window.setTimeout(() => {
      if (!openedRef.current) {
        openedRef.current = true;
        onOpenedRef.current?.();
      }
    }, ms);
    return () => window.clearTimeout(t);
  }, [reduce, isPresent]);

  const noBlur = prefersReducedTransparency();

  return (
    <motion.div
      className="fixed inset-0 z-[100]"
      role="dialog"
      aria-modal="true"
      aria-label={cover.title ? `Reading ${cover.title}` : "Reading room"}
      initial="closed"
      animate="open"
      exit="closed"
      onAnimationComplete={() => {
        // Exit finished (AnimatePresence removed us) → close is complete.
        if (!isPresent) onClosed?.();
      }}
    >
      {/* Darkening + (static) blur backdrop */}
      <motion.div
        className="absolute inset-0"
        variants={{
          closed: { backgroundColor: "rgba(8,7,6,0)" },
          open: { backgroundColor: "rgba(8,7,6,0.78)" },
        }}
        transition={{ duration: reduce ? 0.25 : 0.6, ease: SETTLE }}
        style={noBlur ? undefined : { backdropFilter: "blur(20px)", WebkitBackdropFilter: "blur(20px)" }}
      />

      {/* The room — dissolves (and gently scales unless reduced motion) into place */}
      <motion.div
        className="absolute inset-0 flex flex-col kinora-bg"
        variants={{
          closed: { opacity: 0, scale: reduce ? 1 : 1.06 },
          open: { opacity: 1, scale: 1 },
        }}
        transition={{ duration: reduce ? 0.25 : 0.6, ease: SETTLE, delay: reduce ? 0.05 : 0.42 }}
        // .kinora-bg sets position:relative; force absolute back so inset-0 fills.
        style={{ transformOrigin: "center", position: "absolute" }}
      >
        {/* Aurora warm-light wash — mirrors login page's aurora blobs */}
        <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
          <div
            className="absolute"
            style={{
              width: 420,
              height: 420,
              top: "-8%",
              left: "-5%",
              borderRadius: "50%",
              filter: "blur(80px)",
              opacity: 0.4,
              background: "radial-gradient(circle, rgba(212,164,78,0.12), transparent 70%)",
            }}
          />
          <div
            className="absolute"
            style={{
              width: 380,
              height: 380,
              bottom: "-5%",
              right: "-8%",
              borderRadius: "50%",
              filter: "blur(80px)",
              opacity: 0.35,
              background: "radial-gradient(circle, rgba(120,90,50,0.10), transparent 70%)",
            }}
          />
        </div>
        {/* Film grain — same subtle noise as login */}
        <div
          className="pointer-events-none absolute inset-0"
          aria-hidden
          style={{
            opacity: 0.022,
            backgroundImage:
              "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)'/%3E%3C/svg%3E\")",
            zIndex: 1,
          }}
        />
        <div className="relative z-10 flex h-full flex-col">{children}</div>
      </motion.div>

      {/* The cover swings open on its spine, then lifts away (skipped on reduced motion) */}
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center" style={{ perspective: 2200 } as CSSProperties}>
        <motion.div
          className="relative"
          style={{
            width: "min(40vh, 300px)",
            aspectRatio: "2 / 3",
            transformStyle: "preserve-3d",
            transformOrigin: "left center",
          } as CSSProperties}
          variants={{
            closed: { rotateY: 0, opacity: 1 },
            open: reduce ? { rotateY: 0, opacity: 0 } : { rotateY: -168, opacity: 0 },
          }}
          transition={
            reduce
              ? { opacity: { duration: 0.22, ease: "linear" } }
              : { rotateY: { duration: 0.95, ease: HINGE, delay: 0.12 }, opacity: { duration: 0.25, ease: "linear", delay: 0.95 } }
          }
        >
          {/* Front cover face */}
          <div
            className="absolute inset-0 overflow-hidden"
            style={{
              background: cover.gradient,
              borderRadius: "3px 8px 8px 3px",
              backfaceVisibility: "hidden",
              boxShadow: "0 30px 60px -20px rgba(0,0,0,0.85)",
            }}
          >
            {cover.image && (
              <img
                src={cover.image}
                alt=""
                className="absolute inset-0 h-full w-full object-cover"
                onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
              />
            )}
            <div className="absolute inset-y-0 left-0" style={{ width: 14, background: "linear-gradient(90deg, rgba(0,0,0,0.4), transparent)" }} />
          </div>

          {/* Back cover face (inside of the front cover) — visible when rotated past 90° */}
          <div
            className="absolute inset-0 overflow-hidden"
            style={{
              background: "linear-gradient(135deg, #2a2520 0%, #1e1a16 100%)",
              borderRadius: "8px 3px 3px 8px",
              backfaceVisibility: "hidden",
              transform: "rotateY(180deg)",
              boxShadow: "inset 0 0 30px rgba(0,0,0,0.5)",
            }}
          >
            <div className="absolute inset-0 flex items-center justify-center">
              <span style={{ color: "rgba(212,164,78,0.3)", fontSize: 11, fontStyle: "italic", letterSpacing: "0.05em" }}>
                {cover.title}
              </span>
            </div>
          </div>
        </motion.div>
      </div>
    </motion.div>
  );
}
