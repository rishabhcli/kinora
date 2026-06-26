/** @type {import('tailwindcss').Config} */

/* Every colour resolves to a CSS custom property defined in
   src/styles/tokens.css, stored as an RGB triple so Tailwind's `/<alpha>`
   opacity modifiers keep working (e.g. `text-kinora-text/85`, `bg-accent/40`).
   Change a token in tokens.css and the whole app shifts — no edits here. */
const c = (token) => `rgb(var(${token}) / <alpha-value>)`;

module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        /* ── semantic scale (new) — bg-surface, text-muted, border-hairline … ── */
        bg: c("--k-bg-rgb"),
        "bg-deep": c("--k-bg-deep-rgb"),
        surface: c("--k-surface-rgb"),
        "surface-raised": c("--k-surface-raised-rgb"),
        "surface-high": c("--k-surface-high-rgb"),
        text: c("--k-text-rgb"),
        muted: c("--k-muted-rgb"),
        subtle: c("--k-subtle-rgb"),
        faint: c("--k-faint-rgb"),
        accent: c("--k-accent-rgb"),
        "accent-strong": c("--k-accent-strong-rgb"),
        "accent-deep": c("--k-accent-deep-rgb"),
        "accent-cool": c("--k-accent-cool-rgb"),
        success: c("--k-success-rgb"),
        warning: c("--k-warning-rgb"),
        danger: c("--k-danger-rgb"),
        info: c("--k-info-rgb"),
        /* dedicated line colours so `border-hairline` reads as a hairline */
        hairline: "var(--k-border)",
        "hairline-strong": "var(--k-border-strong)",

        /* ── legacy aliases — keep every existing `*-kinora-*` class working ── */
        kinora: {
          bg: c("--k-bg-rgb"),
          "bg-deep": c("--k-bg-deep-rgb"),
          surface: c("--k-surface-rgb"),
          text: c("--k-text-rgb"),
          muted: c("--k-muted-rgb"),
          subtle: c("--k-subtle-rgb"),
          gold: c("--k-accent-rgb"),
          "gold-light": c("--k-accent-strong-rgb"),
        },
      },

      fontFamily: {
        /* sans/serif are remapped to the token faces so existing
           `font-sans`/`font-serif` usage upgrades in place. */
        sans: ["var(--k-font-ui)"],
        serif: ["var(--k-font-display)"],
        ui: ["var(--k-font-ui)"],
        display: ["var(--k-font-display)"],
        reading: ["var(--k-font-reading)"],
        mono: ["var(--k-font-mono)"],
      },

      fontSize: {
        /* token-driven modular scale, exposed as `text-k-*` so it never
           clobbers Tailwind's default `text-sm/-lg/…` that components rely on */
        "k-xs": "var(--k-text-xs)",
        "k-sm": "var(--k-text-sm)",
        "k-base": "var(--k-text-base)",
        "k-md": "var(--k-text-md)",
        "k-lg": "var(--k-text-lg)",
        "k-xl": "var(--k-text-xl)",
        "k-2xl": "var(--k-text-2xl)",
        "k-3xl": "var(--k-text-3xl)",
        "k-4xl": "var(--k-text-4xl)",
        "k-5xl": "var(--k-text-5xl)",
      },

      letterSpacing: {
        "k-display": "var(--k-tracking-display)",
        "k-tight": "var(--k-tracking-tight)",
        "k-wide": "var(--k-tracking-wide)",
        "k-caps": "var(--k-tracking-caps)",
      },

      /* All ADDITIVE (namespaced) — base scales untouched so existing
         rounded / shadow / blur utilities keep their geometry (buttons safe). */
      boxShadow: {
        "elev-1": "var(--k-elev-1)",
        "elev-2": "var(--k-elev-2)",
        "elev-3": "var(--k-elev-3)",
        "elev-4": "var(--k-elev-4)",
        "elev-5": "var(--k-elev-5)",
        "ring-top": "var(--k-ring-top)",
        glow: "var(--k-glow-accent)",
      },
      borderRadius: {
        "k-xs": "var(--k-radius-xs)",
        "k-sm": "var(--k-radius-sm)",
        k: "var(--k-radius)",
        "k-lg": "var(--k-radius-lg)",
        "k-xl": "var(--k-radius-xl)",
      },
      backdropBlur: {
        "k-sm": "var(--k-blur-sm)",
        k: "var(--k-blur)",
        "k-lg": "var(--k-blur-lg)",
        "k-xl": "var(--k-blur-xl)",
      },

      animation: {
        "fade-in": "fadeIn 0.4s ease-out",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};
