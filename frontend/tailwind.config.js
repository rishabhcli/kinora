/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        kinora: {
          ink: "#0b0b12",
          panel: "#14141f",
          panel2: "#1b1b2b",
          line: "#272739",
          mist: "#e8e9f3",
          muted: "#9aa0b5",
          glow: "#7c5cff",
          // Lighter accent that clears WCAG AA (>= 4.5:1) for small text on the dark panels.
          iris: "#a78bfa",
          // Semantic accents for QA badges, buffer health and notices.
          ok: "#34d399",
          warn: "#fbbf24",
          danger: "#f87171",
          gold: "#f5c87a",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "sans-serif"],
        serif: ["Georgia", "Cambria", "Times New Roman", "serif"],
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "ken-burns": {
          "0%": { transform: "scale(1.02) translate(0, 0)" },
          "100%": { transform: "scale(1.16) translate(-2.5%, -2%)" },
        },
        "pulse-glow": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        shimmer: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        "fade-in": {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        "slide-in": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.6s ease-out both",
        "ken-burns": "ken-burns 18s ease-out alternate infinite",
        "pulse-glow": "pulse-glow 2.4s ease-in-out infinite",
        shimmer: "shimmer 1.8s ease-in-out infinite",
        "fade-in": "fade-in 0.4s ease-out both",
        "slide-in": "slide-in 0.32s ease-out both",
      },
    },
  },
  plugins: [],
};
