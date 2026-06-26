import { useCallback, useEffect, useRef, useState } from "react";

// Read-aloud on the Web Speech API with word-level highlighting. `boundary`
// events give a charIndex into the utterance text; we map that to the word token
// containing it, so the active word can be highlighted in lockstep with speech.
// Electron is Chromium, so local-voice boundary events fire reliably.

export interface TtsToken {
  text: string;
  start: number; // char offset into the source text (inclusive)
  end: number; // char offset (exclusive)
  index: number;
}

/** Split text into non-whitespace word tokens carrying their char offsets. */
export function tokenizeWords(text: string): TtsToken[] {
  const tokens: TtsToken[] = [];
  const re = /\S+/g;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    tokens.push({ text: m[0], start: m.index, end: m.index + m[0].length, index: i++ });
  }
  return tokens;
}

/** The token containing `charIndex`, else the next token after it, else null. */
export function findTokenAtChar(tokens: TtsToken[], charIndex: number): TtsToken | null {
  let next: TtsToken | null = null;
  for (const t of tokens) {
    if (charIndex >= t.start && charIndex < t.end) return t;
    if (t.start >= charIndex && next === null) next = t;
  }
  return next;
}

export type TtsState = "idle" | "playing" | "paused";

export interface UseTtsOptions {
  getText: () => string;
  rate?: number;
  voiceURI?: string | null;
  onError?: (error: string) => void;
  onActiveWordChange?: (token: TtsToken | null) => void;
}

export interface UseTtsResult {
  supported: boolean;
  state: TtsState;
  activeWordIndex: number;
  tokens: TtsToken[];
  play: () => void;
  pause: () => void;
  resume: () => void;
  toggle: () => void;
  stop: () => void;
}

function getSynth(): SpeechSynthesis | null {
  if (typeof window === "undefined") return null;
  return window.speechSynthesis ?? null;
}
function getUtteranceCtor(): typeof SpeechSynthesisUtterance | null {
  if (typeof window === "undefined") return null;
  return window.SpeechSynthesisUtterance ?? null;
}

export function useTts(options: UseTtsOptions): UseTtsResult {
  const supported = getSynth() !== null && getUtteranceCtor() !== null;
  const [state, setStateRaw] = useState<TtsState>("idle");
  const [activeWordIndex, setActiveWordIndex] = useState(-1);
  const [tokens, setTokens] = useState<TtsToken[]>([]);
  const tokensRef = useRef<TtsToken[]>([]);
  const stateRef = useRef<TtsState>("idle");
  const optsRef = useRef(options);
  optsRef.current = options;

  const setState = useCallback((next: TtsState) => {
    stateRef.current = next;
    setStateRaw(next);
  }, []);

  const setActive = useCallback((idx: number) => {
    setActiveWordIndex(idx);
    const tok = idx >= 0 ? tokensRef.current[idx] ?? null : null;
    optsRef.current.onActiveWordChange?.(tok);
  }, []);

  const play = useCallback(() => {
    const synth = getSynth();
    const Utterance = getUtteranceCtor();
    if (!synth || !Utterance) return;
    const text = optsRef.current.getText() ?? "";
    const toks = tokenizeWords(text);
    tokensRef.current = toks;
    setTokens(toks);

    synth.cancel();
    const u = new Utterance(text);
    u.rate = optsRef.current.rate ?? 1;
    const vURI = optsRef.current.voiceURI;
    if (vURI) {
      const voice = synth.getVoices().find((v) => v.voiceURI === vURI);
      if (voice) u.voice = voice;
    }
    u.addEventListener("boundary", (e: SpeechSynthesisEvent) => {
      if (e.name && e.name !== "word") return; // skip sentence boundaries
      const tok = findTokenAtChar(tokensRef.current, e.charIndex ?? 0);
      setActive(tok ? tok.index : -1);
    });
    u.addEventListener("end", () => {
      setState("idle");
      setActive(-1);
    });
    u.addEventListener("error", (e: SpeechSynthesisErrorEvent) => {
      setState("idle");
      setActive(-1);
      optsRef.current.onError?.(e.error ?? "tts-error");
    });
    synth.speak(u);
    setState("playing");
    setActive(-1);
  }, [setActive, setState]);

  const pause = useCallback(() => {
    const synth = getSynth();
    if (!synth) return;
    synth.pause();
    setState("paused");
  }, [setState]);

  const resume = useCallback(() => {
    const synth = getSynth();
    if (!synth) return;
    synth.resume();
    setState("playing");
  }, [setState]);

  const stop = useCallback(() => {
    const synth = getSynth();
    if (!synth) return;
    synth.cancel();
    setState("idle");
    setActive(-1);
  }, [setActive, setState]);

  const toggle = useCallback(() => {
    const cur = stateRef.current;
    if (cur === "idle") play();
    else if (cur === "playing") pause();
    else resume();
  }, [play, pause, resume]);

  // Stop speaking if the component unmounts mid-utterance.
  useEffect(() => {
    return () => {
      getSynth()?.cancel();
    };
  }, []);

  return { supported, state, activeWordIndex, tokens, play, pause, resume, toggle, stop };
}
