import { useEffect, useState } from "react";

/**
 * Blob Rain Animation — canvas pre-rendered, GPU-cheap.
 *
 * 1. Rain pattern drawn SHARP to 300x300 canvas A (matches original ::before)
 * 2. Canvas B: draw A 9 times (3x3) with ctx.filter=blur+brightness → blur baked into PNG
 * 3. PNG used as repeating background-image, animated with background-position (zero GPU)
 * 4. Hue shifting via mix-blend-mode: hue overlay (cheap color blend)
 * 5. Dot grid as static CSS (zero cost)
 *
 * No CSS filter, no backdrop-filter, no transform on filtered element.
 * The blur is pixels in a PNG — scrolling it costs nothing.
 */
export default function BlobRainAnimation() {
  const [bgUrl, setBgUrl] = useState<string>("");

  useEffect(() => {
    const W = 300;
    const H = 300;
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Black background
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, W, H);

    // Step 1: Draw rain pattern with manual glow on canvas A (300x300)
    // Use "lighter" compositing to additively build up brightness
    // Multiple widths simulate gaussian blur, additive buildup simulates brightness(9)
    ctx.globalCompositeOperation = "lighter";

    const streakPositions: Array<[number, number]> = [
      [0, 235], [300, 235],
      [0, 252], [300, 252],
      [0, 150], [300, 150],
      [0, 253], [300, 253],
      [0, 204], [300, 204],
      [0, 134], [300, 134],
      [0, 179], [300, 179],
      [0, 299], [300, 299],
      [0, 215], [300, 215],
      [0, 281], [300, 281],
      [0, 158], [300, 158],
      [0, 210], [300, 210],
    ];

    // Draw each streak as multiple layers: wide+dim → narrow+bright
    // This simulates blur(35px) brightness(9) without canvas filter
    const streakLayers = [
      { w: 80, a: 0.03 },
      { w: 60, a: 0.06 },
      { w: 40, a: 0.12 },
      { w: 24, a: 0.25 },
      { w: 12, a: 0.5 },
      { w: 6, a: 1.0 },
    ];

    for (const [x, y] of streakPositions) {
      for (const { w, a } of streakLayers) {
        ctx.fillStyle = `rgba(255, 170, 0, ${a})`;
        ctx.fillRect(x - w / 2, y, w, 100);
      }
    }

    const dotPositions: Array<[number, number]> = [
      [150, 117.5], [150, 126], [150, 75],
      [150, 126.5], [150, 102], [150, 67],
      [150, 89.5], [150, 149.5], [150, 107.5],
      [150, 140.5], [150, 79], [150, 105],
    ];

    const dotLayers = [
      { r: 40, a: 0.04 },
      { r: 25, a: 0.1 },
      { r: 15, a: 0.2 },
      { r: 8, a: 0.5 },
      { r: 4, a: 1.0 },
    ];

    for (const [x, y] of dotPositions) {
      for (const { r, a } of dotLayers) {
        ctx.fillStyle = `rgba(255, 30, 0, ${a})`;
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    ctx.globalCompositeOperation = "source-over";

    // Step 2: Draw canvas A onto canvas B 9 times (3x3) to handle tile edges
    // No filter needed — glow is already baked in via additive layers above
    const canvasB = document.createElement("canvas");
    canvasB.width = W;
    canvasB.height = H;
    const ctxB = canvasB.getContext("2d");
    if (!ctxB) return;
    ctxB.fillStyle = "#000";
    ctxB.fillRect(0, 0, W, H);
    for (let dx = -W; dx <= W; dx += W) {
      for (let dy = -H; dy <= H; dy += H) {
        ctxB.drawImage(canvas, dx, dy);
      }
    }

    setBgUrl(canvasB.toDataURL("image/png"));
  }, []);

  if (!bgUrl) {
    return <div className="absolute inset-0 bg-black" style={{ contain: "strict" }} />;
  }

  return (
    <div
      className="absolute inset-0 overflow-hidden bg-black"
      style={{ contain: "strict" }}
    >
      {/* Rain layer — pre-blurred PNG texture, scrolled with background-position
          Oversized + rotated like original ::before (inset:-145%, rotate:-45deg)
          Vertical scroll appears diagonal due to rotation — just like original */}
      <div
        style={{
          position: "absolute",
          inset: "-145%",
          transform: "rotate(-45deg)",
          backgroundImage: `url(${bgUrl})`,
          backgroundRepeat: "repeat",
          backgroundSize: "300px 300px",
          animation: "blob-rain-fall 6s linear infinite",
          willChange: "background-position",
        }}
      />

      {/* Hue overlay — animated solid color with mix-blend-mode: hue */}
      <div
        className="absolute inset-0"
        style={{
          zIndex: 1,
          mixBlendMode: "hue",
          animation: "blob-rain-hue 5s ease-in-out infinite",
        }}
      />

      {/* Dot grid overlay — static, sharp, on top (matches original ::after) */}
      <div
        className="absolute inset-0"
        style={{
          zIndex: 2,
          pointerEvents: "none",
          backgroundImage:
            "radial-gradient(circle at 50% 50%, #0000 0, #0000 2px, hsl(0 0 4%) 2px)",
          backgroundSize: "8px 8px",
        }}
      />

      <style>{`
        @keyframes blob-rain-fall {
          0% { background-position: 0px 0px; }
          100% { background-position: 0px 300px; }
        }
        @keyframes blob-rain-hue {
          0% { background-color: hsl(40, 100%, 50%); }
          25% { background-color: hsl(15, 100%, 50%); }
          28% { background-color: hsl(40, 100%, 50%); }
          32% { background-color: hsl(20, 100%, 50%); }
          39% { background-color: hsl(40, 100%, 50%); }
          40% { background-color: hsl(20, 100%, 50%); }
          41% { background-color: hsl(40, 100%, 50%); }
          42% { background-color: hsl(15, 100%, 50%); }
          44% { background-color: hsl(40, 100%, 50%); }
          58% { background-color: hsl(20, 100%, 50%); }
          64% { background-color: hsl(40, 100%, 50%); }
          80% { background-color: hsl(15, 100%, 50%); }
          to { background-color: hsl(40, 100%, 50%); }
        }
      `}</style>
    </div>
  );
}
