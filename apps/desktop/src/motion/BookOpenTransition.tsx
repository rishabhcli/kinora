import { useEffect, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, motion, useIsPresent } from "framer-motion";
import { useMotion } from "./MotionProvider";
import { flipFrom, heroCoverRect, type Rect } from "./useSharedElement";

/**
 * <BookOpenTransition> — the headline: a book lifts off the shelf and
 * becomes the film, then folds back onto the shelf when you leave.
 *
 * Division of labour (per the contract): THIS primitive owns the
 * shared-element TRAVEL — a cover clone flies from its on-shelf rect to
 * the reading room's centred hero box using transforms only. The reading
 * room (Agent 10) owns the REVEAL (its cover hinges open into the first
 * frame). We delay mounting the room until the travel lands, so the clone
 * hands off to the room's own closed cover at the exact same spot — no
 * flash, no jump.
 *
 * Phase machine:
 *   idle → travel (clone flies in, room not yet mounted)
 *        → settle (room mounts under the static clone; clone holds opaque)
 *        → open   (clone CROSS-DISSOLVES out as the room takes over)
 *        → close  (clone cross-dissolves in, flies back to the shelf slot,
 *                  room fades) → idle
 *
 * Reduced motion: no travel — the room mounts immediately (a clean fade).
 *
 * Consumer drives `open`; renders the room via the render-prop so we
 * control its mount timing:
 *
 *   <BookOpenTransition open={roomOpen} originRect={rect} cover={art}
 *      onClosed={() => setSelectedBook(null)}>
 *     {(opened) => <ReadingRoom book={opened ? book : null} onClose={close} />}
 *   </BookOpenTransition>
 */

export interface CoverArt {
  image?: string;
  gradient?: string;
}

export interface BookOpenTransitionProps {
  /** Whether the room should be open. */
  open: boolean;
  /** The on-shelf cover rect to morph from (and back to). */
  originRect: Rect | null;
  /** Cover art for the flying clone. */
  cover: CoverArt;
  /** Fires when the travel lands and the room becomes interactive. */
  onOpened?: () => void;
  /** Fires when the close morph finishes — consumer should drop the book. */
  onClosed?: () => void;
  /** Render-prop: receives whether the room should be mounted. */
  children: (opened: boolean) => ReactNode;
}

type Phase = "idle" | "travel" | "settle" | "open" | "close";

// How long the clone parks (opaque) over the freshly-mounted room before
// fading. The room's heavy mount (big tree + first video decode) happens
// during this STATIC hold — nothing is animating, so the stall is invisible.
const SETTLE_MS = 240;

const COVER_RADIUS = "3px 8px 8px 3px"; // matches the reading room's cover

export function BookOpenTransition({
  open,
  originRect,
  cover,
  onOpened,
  onClosed,
  children,
}: BookOpenTransitionProps) {
  const { reduced, spring } = useMotion();
  const [phase, setPhase] = useState<Phase>("idle");
  const origin = useRef<Rect | null>(null);
  const hero = useRef<Rect>(heroCoverRect());

  // Drive the phase machine off the `open` boolean.
  useEffect(() => {
    // Start (or RESTART from a close-in-flight, so re-tapping a book mid-close
    // isn't dropped — otherwise `open` stays true, the effect never re-fires,
    // and the room is stuck blank).
    if (open && (phase === "idle" || phase === "close")) {
      origin.current = originRect ?? heroCoverRect();
      hero.current = heroCoverRect();
      if (reduced) {
        setPhase("open");
        onOpened?.();
      } else {
        setPhase("travel");
      }
    } else if (!open && phase !== "idle" && phase !== "close") {
      if (reduced) {
        setPhase("idle");
        onClosed?.();
      } else {
        setPhase("close");
      }
    }
    // We intentionally key only on `open`: phase transitions are driven by
    // animation completion, not by re-running this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Settle: the room is mounted (and warming up) while the clone holds still
  // and opaque on top. Once it has had a beat to paint, advance to `open` —
  // which removes the clone from <AnimatePresence> and plays its cross-dissolve
  // EXIT. Driving the open hand-off on a timer (not the clone's
  // onAnimationComplete) keeps it deterministic even if the travel spring is
  // interrupted by a parent re-render.
  useEffect(() => {
    if (phase !== "settle") return;
    const id = window.setTimeout(() => {
      setPhase("open");
      onOpened?.();
    }, SETTLE_MS);
    return () => window.clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  // Room mounts at settle (covered by the static clone), stays through close.
  const opened = phase === "settle" || phase === "open" || phase === "close";
  // Clone is on screen while travelling, holding (settle), or returning.
  // At `open` it is REMOVED from <AnimatePresence>, which plays its exit
  // (cross-dissolve) and then UNMOUNTS it — no leftover clone/scrim remains.
  const showClone = phase === "travel" || phase === "settle" || phase === "close";

  const flip =
    origin.current && hero.current
      ? flipFrom(origin.current, hero.current)
      : { x: 0, y: 0, scale: 1 };

  const cloneTransition = spring("cinematic");

  return (
    <>
      {/* The room — mount gated by the morph so the hand-off is seamless.
          Opacity-only wrapper (safe for the room's fixed overlay); it
          dims out during the close flight. */}
      <motion.div
        animate={{ opacity: phase === "close" ? 0 : 1 }}
        transition={{ duration: reduced ? 0 : 0.24, ease: "linear" }}
        style={{ position: "relative", zIndex: 100 }}
      >
        {children(opened)}
      </motion.div>

      <AnimatePresence>
        {showClone && (
          <CloneLayer
            key="book-open-morph"
            phase={phase}
            flip={flip}
            hero={hero.current}
            cover={cover}
            cloneTransition={cloneTransition}
            onTravelLanded={() => {
              // travel → park over the mounting room (settle); the SETTLE_MS
              // timer then advances to `open`.
              if (phase === "travel") setPhase("settle");
            }}
            onFlewHome={() => {
              // close → the cover has flown back to its shelf slot. Drop the
              // book and go idle: that removes the layer from <AnimatePresence>,
              // which plays its exit cross-dissolve and UNMOUNTS it. Calling
              // onClosed here (cover landed) rather than after the exit keeps
              // the consumer's unmount in lock-step with the visible motion.
              setPhase("idle");
              onClosed?.();
            }}
          />
        )}
      </AnimatePresence>
    </>
  );
}

/**
 * The flying-clone layer (scrim + cover clone). Split into its own component
 * so it OWNS the cross-dissolve as an <AnimatePresence> exit: when the parent
 * stops rendering it (phase → "open" or "idle"), AnimatePresence fades the
 * whole layer (scrim + clone together) and then UNMOUNTS it. That is the fix
 * for the stuck-element bug — the previous version faded an outer wrapper while
 * the clone hung on its own un-changing animate target, and an interrupted
 * exit could leave the scrim/clone painted over the now-interactive room.
 *
 * `useIsPresent()` lets the inner cover skip its travel-complete callback while
 * the layer is exiting, so a mid-flight interruption can't mis-fire the machine.
 */
function CloneLayer({
  phase,
  flip,
  hero,
  cover,
  cloneTransition,
  onTravelLanded,
  onFlewHome,
}: {
  phase: Phase;
  flip: { x: number; y: number; scale: number };
  hero: Rect;
  cover: CoverArt;
  cloneTransition: ReturnType<ReturnType<typeof useMotion>["spring"]>;
  onTravelLanded: () => void;
  onFlewHome: () => void;
}) {
  const isPresent = useIsPresent();

  return (
    <motion.div
      className="fixed inset-0"
      // pointerEvents:none ALWAYS — even mid-fade the room beneath stays fully
      // interactive, so a slow/interrupted exit can never block clicks.
      style={{ zIndex: 1000, pointerEvents: "none" }}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      // The layer-level exit is the cross-dissolve: opening hands off to the
      // room, closing dims the scrim as the cover flies home.
      exit={{ opacity: 0 }}
      transition={{ duration: 0.32, ease: [0.4, 0, 0.2, 1] }}
    >
      {/* Focus scrim */}
      <div
        className="absolute inset-0"
        style={{ background: "rgba(8, 7, 6, 0.62)" }}
      />

      {/* The flying cover clone, parked on the hero box and
          transform-offset to the shelf slot, then released. Scale + a hair of
          lift give the open a cinematic "rise off the shelf" feel; transforms
          and opacity only (no layout properties) keep it on the GPU at 60fps. */}
      <motion.div
        className="absolute"
        style={{
          left: hero.left,
          top: hero.top,
          width: hero.width,
          height: hero.height,
          borderRadius: COVER_RADIUS,
          overflow: "hidden",
          background: cover.gradient ?? "#1a1814",
          boxShadow: "0 40px 90px -24px rgba(0,0,0,0.85)",
          transformOrigin: "center center",
          willChange: "transform, opacity",
        }}
        initial={
          phase === "close"
            ? { x: 0, y: 0, scale: 1, opacity: 0 }
            : { x: flip.x, y: flip.y, scale: flip.scale, opacity: 1 }
        }
        animate={
          phase === "close"
            ? { x: flip.x, y: flip.y, scale: flip.scale, opacity: 1 }
            : { x: 0, y: 0, scale: 1, opacity: 1 }
        }
        // On OPEN, the clone doesn't just dissolve in place — it rises and
        // scales up a touch as it hands off, so the cover reads as "becoming"
        // the film. (Transform/opacity only; close just fades with the layer.)
        exit={
          phase === "open"
            ? { scale: 1.06, opacity: 0, transition: { duration: 0.38, ease: [0.16, 1, 0.3, 1] } }
            : { opacity: 0, transition: { duration: 0.2, ease: "linear" } }
        }
        transition={
          phase === "close"
            ? { default: cloneTransition, opacity: { duration: 0.12 } }
            : cloneTransition
        }
        onAnimationComplete={() => {
          // Ignore completions fired while the layer is exiting (interrupted
          // in-flight springs) — only the live, present layer drives the machine.
          if (!isPresent) return;
          // travel → the cover has landed on the hero box: hand to settle.
          if (phase === "travel") onTravelLanded();
          // close → the cover has flown back to its shelf slot: drop the book.
          else if (phase === "close") onFlewHome();
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
        {/* Spine shadow, mirrors the shelf cover's edge. */}
        <div
          className="absolute inset-y-0 left-0"
          style={{ width: 14, background: "linear-gradient(90deg, rgba(0,0,0,0.4), transparent)" }}
        />
      </motion.div>
    </motion.div>
  );
}

export default BookOpenTransition;
