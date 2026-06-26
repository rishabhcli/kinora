import { vi } from "vitest";

// Shared Web Speech API mock for tests. jsdom implements neither speechSynthesis
// nor SpeechSynthesisUtterance, so install a controllable fake.

type Handler = (e: unknown) => void;

export class MockUtterance {
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

export function installSpeech() {
  const spoken: MockUtterance[] = [];
  const synth = {
    speak: vi.fn((u: MockUtterance) => spoken.push(u)),
    cancel: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    getVoices: vi.fn(() => [] as SpeechSynthesisVoice[]),
  };
  (window as unknown as { speechSynthesis: unknown }).speechSynthesis = synth;
  (window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = MockUtterance;
  return {
    spoken,
    synth,
    uninstall() {
      delete (window as unknown as { speechSynthesis?: unknown }).speechSynthesis;
      delete (window as unknown as { SpeechSynthesisUtterance?: unknown }).SpeechSynthesisUtterance;
    },
  };
}
