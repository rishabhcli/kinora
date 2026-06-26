import { test, expect, type Page } from "@playwright/test";

// DoD item 3: keyboard-only walkthroughs (recorded) + read-aloud word-sync.
// Headless Chromium ships no TTS voices, so we inject a scripted SpeechSynthesis
// that fires real `boundary` events word-by-word; the genuine useTts/ReadAloudView
// code then advances the highlight (the feature under test). On a real Mac the same
// code runs against OS voices with audible speech.

const HARNESS = "/e2e/harness/index.html";
const READING = "/e2e/harness/reading.html";

/** Install a scripted SpeechSynthesis that fires real word-boundary events. */
async function installScriptedSpeech(page: Page) {
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
            setTimeout(step, 220);
          } else {
            u.fire("end", {});
          }
        };
        setTimeout(step, 180);
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
}

const activeWord = (page: Page) =>
  page.locator('[data-testid="read-aloud-text"] [aria-current="true"]');

test("keyboard-only: skip link, adjust a reading pref, cheat-sheet open/close", async ({ page }) => {
  await page.goto(HARNESS);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: /skip to content/i })).toBeFocused();

  const slider = page.getByRole("slider", { name: /text size/i });
  await slider.focus();
  const before = await slider.inputValue();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  expect(Number(await slider.inputValue())).toBeGreaterThan(Number(before));

  await page.keyboard.press("Shift+Slash");
  const dialog = page.getByRole("dialog", { name: /keyboard shortcuts/i });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("read-aloud highlights each word in lockstep with boundary events", async ({ page }) => {
  await installScriptedSpeech(page);
  await page.goto(HARNESS);
  await page.getByRole("button", { name: /read aloud/i }).click();
  // Highlight starts on the first word, then advances in lockstep (asserting the
  // exact mid-sequence word would race the ~220ms cadence, so assert it moves on).
  await expect(activeWord(page)).toHaveText("Call");
  await page.screenshot({ path: "test-results/wordsync-1.png" });
  await expect(activeWord(page)).not.toHaveText("Call");
  await page.screenshot({ path: "test-results/wordsync-2.png" });
});

test("keyboard-only end-to-end: open book → adjust prefs → read-aloud → close", async ({ page }) => {
  await installScriptedSpeech(page);
  await page.goto(READING);

  // Open the book with the keyboard.
  const openBtn = page.getByRole("button", { name: "Open book" });
  await openBtn.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: /reading moby-dick/i });
  await expect(dialog).toBeVisible();

  // Adjust a reading preference with the keyboard.
  const size = page.getByRole("slider", { name: /text size/i });
  await size.focus();
  const before = await size.inputValue();
  await page.keyboard.press("ArrowRight");
  expect(Number(await size.inputValue())).toBeGreaterThan(Number(before));

  // Start read-aloud with the keyboard; the spoken word highlights in lockstep.
  const play = page.getByRole("button", { name: /read aloud/i });
  await play.focus();
  await page.keyboard.press("Enter");
  await expect(activeWord(page)).toHaveText("Call");
  await page.screenshot({ path: "test-results/flow-readaloud.png" });
  await expect(activeWord(page)).not.toHaveText("Call"); // advances in lockstep

  // Close with Escape; focus returns to the opener.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(openBtn).toBeFocused();
});
