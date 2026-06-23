import { type EvalReport } from "@kinora/core";

/** Turn a canon entity_key ("character_arwen", "lady-galadriel") into a label. */
function prettyName(key: string): string {
  return key
    .replace(/^(character|char|entity)[_-]/i, "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim();
}

interface CharRow {
  key: string;
  crew: number | null;
  baseline: number | null;
  weak: boolean;
}

function buildRows(report: EvalReport): CharRow[] {
  const { crew, baseline } = report.per_character_ccs;
  const keys = Array.from(new Set([...Object.keys(crew), ...Object.keys(baseline)]));
  const min = report.thresholds.ccs_min;
  return keys
    .map((key) => {
      const c = crew[key] ?? null;
      return { key, crew: c, baseline: baseline[key] ?? null, weak: c !== null && c < min };
    })
    // Weakest crew CCS first — those are the characters that need canon tuning.
    .sort((a, b) => (a.crew ?? Infinity) - (b.crew ?? Infinity));
}

function Score({ value, weak }: { value: number | null; weak?: boolean }) {
  if (value === null) return <span className="text-white/25">—</span>;
  return (
    <span className={`tabular-nums ${weak ? "text-rose-300" : "text-white/80"}`}>
      {value.toFixed(3)}
    </span>
  );
}

/**
 * Per-character Character Consistency Score across every shot a character
 * appears in (§13), crew vs baseline. Rows are ordered weakest-crew-first and
 * any below the pre-registered CCS gate are flagged — those are exactly the
 * characters whose canon (locked reference, appearance notes) wants tuning.
 */
export function PerCharacterCcsTable({ report }: { report: EvalReport }) {
  const rows = buildRows(report);
  const min = report.thresholds.ccs_min;
  const weakCount = rows.filter((r) => r.weak).length;

  if (rows.length === 0) {
    return (
      <p className="rounded-xl border border-white/8 bg-white/[0.02] p-4 text-[12px] text-white/45">
        No per-character CCS recorded for this run.
      </p>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-white/8">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="bg-white/[0.04] text-left text-[10.5px] uppercase tracking-wider text-white/45">
            <th className="px-3 py-2 font-medium">Character</th>
            <th className="px-3 py-2 text-right font-medium">Crew</th>
            <th className="px-3 py-2 text-right font-medium">Baseline</th>
            <th className="px-3 py-2 text-right font-medium">Δ</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const delta = row.crew !== null && row.baseline !== null ? row.crew - row.baseline : null;
            return (
              <tr
                key={row.key}
                className={`border-t border-white/5 ${row.weak ? "bg-rose-500/[0.07]" : ""}`}
              >
                <td className="px-3 py-2">
                  <span className="text-white/85" title={row.key}>
                    {prettyName(row.key)}
                  </span>
                  {row.weak && (
                    <span className="ml-2 rounded-full bg-rose-400/15 px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide text-rose-300">
                      tune canon
                    </span>
                  )}
                </td>
                <td className="px-3 py-2 text-right">
                  <Score value={row.crew} weak={row.weak} />
                </td>
                <td className="px-3 py-2 text-right">
                  <Score value={row.baseline} />
                </td>
                <td className="px-3 py-2 text-right">
                  {delta === null ? (
                    <span className="text-white/25">—</span>
                  ) : (
                    <span className={`tabular-nums ${delta >= 0 ? "text-emerald-300/90" : "text-rose-300"}`}>
                      {delta >= 0 ? "+" : ""}
                      {delta.toFixed(3)}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="border-t border-white/5 px-3 py-2 text-[10.5px] text-white/40">
        {weakCount > 0
          ? `${weakCount} character${weakCount === 1 ? "" : "s"} below the ≥${min.toFixed(2)} gate — tune their canon.`
          : `All characters clear the ≥${min.toFixed(2)} CCS gate.`}
      </p>
    </div>
  );
}
