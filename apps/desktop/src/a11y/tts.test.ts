import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { tokenizeWords, findTokenAtChar, useTts } from "./tts";

describe("tokenizeWords", () => {
  it("splits into non-whitespace tokens with char offsets", () => {
    const tokens = tokenizeWords("The quick brown");
    expect(tokens.map((t) => t.text)).toEqual(["The", "quick", "brown"]);
    expect(tokens[0]).toMatchObject({ start: 0, end: 3, index: 0 });
    expect(tokens[1]).toMatchObject({ start: 4, end: 9, index: 1 });
    expect(tokens[2]).toMatchObject({ start: 10, end: 15, index: 2 });
  });

  it("handles newlines and runs of whitespace", () => {
    const tokens = tokenizeWords("a\n\n  b");
    expect(tokens.map((t) => t.text)).toEqual(["a", "b"]);
    expect(tokens[1].start).toBe(5);
  });

  it("keeps punctuation attached to its word", () => {
    expect(tokenizeWords('"Hello," she said.').map((t) => t.text)).toEqual([
      '"Hello,"',
      "she",
      "said.",
    ]);
  });
});

describe("findTokenAtChar", () => {
  const tokens = tokenizeWords("The quick brown fox");
  it("returns the token containing the char index", () => {
    expect(findTokenAtChar(tokens, 0)?.text).toBe("The");
    expect(findTokenAtChar(tokens, 5)?.text).toBe("quick");
    expect(findTokenAtChar(tokens, 16)?.text).toBe("fox");
  });
  it("returns the next token when the index lands in whitespace", () => {
    expect(findTokenAtChar(tokens, 3)?.text).toBe("quick"); // the space after 'The'
  });
  it("returns null past the end", () => {
    expect(findTokenAtChar(tokens, 999)).toBeNull();
  });
});

// ---- useTts (mocked Web Speech API) -----------------------------------------

type Handler = (e: unknown) => void;
class MockUtterance {
  text: string;
  rate = 1;
  pitch = 1;
  volume = 1;
  lang = "";
  voice: SpeechSynthesisVoice | null = null;
  private listeners: Record<string, Handler[]> = {};
  constructor(text: string) {
    this.text = text;
  }
  addEventListener(type: string, cb: Handler) {
    (this.listeners[type] ||= []).push(cb);
  }
  removeEventListener(type: string, cb: Handler) {
    this.listeners[type] = (this.listeners[type] || []).filter((h) => h !== cb);
  }
  fire(type: string, e: unknown) {
    (this.listeners[type] || []).forEach((h) => h(e));
  }
}

let spoken: MockUtterance[] = [];
let synth: {
  speak: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
  pause: ReturnType<typeof vi.fn>;
  resume: ReturnType<typeof vi.fn>;
  getVoices: ReturnType<typeof vi.fn>;
};

function installSpeech() {
  spoken = [];
  synth = {
    speak: vi.fn((u: MockUtterance) => spoken.push(u)),
    cancel: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    getVoices: vi.fn(() => []),
  };
  (window as unknown as { speechSynthesis: unknown }).speechSynthesis = synth;
  (window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = MockUtterance;
}

beforeEach(() => installSpeech());
afterEach(() => {
  delete (window as unknown as { speechSynthesis?: unknown }).speechSynthesis;
  delete (window as unknown as { SpeechSynthesisUtterance?: unknown }).SpeechSynthesisUtterance;
});

describe("useTts", () => {
  it("reports supported when the Web Speech API exists", () => {
    const { result } = renderHook(() => useTts({ getText: () => "hello world" }));
    expect(result.current.supported).toBe(true);
    expect(result.current.state).toBe("idle");
    expect(result.current.activeWordIndex).toBe(-1);
  });

  it("play() speaks the page text at the configured rate", () => {
    const { result } = renderHook(() => useTts({ getText: () => "The quick brown fox", rate: 1.5 }));
    act(() => result.current.play());
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(spoken[0].text).toBe("The quick brown fox");
    expect(spoken[0].rate).toBe(1.5);
    expect(result.current.state).toBe("playing");
  });

  it("a word boundary highlights the matching token", () => {
    const { result } = renderHook(() => useTts({ getText: () => "The quick brown fox" }));
    act(() => result.current.play());
    act(() => spoken[0].fire("boundary", { name: "word", charIndex: 4 }));
    expect(result.current.activeWordIndex).toBe(1); // "quick"
    act(() => spoken[0].fire("boundary", { name: "word", charIndex: 16 }));
    expect(result.current.activeWordIndex).toBe(3); // "fox"
  });

  it("end returns to idle and clears the highlight", () => {
    const { result } = renderHook(() => useTts({ getText: () => "a b" }));
    act(() => result.current.play());
    act(() => spoken[0].fire("boundary", { name: "word", charIndex: 0 }));
    expect(result.current.activeWordIndex).toBe(0);
    act(() => spoken[0].fire("end", {}));
    expect(result.current.state).toBe("idle");
    expect(result.current.activeWordIndex).toBe(-1);
  });

  it("pause/resume/stop drive the engine and state", () => {
    const { result } = renderHook(() => useTts({ getText: () => "a b c" }));
    act(() => result.current.play());
    act(() => result.current.pause());
    expect(synth.pause).toHaveBeenCalled();
    expect(result.current.state).toBe("paused");
    act(() => result.current.resume());
    expect(synth.resume).toHaveBeenCalled();
    expect(result.current.state).toBe("playing");
    act(() => result.current.stop());
    expect(synth.cancel).toHaveBeenCalled();
    expect(result.current.state).toBe("idle");
    expect(result.current.activeWordIndex).toBe(-1);
  });

  it("is unsupported and inert without the Web Speech API", () => {
    delete (window as unknown as { speechSynthesis?: unknown }).speechSynthesis;
    const { result } = renderHook(() => useTts({ getText: () => "x" }));
    expect(result.current.supported).toBe(false);
    act(() => result.current.play());
    expect(result.current.state).toBe("idle");
  });
});
