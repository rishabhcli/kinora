import type { CanonStateResponse } from "@kinora/core";

interface CanonContinuitySectionProps {
  states: CanonStateResponse[];
}

function beatRange(state: CanonStateResponse): string {
  const to = state.valid_to_beat == null ? "now" : `beat ${state.valid_to_beat}`;
  return `beat ${state.valid_from_beat} → ${to}`;
}

function StateRow({ state }: { state: CanonStateResponse }) {
  return (
    <li className={`flex items-start gap-2 rounded-lg px-2.5 py-2 ${state.active ? "bg-white/[0.03]" : "bg-white/[0.015]"}`}>
      <span
        className={`mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full ${state.active ? "bg-emerald-400" : "bg-white/25"}`}
        aria-hidden
      />
      <div className="min-w-0 flex-1">
        <p className={`text-[12.5px] leading-snug ${state.active ? "text-white/85" : "text-white/45 line-through decoration-white/20"}`}>
          <span className="font-medium text-white/90">{state.subject_entity_key}</span>{" "}
          <span className="text-white/50">{state.predicate}</span>{" "}
          <span className="font-medium">{state.object_value}</span>
        </p>
        <p className="mt-0.5 font-mono text-[10px] text-white/35">{beatRange(state)}</p>
      </div>
      <span
        className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide ${
          state.active ? "bg-emerald-400/15 text-emerald-200" : "bg-white/8 text-white/45"
        }`}
      >
        {state.active ? "active" : "retired"}
      </span>
    </li>
  );
}

/**
 * The versioned continuity facts (§8.5) — the "forgetting" half of the canon
 * graph. Active facts are what the story currently believes; retired facts
 * (their interval closed) survive for time-travel reads but drop out of forward
 * generation. Read-only here: edits flow through the Continuity Supervisor.
 */
export function CanonContinuitySection({ states }: CanonContinuitySectionProps) {
  if (states.length === 0) return null;
  const active = states.filter((s) => s.active);
  const retired = states.filter((s) => !s.active);
  return (
    <section className="space-y-2.5">
      <div className="flex items-baseline justify-between px-0.5">
        <div>
          <span className="text-[12px] font-semibold uppercase tracking-[0.16em] text-white/55">Continuity</span>
          <span className="ml-2 text-[11px] text-white/30">Versioned facts · forgetting (§8.5)</span>
        </div>
        <span className="font-mono text-[11px] text-white/35">
          {active.length}
          {retired.length > 0 ? ` · ${retired.length} retired` : ""}
        </span>
      </div>
      <ul className="space-y-1.5">
        {active.map((s) => (
          <StateRow key={s.id} state={s} />
        ))}
        {retired.map((s) => (
          <StateRow key={s.id} state={s} />
        ))}
      </ul>
    </section>
  );
}
