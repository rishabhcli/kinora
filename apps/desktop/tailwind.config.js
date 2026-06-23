/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/renderer/index.html", "./src/renderer/src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Warm library
        walnut: { DEFAULT: "#241811", deep: "#160e08" },
        oak: { DEFAULT: "#7a512f", light: "#9c6d40", dark: "#5a3c22" },
        parchment: { DEFAULT: "#f3ecdd", warm: "#efe5cf" },
        ink: { DEFAULT: "#1c150f", soft: "#5b4d40", faint: "#9b8a78" },
        // Cinematic accent (projector ember)
        ember: { DEFAULT: "#e0863a", deep: "#c26a24", glow: "#f4a85d" },
      },
      fontFamily: {
        display: ['ui-serif', '"New York"', "Georgia", "Times New Roman", "serif"],
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"SF Pro Text"',
          "system-ui",
          "sans-serif",
        ],
      },
      borderRadius: { glass: "26px" },
      boxShadow: {
        glass: "0 12px 48px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.30)",
        cover: "0 12px 30px rgba(0,0,0,0.55)",
      },
    },
  },
  plugins: [],
};
