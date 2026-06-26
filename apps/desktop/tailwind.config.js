/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        kinora: {
          bg: "#181614",
          "bg-deep": "#0e0d0c",
          text: "#e8e2d8",
          muted: "#a89e94",
          subtle: "#8d8378",
          gold: "#d4a44e",
          "gold-light": "#e8c878",
        },
      },
      fontFamily: {
        sans: [
          "DM Sans",
          "-apple-system",
          "BlinkMacSystemFont",
          "SF Pro Display",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        serif: ["Fraunces", "Georgia", "serif"],
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
