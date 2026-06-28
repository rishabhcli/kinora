/**
 * The HTML shell + CSS for the Kinora docs portal.
 *
 * One self-contained template (no external CSS/JS, no fonts fetched) so the
 * built site is fully static and works offline. Dark, cinematic palette to
 * match the product. The page layout is a fixed sidebar nav + a content column
 * with an auto table of contents.
 */

const CSS = `
:root {
  --bg: #0b0e14; --panel: #121722; --ink: #e6ebf4; --muted: #8b97ad;
  --accent: #6ea8ff; --accent-2: #b78bff; --border: #232b3a; --code-bg: #0e131d;
  --good: #4ade80; --warn: #fbbf24; --danger: #f87171;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.layout { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }
.sidebar {
  background: var(--panel); border-right: 1px solid var(--border);
  padding: 28px 20px; position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.brand { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 4px; }
.brand .dot { color: var(--accent-2); }
.tagline { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.nav-group { margin-bottom: 18px; }
.nav-group h4 {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 0 0 8px; font-weight: 600;
}
.nav-group a { display: block; padding: 5px 10px; border-radius: 7px; color: var(--ink); font-size: 14px; }
.nav-group a:hover { background: rgba(110,168,255,0.08); text-decoration: none; }
.nav-group a.active { background: rgba(110,168,255,0.16); color: var(--accent); font-weight: 600; }
.content { padding: 48px 56px; max-width: 860px; }
.content h1 { font-size: 34px; letter-spacing: -0.02em; margin: 0 0 8px; }
.content h2 { font-size: 24px; margin: 40px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
.content h3 { font-size: 19px; margin: 28px 0 8px; }
.content h4 { font-size: 16px; margin: 20px 0 6px; color: var(--muted); }
.content p { margin: 12px 0; }
.content ul, .content ol { margin: 12px 0; padding-left: 24px; }
.content li { margin: 4px 0; }
.content code {
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 5px;
  padding: 1px 6px; font-size: 13.5px; font-family: "SF Mono", Menlo, Consolas, monospace;
  color: #d6e2ff;
}
.content pre {
  background: var(--code-bg); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px; overflow-x: auto; margin: 16px 0;
}
.content pre code { background: none; border: none; padding: 0; color: #cdd9ee; font-size: 13.5px; }
.content blockquote {
  border-left: 3px solid var(--accent-2); margin: 16px 0; padding: 4px 16px;
  color: var(--muted); background: rgba(183,139,255,0.06); border-radius: 0 8px 8px 0;
}
.content table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; }
.content th, .content td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
.content th { background: var(--panel); font-weight: 600; }
.content hr { border: none; border-top: 1px solid var(--border); margin: 28px 0; }
.method { font-weight: 700; font-size: 12px; padding: 2px 8px; border-radius: 5px; display: inline-block; margin-right: 8px; }
.method.GET { background: rgba(74,222,128,0.16); color: var(--good); }
.method.POST { background: rgba(110,168,255,0.16); color: var(--accent); }
.method.DELETE { background: rgba(248,113,113,0.16); color: var(--danger); }
.method.PUT, .method.PATCH { background: rgba(251,191,36,0.16); color: var(--warn); }
.method.WS { background: rgba(183,139,255,0.16); color: var(--accent-2); }
.endpoint { border: 1px solid var(--border); border-radius: 10px; padding: 14px 18px; margin: 14px 0; background: var(--panel); }
.endpoint .path { font-family: "SF Mono", Menlo, monospace; font-size: 14px; }
.endpoint .summary { color: var(--muted); font-size: 14px; margin: 6px 0 0; }
.badge { font-size: 11px; padding: 1px 7px; border-radius: 20px; border: 1px solid var(--border); color: var(--muted); margin-left: 6px; }
.badge.auth { color: var(--warn); border-color: rgba(251,191,36,0.4); }
.footer { color: var(--muted); font-size: 12px; margin-top: 56px; border-top: 1px solid var(--border); padding-top: 16px; }
.version-pill { font-size: 11px; background: rgba(110,168,255,0.14); color: var(--accent); padding: 2px 9px; border-radius: 20px; }
@media (max-width: 820px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { position: static; height: auto; }
  .content { padding: 32px 22px; }
}
`;

/** Render the navigation HTML from the nav structure, marking the active page. */
function renderNav(nav, activeSlug) {
  return nav
    .map((group) => {
      const links = group.items
        .map((item) => {
          const active = item.slug === activeSlug ? " active" : "";
          return `<a class="nav-link${active}" href="${item.slug}.html">${item.title}</a>`;
        })
        .join("\n");
      return `<div class="nav-group"><h4>${group.title}</h4>${links}</div>`;
    })
    .join("\n");
}

/** Wrap rendered content HTML in the full page shell. */
export function renderPage({ title, contentHtml, nav, activeSlug, version }) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${title} · Kinora docs</title>
<style>${CSS}</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <p class="brand">Kinora<span class="dot">.</span> <span class="version-pill">v${version}</span></p>
    <p class="tagline">Developer documentation</p>
    ${renderNav(nav, activeSlug)}
  </aside>
  <main class="content">
    ${contentHtml}
    <div class="footer">
      Kinora developer docs · API v${version} · Apache-2.0 ·
      Generated from the single source-of-truth catalog.
    </div>
  </main>
</div>
</body>
</html>`;
}

export { CSS };
