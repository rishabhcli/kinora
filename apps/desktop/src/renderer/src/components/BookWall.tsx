import type { CSSProperties } from "react";

interface Cover {
  title: string;
  author: string;
  bg: string;
  foil: string;
}

// Clothbound-classic covers: a deep cloth color with a gold/cream foil frame +
// centered serif — reads as a wall of real, beautiful books rather than blocks.
const COVERS: Cover[] = [
  { title: "Frankenstein", author: "Mary Shelley", bg: "#243042", foil: "#cdb88a" },
  { title: "Dracula", author: "Bram Stoker", bg: "#5e1a1f", foil: "#e0c483" },
  { title: "Moby-Dick", author: "Herman Melville", bg: "#143a44", foil: "#d8c690" },
  { title: "Jane Eyre", author: "Charlotte Brontë", bg: "#3a2440", foil: "#d9bd86" },
  { title: "The Odyssey", author: "Homer", bg: "#1d3a5e", foil: "#e3c987" },
  { title: "Pride & Prejudice", author: "Jane Austen", bg: "#4a3220", foil: "#e6cd92" },
  { title: "Great Expectations", author: "Charles Dickens", bg: "#27402e", foil: "#d6c489" },
  { title: "Crime & Punishment", author: "F. Dostoevsky", bg: "#5a1d1d", foil: "#dabd80" },
  { title: "Dorian Gray", author: "Oscar Wilde", bg: "#1f3b34", foil: "#dcc488" },
  { title: "Wuthering Heights", author: "Emily Brontë", bg: "#34404c", foil: "#cdbb8e" },
  { title: "Treasure Island", author: "R. L. Stevenson", bg: "#6b4a18", foil: "#f0dca0" },
  { title: "The Time Machine", author: "H. G. Wells", bg: "#1c4048", foil: "#d6c78f" },
  { title: "Don Quixote", author: "M. de Cervantes", bg: "#5a3420", foil: "#e8d199" },
  { title: "War & Peace", author: "Leo Tolstoy", bg: "#222f4e", foil: "#d8c186" },
  { title: "The Iliad", author: "Homer", bg: "#402038", foil: "#dbbf88" },
];

function CoverCard({ cover }: { cover: Cover }) {
  return (
    <div
      className="relative aspect-[2/3] w-full overflow-hidden rounded-[5px] shadow-cover"
      style={{ backgroundColor: cover.bg }}
    >
      <div className="absolute inset-0 bg-[linear-gradient(100deg,rgba(255,255,255,0.17),transparent_38%,transparent_70%,rgba(0,0,0,0.32))]" />
      <div
        className="absolute inset-[7%] rounded-[2px] border"
        style={{ borderColor: cover.foil, opacity: 0.55 }}
      />
      <div className="absolute inset-[7%] flex flex-col items-center justify-center gap-2.5 px-2 text-center">
        <div className="h-px w-7" style={{ backgroundColor: cover.foil, opacity: 0.7 }} />
        <p
          className="font-display text-[15px] font-semibold leading-[1.15]"
          style={{ color: cover.foil }}
        >
          {cover.title}
        </p>
        <div className="h-px w-7" style={{ backgroundColor: cover.foil, opacity: 0.7 }} />
        <p
          className="absolute bottom-1.5 text-[8px] uppercase tracking-[0.22em]"
          style={{ color: cover.foil, opacity: 0.72 }}
        >
          {cover.author}
        </p>
      </div>
    </div>
  );
}

/** A living wall of clothbound books scrolling in alternating directions. */
export function BookWall({ columns = 5 }: { columns?: number }) {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden bg-walnut">
      <div
        className="absolute inset-0 grid gap-6 px-6"
        style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
      >
        {Array.from({ length: columns }).map((_, col) => {
          const rotated = COVERS.slice(col).concat(COVERS.slice(0, col));
          const loop = rotated.concat(rotated);
          const duration = 70 + (col % 3) * 18;
          return (
            <div key={col} className="relative -my-28">
              <div
                className={col % 2 === 0 ? "col-up" : "col-down"}
                style={{ "--dur": `${duration}s` } as CSSProperties}
              >
                <div className="flex flex-col gap-6">
                  {loop.map((cover, j) => (
                    <CoverCard key={j} cover={cover} />
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      {/* Warm projector light from above + a gentle base — covers stay vivid,
          the glass card stays legible. */}
      <div className="absolute inset-0 bg-[radial-gradient(85%_55%_at_50%_-5%,rgba(224,134,58,0.22),transparent_55%)]" />
      <div className="absolute inset-0 bg-[radial-gradient(120%_90%_at_50%_50%,transparent_40%,rgba(15,10,6,0.5))]" />
      <div className="absolute inset-x-0 bottom-0 h-1/3 bg-gradient-to-t from-walnut-deep/85 to-transparent" />
    </div>
  );
}
