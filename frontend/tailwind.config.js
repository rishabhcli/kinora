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
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "sans-serif"],
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "ken-burns": {
          "0%": { transform: "scale(1) translate(0, 0)" },
          "100%": { transform: "scale(1.12) translate(-2%, -2%)" },
        },
        "pulse-glow": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.6s ease-out both",
        "ken-burns": "ken-burns 12s ease-out alternate infinite",
        "pulse-glow": "pulse-glow 2.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
