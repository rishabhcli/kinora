/**
 * Browser export helpers for the metrics panel — turning the §13 proof into
 * artifacts a hackathon deck can use: a JSON download of the raw report, and a
 * rasterized PNG of the buffer-occupancy sawtooth (the chart everyone screenshots).
 * Kept DOM-only and out of `@kinora/core` (that stays framework-agnostic).
 */

/** Trigger a browser download of a Blob under `filename`. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke on the next tick so the click has a chance to start the download.
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Download arbitrary text (JSON, Markdown, …) as a file. */
export function downloadText(text: string, filename: string, type = "text/plain"): void {
  downloadBlob(new Blob([text], { type: `${type};charset=utf-8` }), filename);
}

/**
 * Rasterize a self-contained `<svg>` to a PNG Blob at `scale`×, on an opaque
 * walnut backdrop so the (transparent) chart reads on a light slide. The SVG
 * must carry its colors as inline attributes — a serialized SVG has no access to
 * the page stylesheet, which is why the chart uses inline `fill`/`stroke`.
 */
export function svgToPng(svg: SVGSVGElement, scale = 2): Promise<Blob> {
  const xml = new XMLSerializer().serializeToString(svg);
  const dataUrl = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`;
  const box = svg.viewBox.baseVal;
  const width = (box && box.width) || svg.clientWidth || 760;
  const height = (box && box.height) || svg.clientHeight || 240;

  return new Promise<Blob>((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      const ctx = canvas.getContext("2d");
      if (!ctx) return reject(new Error("no 2d context"));
      ctx.fillStyle = "#160e08"; // walnut-deep
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error("toBlob returned null"))),
        "image/png",
      );
    };
    img.onerror = () => reject(new Error("svg image failed to load"));
    img.src = dataUrl;
  });
}
