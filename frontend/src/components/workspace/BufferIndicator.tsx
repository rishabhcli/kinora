import {
  bufferFillFraction,
  bufferHealth,
  HIGH_WATERMARK_S,
  LOW_WATERMARK_S,
  lowMarkFraction,
} from "../../lib/buffer";

interface BufferIndicatorProps {
  committedSecondsAhead: number;
  low?: number;
  high?: number;
}

const HEALTH_COLOR: Record<string, string> = {
  low: "bg-kinora-warn",
  ok: "bg-kinora-glow",
  full: "bg-kinora-ok",
};

/**
 * The deliberately subtle buffer hairline (kinora.md §5.3): a faint line that
 * fills toward the high watermark H, with a tick at the low watermark L. The
 * only surfacing of the generation machinery.
 */
export function BufferIndicator({
  committedSecondsAhead,
  low = LOW_WATERMARK_S,
  high = HIGH_WATERMARK_S,
}: BufferIndicatorProps) {
  const fill = bufferFillFraction(committedSecondsAhead, high);
  const health = bufferHealth(committedSecondsAhead, low, high);
  const lowMark = lowMarkFraction(low, high);

  return (
    <div
      className="group relative w-full"
      role="meter"
      aria-label="Generation buffer"
      aria-valuemin={0}
      aria-valuemax={Math.round(high)}
      aria-valuenow={Math.round(committedSecondsAhead)}
    >
      <div className="relative h-[3px] w-full overflow-visible rounded-full bg-white/10">
        <div
          data-testid="buffer-fill"
          data-health={health}
          className={`h-full rounded-full transition-[width] duration-500 ${HEALTH_COLOR[health]}`}
          style={{ width: `${fill * 100}%` }}
        />
        <span
          aria-hidden="true"
          data-testid="buffer-low-mark"
          className="absolute top-1/2 h-2 w-px -translate-y-1/2 bg-white/40"
          style={{ left: `${lowMark * 100}%` }}
          title="low watermark"
        />
      </div>
      <span className="pointer-events-none absolute -top-6 right-0 rounded bg-black/60 px-1.5 py-0.5 text-[0.65rem] tabular-nums text-kinora-muted opacity-0 transition-opacity group-hover:opacity-100">
        {Math.round(committedSecondsAhead)}s buffered
      </span>
    </div>
  );
}
