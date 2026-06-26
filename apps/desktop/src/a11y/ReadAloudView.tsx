import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";
import { useTts } from "./tts";
import { useReducedMotionPref } from "./useReducedMotionPref";

// Renders page text as word spans and highlights the spoken word in lockstep
// with the TTS engine's boundary events. Self-contained (its own controls), and
// also the publishable primitive Agent 10 can mount inside the reading text-pane
// so highlighting tracks the same words the reader sees.

const controlsStyle: CSSProperties = { display: "flex", gap: "0.5rem", marginBottom: "0.75rem" };
const buttonStyle: CSSProperties = {
  background: "rgba(255,255,255,0.06)",
  border: "1px solid rgba(255,255,255,0.16)",
  color: "inherit",
  borderRadius: 10,
  padding: "0.4rem 0.9rem",
  cursor: "pointer",
};
const activeWordStyle: CSSProperties = {
  background: "rgba(244,201,122,0.35)",
  borderRadius: 4,
  boxShadow: "0 0 0 2px rgba(244,201,122,0.35)",
  transition: "background 120ms ease",
};

export interface ReadAloudViewProps {
  text: string;
  rate?: number;
  voiceURI?: string | null;
  className?: string;
  /** Show the built-in play/pause/stop controls (default true). */
  showControls?: boolean;
}

export function ReadAloudView({ text, rate, voiceURI, className, showControls = true }: ReadAloudViewProps) {
  const reduce = useReducedMotionPref();
  const { supported, state, activeWordIndex, tokens, toggle, stop } = useTts({
    getText: () => text,
    rate,
    voiceURI,
  });
  const activeRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (activeWordIndex >= 0 && activeRef.current) {
      activeRef.current.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
    }
  }, [activeWordIndex, reduce]);

  // Interleave whitespace gaps with word spans so the original text is preserved.
  let body: ReactNode = text;
  if (tokens.length > 0) {
    const nodes: ReactNode[] = [];
    let cursor = 0;
    for (const tok of tokens) {
      if (tok.start > cursor) nodes.push(text.slice(cursor, tok.start));
      const isActive = tok.index === activeWordIndex;
      nodes.push(
        <span
          key={tok.index}
          ref={
            isActive
              ? (el) => {
                  activeRef.current = el;
                }
              : undefined
          }
          aria-current={isActive ? "true" : undefined}
          className={isActive ? "tts-word tts-word--active" : "tts-word"}
          style={isActive ? activeWordStyle : undefined}
        >
          {tok.text}
        </span>,
      );
      cursor = tok.end;
    }
    if (cursor < text.length) nodes.push(text.slice(cursor));
    body = nodes;
  }

  const playLabel =
    state === "playing" ? "Pause read-aloud" : state === "paused" ? "Resume read-aloud" : "Read aloud";

  return (
    <div className={className}>
      {showControls && (
        <div role="group" aria-label="Read aloud controls" style={controlsStyle}>
          <button
            type="button"
            onClick={toggle}
            disabled={!supported}
            aria-pressed={state !== "idle"}
            style={{ ...buttonStyle, opacity: supported ? 1 : 0.5 }}
          >
            {playLabel}
          </button>
          {state !== "idle" && (
            <button type="button" onClick={stop} aria-label="Stop read-aloud" style={buttonStyle}>
              Stop
            </button>
          )}
        </div>
      )}
      <div data-testid="read-aloud-text" style={{ lineHeight: 1.7 }}>
        {body}
      </div>
      {!supported && (
        <p style={{ fontSize: "0.8rem", opacity: 0.65, marginTop: "0.5rem" }}>
          Read-aloud isn’t available in this environment.
        </p>
      )}
    </div>
  );
}
