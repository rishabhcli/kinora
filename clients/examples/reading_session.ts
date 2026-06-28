/**
 * Example: the whole reading-session loop with the TypeScript SDK.
 *
 *   login -> list books -> open a session -> post intent -> stream events.
 *
 * Defaults to a built-in mock fetch (no live backend, zero video spend). Set
 * KINORA_BASE_URL to run against a real backend.
 */
import { KinoraClient, isEvent, type FetchLike } from "../typescript/src/index.js";

const BASE_URL = process.env.KINORA_BASE_URL ?? "http://localhost:8000";
const EMAIL = process.env.KINORA_EMAIL ?? "demo@kinora.local";
const PASSWORD = process.env.KINORA_PASSWORD ?? "demo-password-123";
const USE_MOCK = !process.env.KINORA_BASE_URL;

/** A deterministic mock backend so the example runs offline. */
function mockFetch(): FetchLike {
  const sse =
    ": connected\n\n" +
    'event: buffer_state\ndata: {"event":"buffer_state","committed_seconds_ahead":25,"bursting":true,"idle":false,"budget_remaining_s":1650}\n\n' +
    'event: clip_ready\ndata: {"event":"clip_ready","shot_id":"shot_0001","oss_url":"http://example/clip.mp4","video_seconds":0}\n\n';
  return async (input) => {
    const url = String(input);
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    if (url.endsWith("/api/auth/login"))
      return json(200, { access_token: "demo-token", token_type: "bearer", expires_in: 3600 });
    if (url.endsWith("/api/books"))
      return json(200, [
        { id: "book_demo", title: "The Demo Book", author: "A. Writer", status: "ready", progress: 1 },
      ]);
    if (url.endsWith("/api/sessions"))
      return json(201, {
        session_id: "sess_demo",
        book_id: "book_demo",
        focus_word: 0,
        velocity_wps: 4,
        mode: "viewer",
        committed_seconds_ahead: 0,
        bursting: false,
        budget_remaining_s: 1650,
        inflight: {},
      });
    if (url.includes("/intent"))
      return json(200, {
        session_id: "sess_demo",
        settled: true,
        allow_promotion: true,
        idle: false,
        bursting: true,
        committed_seconds_ahead: 25,
        promoted: ["shot_0001"],
        keyframed: ["shot_0002"],
        cancelled: 0,
      });
    if (url.includes("/events"))
      return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } });
    return json(404, { error: { type: "not_found", message: url } });
  };
}

async function main(): Promise<void> {
  console.log(`Kinora TS example — ${USE_MOCK ? "MOCK backend" : BASE_URL}`);
  const client = new KinoraClient({
    baseUrl: BASE_URL,
    ...(USE_MOCK ? { fetch: mockFetch() } : {}),
  });

  await client.auth.login({ email: EMAIL, password: PASSWORD });
  console.log("authenticated:", client.isAuthenticated());

  const books = await client.books.list();
  const book = books.collect().find((b) => b.status === "ready") ?? books.first();
  if (!book) throw new Error("no books — run `make seed-demo`");
  console.log(`book: ${book.title} (${book.id}) status=${book.status}`);

  const session = await client.sessions.create({ book_id: book.id, focus_word: 0 });
  console.log(`session: ${session.session_id}`);

  const intent = await client.sessions.intent(session.session_id, { focus_word: 120, velocity: 4.2 });
  console.log(`intent -> promoted=${intent.promoted.join(",")} ahead=${intent.committed_seconds_ahead}s`);

  console.log("streaming events…");
  const controller = new AbortController();
  for await (const ev of client.sessions.events(session.session_id, { signal: controller.signal })) {
    if (isEvent(ev, "buffer_state")) console.log(`  buffer_state: ${ev.committed_seconds_ahead}s ahead`);
    if (isEvent(ev, "clip_ready")) {
      console.log(`  clip_ready: shot ${ev.shot_id} -> ${ev.oss_url}`);
      controller.abort();
      break;
    }
  }
  console.log("done.");
}

main().catch((e) => {
  console.error("example failed:", e);
  process.exitCode = 1;
});
