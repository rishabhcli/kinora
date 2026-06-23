/**
 * Region-select capture helpers (§5.4 pointer-based commenting) — the DOM half.
 *
 * The normalized-coordinate geometry is the pure `contentNormFromPixels` in
 * `@kinora/core` (tested there); this module wraps it with the live element
 * measurement and the best-effort PNG screenshot. The PNG is genuinely optional:
 * cross-origin clip URLs (presigned OSS/MinIO) taint a canvas unless the bucket
 * sends CORS headers, so export degrades to `null` and the regen still fires on
 * the note + shot id alone (the backend classifier reads the text, not pixels).
 */
import { contentNormFromPixels, type NormBox, type PixelBox } from "@kinora/core";

export type { NormBox, PixelBox } from "@kinora/core";

/** Longest edge of the exported PNG — keeps the comment payload small. */
const MAX_PNG_EDGE = 768;
/** Give up on a frame sample after this long (a stalled/blocked load). */
const EXPORT_TIMEOUT_MS = 4000;

/**
 * Map a pixel box drawn over the displayed (`object-contain`) video element to
 * normalized coordinates over the real video content. Returns `null` when the
 * video has no intrinsic size yet or the box collapses to nothing.
 */
export function elementBoxToContentNorm(
  video: HTMLVideoElement,
  box: PixelBox,
): NormBox | null {
  const rect = video.getBoundingClientRect();
  return contentNormFromPixels(rect.width, rect.height, video.videoWidth, video.videoHeight, box);
}

/**
 * Best-effort: render the boxed region of a clip frame to a base64 PNG (no
 * `data:` prefix). Resolves `null` if the frame can't be sampled — a tainted
 * (non-CORS) source, a load error, or a timeout. Never throws.
 */
export async function exportRegionPng(
  clipUrl: string,
  timeS: number,
  box: NormBox,
): Promise<string | null> {
  return new Promise<string | null>((resolve) => {
    const video = document.createElement("video");
    let done = false;
    const finish = (value: string | null): void => {
      if (done) return;
      done = true;
      window.clearTimeout(timer);
      video.removeAttribute("src");
      video.load();
      resolve(value);
    };
    const timer = window.setTimeout(() => finish(null), EXPORT_TIMEOUT_MS);

    video.crossOrigin = "anonymous";
    video.muted = true;
    video.preload = "auto";
    video.playsInline = true;
    video.onerror = () => finish(null);
    video.onloadeddata = () => {
      // Seek to the same frame the Director is looking at; clamp into range.
      const t = Math.min(Math.max(timeS, 0), Math.max((video.duration || timeS) - 0.05, 0));
      if (Math.abs(video.currentTime - t) < 0.01) video.dispatchEvent(new Event("seeked"));
      else video.currentTime = t;
    };
    video.onseeked = () => {
      try {
        const vw = video.videoWidth;
        const vh = video.videoHeight;
        const sx = Math.round(box.x * vw);
        const sy = Math.round(box.y * vh);
        const sw = Math.max(1, Math.round(box.w * vw));
        const sh = Math.max(1, Math.round(box.h * vh));
        const edgeScale = Math.min(1, MAX_PNG_EDGE / Math.max(sw, sh));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(sw * edgeScale));
        canvas.height = Math.max(1, Math.round(sh * edgeScale));
        const ctx = canvas.getContext("2d");
        if (!ctx) return finish(null);
        ctx.drawImage(video, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL("image/png"); // throws if the canvas is tainted
        finish(dataUrl.split(",", 2)[1] ?? null);
      } catch {
        finish(null); // cross-origin without CORS — degrade to a note-only comment
      }
    };
    video.src = clipUrl;
  });
}
