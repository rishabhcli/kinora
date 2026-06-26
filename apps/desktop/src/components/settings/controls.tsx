import { Children, type ReactNode, useId } from "react";
import { motion } from "framer-motion";
import { Icon } from "../icons";
import type { IconName } from "../icons";
import { useReducedMotionPref } from "../../a11y/useReducedMotionPref";

/* ── Section header ─────────────────────────────────────────────────────── */
export function SectionTitle({
  icon,
  title,
  subtitle,
}: {
  icon: IconName;
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="flex items-start gap-3.5 mb-6">
      <span
        className="grid place-items-center rounded-xl shrink-0"
        style={{
          width: 38,
          height: 38,
          background: "linear-gradient(135deg, rgba(212,164,78,0.18) 0%, rgba(212,164,78,0.06) 100%)",
          color: "#e8c878",
          border: "1px solid rgba(212,164,78,0.15)",
          boxShadow: "0 2px 12px -4px rgba(212,164,78,0.2)",
        }}
      >
        <Icon name={icon} size={20} weight="medium" />
      </span>
      <div className="min-w-0 pt-0.5">
        <h2 className="font-serif text-[20px] font-semibold text-kinora-text leading-tight">{title}</h2>
        {subtitle && <p className="text-[12px] text-kinora-muted mt-1">{subtitle}</p>}
      </div>
    </div>
  );
}

/* ── Grouped card (macOS System-Settings style) ─────────────────────────── */
export function SettingsGroup({ title, children }: { title?: string; children: ReactNode }) {
  const rows = Children.toArray(children).filter(Boolean);
  return (
    <div className="mb-6">
      {title && (
        <p className="text-[11px] font-semibold uppercase tracking-wide text-kinora-subtle mb-2 ml-1">
          {title}
        </p>
      )}
      <div
        className="rounded-2xl overflow-hidden"
        style={{
          background: "linear-gradient(180deg, rgba(255,255,255,0.045) 0%, rgba(255,255,255,0.025) 100%)",
          border: "1px solid rgba(255,255,255,0.07)",
          boxShadow: "0 4px 24px -12px rgba(0,0,0,0.4)",
        }}
      >
        {rows.map((row, i) => (
          <div key={i}>
            {i > 0 && <div className="kn-set-divider" />}
            {row}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── A single setting row: [icon] label/desc … control ──────────────────── */
export function Row({
  icon,
  label,
  description,
  htmlFor,
  children,
  align = "center",
}: {
  icon?: IconName;
  label: string;
  description?: string;
  htmlFor?: string;
  children?: ReactNode;
  align?: "center" | "start";
}) {
  return (
    <div className={`flex gap-3.5 px-4 py-3.5 ${align === "start" ? "items-start" : "items-center"} transition-colors hover:bg-white/[0.015]`}>
      {icon && (
        <span className="text-kinora-muted/80 shrink-0 mt-0.5" aria-hidden="true">
          <Icon name={icon} size={17} />
        </span>
      )}
      <div className="flex-1 min-w-0">
        <label htmlFor={htmlFor} className="text-[13px] font-medium text-kinora-text block">
          {label}
        </label>
        {description && <p className="text-[11.5px] text-kinora-muted/80 mt-0.5 leading-snug">{description}</p>}
      </div>
      {children && <div className="shrink-0">{children}</div>}
    </div>
  );
}

/* ── Switch (toggle) ────────────────────────────────────────────────────── */
export function Switch({
  checked,
  onChange,
  label,
  id,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  id?: string;
}) {
  const reduce = useReducedMotionPref();
  return (
    <button
      id={id}
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className="kn-set-focusable relative rounded-full shrink-0"
      style={{
        width: 42,
        height: 25,
        background: checked
          ? "linear-gradient(135deg, #d4a44e 0%, #c8923a 100%)"
          : "rgba(255,255,255,0.12)",
        border: checked ? "none" : "1px solid rgba(255,255,255,0.06)",
        boxShadow: checked ? "0 2px 8px -2px rgba(212,164,78,0.4)" : "none",
        transition: "background 0.25s ease, box-shadow 0.25s ease",
      }}
    >
      <motion.span
        className="absolute rounded-full"
        style={{ width: 19, height: 19, top: 3, left: 3, background: "#fff", boxShadow: "0 1px 4px rgba(0,0,0,0.35)" }}
        animate={{ x: checked ? 17 : 0 }}
        transition={reduce ? { duration: 0 } : { type: "spring", stiffness: 600, damping: 36 }}
      />
    </button>
  );
}

/* ── Segmented control ──────────────────────────────────────────────────── */
export interface SegOption<T extends string> {
  value: T;
  label?: string;
  icon?: IconName;
}
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
}: {
  value: T;
  options: SegOption<T>[];
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  const reduce = useReducedMotionPref();
  const groupId = useId();
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className="inline-flex p-0.5 rounded-xl relative"
      style={{ background: "rgba(0,0,0,0.25)", border: "0.5px solid rgba(255,255,255,0.06)" }}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            role="radio"
            aria-checked={active}
            aria-label={opt.label ?? opt.value}
            onClick={() => onChange(opt.value)}
            className={`kn-set-focusable relative px-2.5 py-1 rounded-[10px] text-[12px] font-medium transition-colors ${
              active ? "text-[#1a1512]" : "text-kinora-muted hover:text-kinora-text"
            }`}
            style={{ zIndex: 1 }}
          >
            {active && (
              <motion.span
                layoutId={`seg-${groupId}`}
                className="absolute inset-0 rounded-[10px]"
                style={{ background: "#e8e2d8", zIndex: -1 }}
                transition={reduce ? { duration: 0 } : { type: "spring", stiffness: 520, damping: 38 }}
              />
            )}
            <span className="inline-flex items-center gap-1.5">
              {opt.icon && <Icon name={opt.icon} size={13} weight="medium" />}
              {opt.label && <span>{opt.label}</span>}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* ── Slider ─────────────────────────────────────────────────────────────── */
export function Slider({
  value,
  min,
  max,
  step,
  onChange,
  label,
  id,
  format,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  label: string;
  id?: string;
  format?: (v: number) => string;
}) {
  const pct = ((value - min) / (max - min)) * 100;
  return (
    <div className="flex items-center gap-3" style={{ minWidth: 190 }}>
      <input
        id={id}
        type="range"
        className="kn-set-slider flex-1"
        min={min}
        max={max}
        step={step}
        value={value}
        aria-label={label}
        aria-valuetext={format ? format(value) : String(value)}
        onChange={(e) => onChange(Number(e.target.value))}
        style={
          {
            // gold fill up to the thumb
            "--kn-set-track": `linear-gradient(90deg, #d4a44e ${pct}%, rgba(255,255,255,0.12) ${pct}%)`,
            background: "transparent",
          } as React.CSSProperties
        }
      />
      <span className="text-[12px] text-kinora-muted tabular-nums w-12 text-right">
        {format ? format(value) : value}
      </span>
    </div>
  );
}

/* ── Stepper (numeric +/–) ──────────────────────────────────────────────── */
export function Stepper({
  value,
  min,
  max,
  step = 1,
  onChange,
  label,
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
  label: string;
}) {
  const clamp = (n: number) => Math.min(max, Math.max(min, n));
  return (
    <div
      className="inline-flex items-center rounded-xl overflow-hidden"
      style={{ background: "rgba(0,0,0,0.25)", border: "0.5px solid rgba(255,255,255,0.06)" }}
    >
      <button
        aria-label={`Decrease ${label}`}
        onClick={() => onChange(clamp(value - step))}
        disabled={value <= min}
        className="kn-set-focusable px-2.5 py-1.5 text-kinora-text hover:bg-white/[0.06] disabled:opacity-30"
      >
        <Icon name="minus" size={14} weight="semibold" />
      </button>
      <span className="px-3 text-[13px] text-kinora-text tabular-nums min-w-[2.5rem] text-center">{value}</span>
      <button
        aria-label={`Increase ${label}`}
        onClick={() => onChange(clamp(value + step))}
        disabled={value >= max}
        className="kn-set-focusable px-2.5 py-1.5 text-kinora-text hover:bg-white/[0.06] disabled:opacity-30"
      >
        <Icon name="plus" size={14} weight="semibold" />
      </button>
    </div>
  );
}

/* ── A small text/action button used inside rows ────────────────────────── */
export function RowButton({
  children,
  onClick,
  icon,
  tone = "default",
}: {
  children: ReactNode;
  onClick?: () => void;
  icon?: IconName;
  tone?: "default" | "danger" | "accent";
}) {
  const color =
    tone === "danger" ? "#f0928a" : tone === "accent" ? "#e8c878" : "rgba(232,226,216,0.92)";
  const hoverBg =
    tone === "danger" ? "rgba(240,146,138,0.08)" : tone === "accent" ? "rgba(232,200,120,0.08)" : "rgba(255,255,255,0.05)";
  return (
    <button
      onClick={onClick}
      className="kn-set-focusable inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg text-[12.5px] font-medium transition-all duration-200"
      style={{ color, background: "transparent" }}
      onMouseEnter={(e) => { e.currentTarget.style.background = hoverBg; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
    >
      {icon && <Icon name={icon} size={14} />}
      {children}
    </button>
  );
}
