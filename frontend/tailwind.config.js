/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        kinora: {
          ink: "#0b0b12",
          panel: "#14141f",
          mist: "#e8e9f3",
          muted: "#9aa0b5",
          glow: "#7c5cff",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "sans-serif"],
      },
    },
  },
  plugins: [],
};
