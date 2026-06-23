import { kenBurnsPreset, kenBurnsTempo, type SyncEngine } from "@kinora/core";
import { type CSSProperties, useCallback, useEffect, useState, useSyncExternalStore } from "react";

export type DegradedVariant = "keyframe" | "illustration" | "audio_text";

interface DegradedStageProps {
  /** The still to pan — a generated keyframe or the book's page image. */
  stillUrl: string | null;
  variant: DegradedVariant;
  /** Beat id — seeds the deterministic Ken-Burns move (stable across re-reads). */
  seed: string | null;
  budgetRemaining: number | null;
  underBudgetPressure: boolean;
  /**
   * The engine — optional, but when supplied the pan adapts to reading velocity
   * (§4.6) and a still whose URL 404s/expires drops itself so the ladder walks
   * down a rung (§12.4) instead of showing a broken image.
   */
  engine?: SyncEngine;
  /** Freeze the pan while the room is idle / the tab is hidden (§4.7). */
  paused?: boolean;
}

const LABEL: Record<DegradedVariant, string> = {
  keyframe: "Composing the next shot",
  illustration: "Reading ahead",
  audio_text: "Reading ahead",
};

const SUBLABEL: Record<DegradedVariant, string> = {
  keyframe: "Holding on the scene's keyframe while the film renders a few seconds ahead.",
  illustration: "Showing the book's own art while the crew renders this scene.",
  audio_text: "The narration carries you — the film catches up as you read.",
};

/** The rung's quality dot — a legible cue for which representation is on screen. */
const DOT: Record<DegradedVariant, string> = {
  keyframe: "bg-ember-glow",
  illustration: "bg-sky-300",
  audio_text: "bg-white/50",
};

const ANNOUNCE: Record<DegradedVariant, string> = {
  keyframe: "Showing a preview still while the film renders.",
  illustration: "Showing the book's illustration while the film renders.",
  audio_text: "Narrated read-along; the film is rendering.",
};

/** Subscribe to the engine's reading velocity (0 when no engine is wired). */
function useEngineVelocity(engine?: SyncEngine): number {
  const subscribe = useCallback(
    (cb: () => void) => engine?.subscribe(cb) ?? (() => {}),
    [engine],
  );
  const getVelocity = useCallback(() => engine?.getSnapshot().velocity ?? 0, [engine]);
  return useSyncExternalStore(subscribe, getVelocity);
}

/**
 * The §12.4 degradation ladder, client side. When the reader reaches a beat whose
 * committed clip isn't ready, we never stall on a spinner — we pan the beat's
 * keyframe (or the book's own page image) with a slow CSS Ken-Burns move at
 * **zero generation cost** (§4.4). Enhancements that make it feel premium:
 *
 * - **decode-ahead** — the still is preloaded; it only swaps in once decoded, so
 *   the bridge never flashes empty (it fades in over the cinematic ground);
 * - **velocity-adaptive** (§4.6) — the pan calms/slows as the reader quickens and
 *   freezes entirely on a skim;
 * - **idle-pause** (§4.7) — the perpetual pan rests when the room is idle/hidden;
 * - **self-healing** — a still that fails to load drops itself so the ladder
 *   steps down a rung rather than showing a broken image;
 * - **accessible** — the rung is announced politely to assistive tech.
 */
export function DegradedStage({
  stillUrl,
  variant,
  seed,
  budgetRemaining,
  underBudgetPressure,
  engine,
  paused = false,
}: DegradedStageProps) {
  const preset = kenBurnsPreset(seed);
  const velocity = useEngineVelocity(engine);
  const tempo = kenBurnsTempo(velocity);

  // Decode-ahead: only show a still once it has actually loaded, so the bridge
  // fades in over the dark ground rather than flashing an empty/broken frame.
  const [displayedUrl, setDisplayedUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!stillUrl) {
      setDisplayedUrl(null);
      return;
    }
    if (stillUrl === displayedUrl) return;
    let cancelled = false;
    const img = new Image();
    img.onload = () => {
      if (!cancelled) setDisplayedUrl(stillUrl);
    };
    img.onerror = () => {
      // 404 / expired presigned URL → drop it; the ladder falls to the next rung.
      if (!cancelled) engine?.dropCurrentStill();
    };
    img.src = stillUrl;
    return () => {
      cancelled = true;
    };
  }, [stillUrl, displayedUrl, engine]);

  // Decode-ahead: warm the next few beats' stills into the browser cache so the
  // bridge is instant when the reader arrives there too (§4.4).
  useEffect(() => {
    if (!engine) return;
    const imgs = engine.upcomingStillUrls(3).map((url) => {
      const img = new Image();
      img.src = url;
      return img;
    });
    return () => {
      for (const img of imgs) {
        img.onload = null;
        img.onerror = null;
      }
    };
  }, [engine, seed, displayedUrl]);

  const frozen = paused || tempo.paused;
  const kbVars = {
    "--kb-from-scale": String(preset.fromScale),
    "--kb-to-scale": String(preset.toScale),
    "--kb-from-x": `${preset.fromX * 100}%`,
    "--kb-to-x": `${preset.toX * 100}%`,
    "--kb-from-y": `${preset.fromY * 100}%`,
    "--kb-to-y": `${preset.toY * 100}%`,
    // Velocity-adaptive duration: a calmer, slower drift as the pace quickens.
    "--kb-dur": `${(preset.durationS * tempo.durationScale).toFixed(1)}s`,
    animationPlayState: frozen ? "paused" : "running",
  } as CSSProperties;

  return (
    <div className="absolute inset-0 overflow-hidden bg-[radial-gradient(120%_100%_at_50%_0%,#1c130c,#0b0705_72%)]">
      {displayedUrl ? (
        <div
          // Keyed so each new still fades in cleanly; it's already decoded, so the
          // fade reveals a real frame (never an empty/loading box).
          key={displayedUrl}
          className="absolute inset-0"
          style={{ animation: "kinora-fade-in 320ms ease both" }}
        >
          <img
            src={displayedUrl}
            alt=""
            aria-hidden
            draggable={false}
            className="ken-burns h-full w-full object-cover"
            style={kbVars}
          />
        </div>
      ) : (
        // Bottom rung / not-yet-loaded: a calm ember motif (never a spinner).
        <div className="absolute inset-0 flex items-center justify-center motion-reduce:hidden">
          {!frozen && (
            <span className="absolute h-24 w-24 animate-ping rounded-full bg-ember/15 [animation-duration:3s]" />
          )}
          <span className="h-12 w-12 rounded-full bg-ember/20 ring-1 ring-ember-glow/25" />
        </div>
      )}

      {/* Cinematic letterbox + a soft warm sweep so a still reads as a held shot. */}
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[7%] bg-black/55" />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[7%] bg-black/55" />
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_90%_at_50%_45%,transparent_55%,rgba(0,0,0,0.45))]" />
      {displayedUrl && !frozen && (
        <div className="shimmer pointer-events-none absolute inset-0 opacity-25 motion-reduce:hidden" />
      )}

      {/* Rung chip, bottom-left — a legible quality cue, announced to assistive tech. */}
      <div className="absolute inset-x-0 bottom-0 flex items-end justify-between gap-3 p-4">
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 rounded-full bg-black/45 px-3 py-1.5 backdrop-blur-sm"
        >
          <span className="relative flex h-2 w-2">
            {!frozen && (
              <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${DOT[variant]} opacity-60`} />
            )}
            <span className={`relative inline-flex h-2 w-2 rounded-full ${DOT[variant]}`} />
          </span>
          <span className="font-sans text-[12px] font-medium text-parchment/90">{LABEL[variant]}</span>
          <span className="sr-only">{ANNOUNCE[variant]}</span>
        </div>
        {underBudgetPressure && (
          <span className="rounded-full bg-amber-400/15 px-2.5 py-1 text-[11px] font-medium text-amber-200 backdrop-blur-sm">
            {budgetRemaining !== null
              ? `Saving film — ${Math.max(0, Math.round(budgetRemaining))}s left`
              : "Saving film budget"}
          </span>
        )}
      </div>

      {/* The sublabel sits centered-low only when there is no still to obscure. */}
      {!displayedUrl && (
        <div className="absolute inset-x-0 top-[46%] px-6 text-center">
          <p className="font-display text-[17px] text-parchment">{LABEL[variant]}</p>
          <p className="mx-auto mt-1.5 max-w-[16rem] text-[13px] leading-relaxed text-white/45">
            {SUBLABEL[variant]}
          </p>
        </div>
      )}
    </div>
  );
}
