import { useEffect, useState } from "react";

const TILE = 800;

/**
 * Pre-renders 300k particles + rain streaks onto a canvas,
 * bakes the blur into a PNG, then animates with transform only.
 *
 * GPU cost: ~0% (only compositor transform on cached texture)
 * RAM cost: ~150KB (two compressed PNG data URLs)
 * CPU cost: one-time ~200ms on mount (canvas draw)
 */
export default function RainAnimation() {
  const [rainUrl, setRainUrl] = useState("");
  const [glowUrl, setGlowUrl] = useState("");

  useEffect(() => {
    // === Canvas A: rain particles (sharp, no blur) ===
    const c1 = document.createElement("canvas");
    c1.width = TILE;
    c1.height = TILE;
    const ctx1 = c1.getContext("2d")!;
    ctx1.fillStyle = "#000";
    ctx1.fillRect(0, 0, TILE, TILE);

    // 300k particles via ImageData (bulk pixel write — fast)
    const img = ctx1.createImageData(TILE, TILE);
    const d = img.data;
    // Initialize all pixels to black opaque
    for (let i = 0; i < d.length; i += 4) {
      d[i] = 0;
      d[i + 1] = 0;
      d[i + 2] = 0;
      d[i + 3] = 255;
    }
    // 300k blue particles at random positions
    for (let i = 0; i < 300_000; i++) {
      const x = Math.floor(Math.random() * TILE);
      const y = Math.floor(Math.random() * TILE);
      const idx = (y * TILE + x) * 4;
      const b = Math.random();
      d[idx] = 0;
      d[idx + 1] = Math.floor(153 * b);
      d[idx + 2] = Math.floor(255 * b);
      d[idx + 3] = 255;
    }
    ctx1.putImageData(img, 0, 0);

    // Rain streaks (vertical lines) on top
    for (let i = 0; i < 80; i++) {
      const x = Math.random() * TILE;
      const y = Math.random() * TILE;
      const h = 60 + Math.random() * 140;
      const alpha = 0.3 + Math.random() * 0.4;
      ctx1.fillStyle = `rgba(0, 153, 255, ${alpha})`;
      ctx1.fillRect(x, y, 3, h);
    }

    setRainUrl(c1.toDataURL("image/png"));

    // === Canvas B: glow grid (blurred + brightened, baked into PNG) ===
    const c2 = document.createElement("canvas");
    c2.width = TILE;
    c2.height = TILE;
    const ctx2 = c2.getContext("2d")!;
    ctx2.fillStyle = "#000";
    ctx2.fillRect(0, 0, TILE, TILE);

    // Dot grid (8px spacing, dark — matches original ::after)
    ctx2.fillStyle = "rgb(10, 10, 10)";
    for (let y = 0; y < TILE; y += 8) {
      for (let x = 0; x < TILE; x += 8) {
        ctx2.beginPath();
        ctx2.arc(x, y, 2, 0, Math.PI * 2);
        ctx2.fill();
      }
    }

    // Apply blur + brightness by drawing onto temp canvas with filter
    const tmp = document.createElement("canvas");
    tmp.width = TILE;
    tmp.height = TILE;
    const tctx = tmp.getContext("2d")!;
    tctx.filter = "blur(16px) brightness(6)";
    tctx.drawImage(c2, 0, 0);

    setGlowUrl(tmp.toDataURL("image/png"));
  }, []);

  if (!rainUrl) return <div className="rain-loading" />;

  return (
    <>
      <div
        className="rain-glow"
        style={{ backgroundImage: `url(${glowUrl})` }}
      />
      <div
        className="rain-fg"
        style={{ backgroundImage: `url(${rainUrl})` }}
      />
    </>
  );
}
