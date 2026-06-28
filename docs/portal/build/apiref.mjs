/**
 * Generate the API-reference page body from the source-of-truth catalog.
 *
 * Reads clients/spec/catalog.mjs (the same data the SDKs are built from) and
 * renders an HTML body: endpoints grouped by tag, the event catalog, the
 * WebSocket channel, and the error-type table. Because it derives from the
 * catalog, the reference can never silently drift from the SDKs.
 */
import { ENDPOINTS, EVENTS, ERROR_TYPES, WEBSOCKET, MODELS, API_PREFIX, fullPath } from "../../../clients/spec/catalog.mjs";
import { escapeHtml } from "./markdown.mjs";

const TAG_TITLES = {
  auth: "Auth",
  books: "Books & Library",
  films: "Films",
  sessions: "Sessions (generation-on-scroll)",
  director: "Director tools",
  prefs: "Directing-style preferences",
  eval: "Evaluation",
  optim: "Cost & performance",
  events: "Event streams (SSE)",
};

function endpointBlock(e) {
  const auth = e.auth ? '<span class="badge auth">bearer</span>' : '<span class="badge">public</span>';
  const reqLink = e.requestModel && e.requestModel !== "multipart"
    ? ` &rarr; body <a href="#model-${e.requestModel}"><code>${e.requestModel}</code></a>`
    : e.requestModel === "multipart"
      ? " &rarr; multipart form"
      : "";
  const respLink = renderModelRef(e.responseModel);
  const query = e.query
    ? `<div class="summary">Query: ${Object.entries(e.query).map(([k, v]) => `<code>${k}</code> — ${escapeHtml(v)}`).join("; ")}</div>`
    : "";
  return `<div class="endpoint" id="op-${e.id}">
  <div><span class="method ${e.method}">${e.method}</span><span class="path">${escapeHtml(fullPath(e))}</span>${auth}</div>
  <div class="summary">${escapeHtml(e.summary)}${reqLink}${respLink ? ` &rarr; ${respLink} <span class="badge">${e.status}</span>` : ""}</div>
  ${query}
</div>`;
}

function renderModelRef(responseModel) {
  if (!responseModel) return "";
  if (responseModel === "text/event-stream") return "<code>text/event-stream</code>";
  if (responseModel.endsWith("[]")) {
    const base = responseModel.slice(0, -2);
    return `<a href="#model-${base}"><code>${base}[]</code></a>`;
  }
  if (MODELS[responseModel]) return `<a href="#model-${responseModel}"><code>${responseModel}</code></a>`;
  return `<code>${escapeHtml(responseModel)}</code>`;
}

function renderEndpoints() {
  const byTag = new Map();
  for (const e of ENDPOINTS) {
    if (!byTag.has(e.tag)) byTag.set(e.tag, []);
    byTag.get(e.tag).push(e);
  }
  const sections = [];
  for (const [tag, list] of byTag) {
    const title = TAG_TITLES[tag] ?? tag;
    sections.push(`<h2 id="tag-${tag}">${escapeHtml(title)}</h2>`);
    sections.push(list.map(endpointBlock).join("\n"));
  }
  return sections.join("\n");
}

function renderModels() {
  const rows = Object.entries(MODELS)
    .map(([name, fields]) => {
      const fieldRows = Object.entries(fields)
        .map(([f, t]) => `<tr><td><code>${escapeHtml(f)}</code></td><td><code>${escapeHtml(t)}</code></td></tr>`)
        .join("");
      return `<h3 id="model-${name}">${escapeHtml(name)}</h3>
<table><thead><tr><th>Field</th><th>Type</th></tr></thead><tbody>${fieldRows}</tbody></table>`;
    })
    .join("\n");
  return `<h2 id="schemas">Schemas</h2>\n${rows}`;
}

function renderEvents() {
  const rows = EVENTS.map(
    (ev) =>
      `<tr><td><code>${escapeHtml(ev.name)}</code></td><td>${escapeHtml(ev.summary)}</td><td>${ev.fields
        .map((f) => `<code>${escapeHtml(f)}</code>`)
        .join(" ")}</td><td>${ev.channels.join(", ")}</td></tr>`,
  ).join("");
  return `<h2 id="events">Server-sent events</h2>
<p>Subscribe with <code>GET ${API_PREFIX}/sessions/{session_id}/events</code> (or the SDK
<code>iter_events</code> / <code>sessions.events()</code> helpers). Each frame is
<code>event: &lt;name&gt;</code> + a JSON <code>data:</code> payload.</p>
<table><thead><tr><th>Event</th><th>Summary</th><th>Fields</th><th>Channels</th></tr></thead><tbody>${rows}</tbody></table>
<h3 id="websocket">WebSocket</h3>
<p><span class="method WS">WS</span> <code>${escapeHtml(API_PREFIX + WEBSOCKET.path)}</code> — ${escapeHtml(WEBSOCKET.summary)}
Client messages: ${WEBSOCKET.clientMessages.map((m) => `<code>${m}</code>`).join(", ")}. Auth: ${escapeHtml(WEBSOCKET.auth)}</p>`;
}

function renderErrors() {
  const rows = ERROR_TYPES.map(
    (e) => `<tr><td><code>${escapeHtml(e.type)}</code></td><td><code>${e.status}</code></td><td>${escapeHtml(e.summary)}</td></tr>`,
  ).join("");
  return `<h2 id="errors">Error types</h2>
<p>Every error is the envelope <code>{ "error": { type, message, detail? } }</code>. The SDKs map
each status onto a typed exception (e.g. <code>404 &rarr; NotFoundError</code>,
<code>402 &rarr; BudgetExceededError</code>).</p>
<table><thead><tr><th>type</th><th>status</th><th>Meaning</th></tr></thead><tbody>${rows}</tbody></table>`;
}

export function renderApiReference() {
  return [
    `<h1>API reference</h1>`,
    `<p>Generated from the single source-of-truth catalog (<code>clients/spec/catalog.mjs</code>) —
the same description the TypeScript and Python SDKs are built from, so this reference can
never drift from the clients. All paths are under <code>${API_PREFIX}</code>; auth is a JWT bearer token.</p>`,
    `<p>Machine-readable: <a href="../../clients/spec/openapi.json"><code>openapi.json</code></a>.</p>`,
    renderEndpoints(),
    renderEvents(),
    renderErrors(),
    renderModels(),
  ].join("\n");
}
