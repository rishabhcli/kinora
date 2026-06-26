import { useEffect, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
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
 *        → open   (room mounts + hinges; clone fades out as hand-off)
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
    if (open && phase === "idle") {
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
  // and opaque on top. Once it has had a beat to paint, fade the clone.
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
        style={{ position: "relative", zIndex: 1 }}
      >
        {children(opened)}
      </motion.div>

      <AnimatePresence>
        {showClone && (
          <motion.div
            key="book-open-morph"
            className="fixed inset-0"
            style={{ zIndex: 1000, pointerEvents: "none" }}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2, ease: "linear" }}
          >
            {/* Focus scrim */}
            <div
              className="absolute inset-0"
              style={{ background: "rgba(8, 7, 6, 0.62)" }}
            />

            {/* The flying cover clone, parked on the hero box and
                transform-offset to the shelf slot, then released. */}
            <motion.div
              className="absolute"
              style={{
                left: hero.current.left,
                top: hero.current.top,
                width: hero.current.width,
                height: hero.current.height,
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
              transition={
                phase === "close"
                  ? { default: cloneTransition, opacity: { duration: 0.12 } }
                  : cloneTransition
              }
              onAnimationComplete={() => {
                // travel → park over the mounting room (settle); the timer
                // takes it to `open`. close → tell the consumer to drop the book.
                if (phase === "travel") setPhase("settle");
                else if (phase === "close") {
                  setPhase("idle");
                  onClosed?.();
                }
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
        )}
      </AnimatePresence>
    </>
  );
}

export default BookOpenTransition;
