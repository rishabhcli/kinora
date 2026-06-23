/**
 * A compact iOS-style segmented control for flipping between Read and Watch
 * when the desktop reading room is too narrow for a side-by-side split.
 */
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  className = "",
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      aria-label="Reading view"
      className={`flex rounded-full border border-white/10 bg-white/[0.08] p-1 backdrop-blur-md ${className}`}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(opt.value)}
            className={`flex-1 rounded-full px-4 py-2 text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-glow/80 ${
              active
                ? "bg-parchment text-walnut-deep shadow-[0_2px_10px_-4px_rgba(0,0,0,0.45)]"
                : "text-white/70 hover:text-white"
            }`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
