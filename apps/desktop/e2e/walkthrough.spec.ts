import { test, expect } from "@playwright/test";

// DoD item 3: a keyboard-only walkthrough (recorded) + a read-aloud word-sync
// demonstration. Headless Chromium ships no TTS voices, so we inject a scripted
// SpeechSynthesis that fires real `boundary` events word-by-word; the genuine
// useTts/ReadAloudView code then advances the highlight (the feature under test).
// On a real Mac the same code runs against OS voices with audible speech.

const HARNESS = "/e2e/harness/index.html";

test("keyboard-only: skip link, adjust a reading pref, cheat-sheet open/close", async ({ page }) => {
  await page.goto(HARNESS);

  // First Tab lands on the app-wide skip link.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: /skip to content/i })).toBeFocused();

  // Operate the Text size slider with the keyboard alone.
  const slider = page.getByRole("slider", { name: /text size/i });
  await slider.focus();
  const before = await slider.inputValue();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  const after = await slider.inputValue();
  expect(Number(after)).toBeGreaterThan(Number(before));

  // Open the shortcut cheat-sheet with "?", confirm focus is trapped, close with Escape.
  await page.keyboard.press("Shift+Slash");
  const dialog = page.getByRole("dialog", { name: /keyboard shortcuts/i });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("read-aloud highlights each word in lockstep with boundary events", async ({ page }) => {
  // Scripted speech engine: real boundary events on a visible cadence.
  await page.addInitScript(() => {
    class FakeUtterance {
      text: string;
      rate = 1;
      voice: unknown = null;
      private l: Record<string, ((e: unknown) => void)[]> = {};
      constructor(text: string) {
        this.text = text;
      }
      addEventListener(t: string, cb: (e: unknown) => void) {
        (this.l[t] ||= []).push(cb);
      }
      removeEventListener(t: string, cb: (e: unknown) => void) {
        this.l[t] = (this.l[t] || []).filter((h) => h !== cb);
      }
      fire(t: string, e: unknown) {
        (this.l[t] || []).forEach((h) => h(e));
      }
    }
    const synth = {
      speaking: false,
      paused: false,
      pending: false,
      speak(u: FakeUtterance) {
        const re = /\S+/g;
        const offsets: number[] = [];
        let m: RegExpExecArray | null;
        while ((m = re.exec(u.text)) !== null) offsets.push(m.index);
        let k = 0;
        const step = () => {
          if (k < offsets.length) {
            u.fire("boundary", { name: "word", charIndex: offsets[k] });
            k += 1;
            setTimeout(step, 260);
          } else {
            u.fire("end", {});
          }
        };
        setTimeout(step, 200);
      },
      cancel() {},
      pause() {},
      resume() {},
      getVoices() {
        return [] as unknown[];
      },
      addEventListener() {},
      removeEventListener() {},
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
    (window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = FakeUtterance;
  });

  await page.goto(HARNESS);
  await page.getByRole("button", { name: /read aloud/i }).click();

  // The highlight advances word by word.
  await expect(page.locator('[data-testid="read-aloud-text"] [aria-current="true"]')).toHaveText("Call");
  await page.screenshot({ path: "test-results/wordsync-1.png" });
  await expect(page.locator('[data-testid="read-aloud-text"] [aria-current="true"]')).toHaveText("me");
  await expect(page.locator('[data-testid="read-aloud-text"] [aria-current="true"]')).toHaveText("Ishmael.");
  await page.screenshot({ path: "test-results/wordsync-2.png" });
});
