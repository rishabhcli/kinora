/** Cinematic visual for the login left panel.
 *  Layered warm gradients that slowly drift — like light from a film
 *  projector hitting a wall. No images, no fake books, just atmosphere. */
export default function BookWall() {
  return (
    <div
      aria-hidden
      className="pointer-events-none relative h-full w-full overflow-hidden"
      style={{ background: "#0d0b09" }}
    >
      {/* Layer 1 — deep base */}
      <div
        className="cinema-layer-1"
        style={{
          position: "absolute",
          inset: "-10%",
          background:
            "radial-gradient(ellipse 70% 80% at 30% 40%, rgba(90,60,35,0.12) 0%, transparent 55%), radial-gradient(ellipse 60% 70% at 75% 60%, rgba(50,40,55,0.10) 0%, transparent 50%)",
        }}
      />

      {/* Layer 2 — warm drift */}
      <div
        className="cinema-layer-2"
        style={{
          position: "absolute",
          inset: "-10%",
          background:
            "radial-gradient(ellipse 50% 60% at 50% 30%, rgba(180,130,60,0.06) 0%, transparent 60%), radial-gradient(ellipse 40% 50% at 20% 70%, rgba(60,50,40,0.08) 0%, transparent 50%)",
        }}
      />

      {/* Layer 3 — cool counter-balance */}
      <div
        className="cinema-layer-3"
        style={{
          position: "absolute",
          inset: "-10%",
          background:
            "radial-gradient(ellipse 45% 55% at 80% 20%, rgba(40,50,70,0.05) 0%, transparent 55%)",
        }}
      />

      {/* Vignette */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(110% 90% at 50% 50%, transparent 35%, rgba(8,7,6,0.5) 100%)",
        }}
      />
    </div>
  );
}
