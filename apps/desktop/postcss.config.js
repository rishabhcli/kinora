module.exports = {
  plugins: {
    // postcss-import MUST run first: it inlines the @import statements in
    // src/styles/index.css (the Captain's CSS aggregator), including the
    // `tailwindcss/{base,components,utilities}` entrypoints, so custom partials
    // land *after* Tailwind's utilities and keep winning by source order.
    "postcss-import": {},
    tailwindcss: {},
    autoprefixer: {},
  },
};
