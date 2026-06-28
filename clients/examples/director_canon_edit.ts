/**
 * Example: a surgical canon edit and watching only the dependent shots regen.
 *
 * Defaults to a built-in mock (no live backend, zero video spend). Set
 * KINORA_BASE_URL to run against a real backend.
 */
import { KinoraClient, isEvent, type FetchLike } from "../typescript/src/index.js";

const BASE_URL = process.env.KINORA_BASE_URL ?? "http://localhost:8000";
const USE_MOCK = !process.env.KINORA_BASE_URL;

function mockFetch(): FetchLike {
  const regen =
    ": connected\n\n" +
    'event: agent_activity\ndata: {"event":"agent_activity","agent":"continuity_supervisor","aspect":"canon","message":"Eleanor -> v2 - 2 shots re-rendering"}\n\n' +
    'event: regen_done\ndata: {"event":"regen_done","shot_id":"shot_0001","oss_url":"http://example/c1.mp4","qa":{"score":0.93}}\n\n' +
    'event: regen_done\ndata: {"event":"regen_done","shot_id":"shot_0007","oss_url":"http://example/c7.mp4","qa":{"score":0.95}}\n\n';
  return async (input) => {
    const url = String(input);
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    if (url.endsWith("/api/auth/login"))
      return json(200, { access_token: "demo", token_type: "bearer", expires_in: 3600 });
    if (url.endsWith("/api/books/book_demo/canon"))
      return json(200, {
        book_id: "book_demo",
        entities: [{ id: "eleanor", type: "character", name: "Eleanor", version: 1, aliases: [] }],
        states: [],
        markdown: "# Canon",
      });
    if (url.includes("/canon_edit"))
      return json(200, { entity_key: "eleanor", version: 2, affected_shot_ids: ["shot_0001", "shot_0007"], skipped_shots: 41 });
    if (url.includes("/events"))
      return new Response(regen, { status: 200, headers: { "content-type": "text/event-stream" } });
    return json(404, { error: { type: "not_found", message: url } });
  };
}

async function main(): Promise<void> {
  console.log(`Kinora TS director example — ${USE_MOCK ? "MOCK backend" : BASE_URL}`);
  const client = new KinoraClient({ baseUrl: BASE_URL, ...(USE_MOCK ? { fetch: mockFetch() } : {}) });
  await client.auth.login({ email: "demo@kinora.local", password: "demo-password-123" });

  const bookId = "book_demo";
  const sessionId = "sess_demo";
  const canon = await client.books.canon(bookId);
  const hero = canon.entities.find((e) => e.name === "Eleanor") ?? canon.entities[0];
  console.log(`editing canon entity: ${hero.name} (v${hero.version})`);

  const edit = await client.director.canonEdit(bookId, {
    entity_key: hero.id,
    changes: { description: "now wears a deep crimson cloak" },
  });
  console.log(`-> entity v${edit.version}; re-rendering ${edit.affected_shot_ids.length} shots, ${edit.skipped_shots} cache hits skipped`);

  const pending = new Set(edit.affected_shot_ids);
  const controller = new AbortController();
  for await (const ev of client.sessions.events(sessionId, { signal: controller.signal })) {
    if (isEvent(ev, "agent_activity")) console.log(`  [${ev.agent}] ${ev.message}`);
    if (isEvent(ev, "regen_done")) {
      console.log(`  regen_done: ${ev.shot_id} -> ${ev.oss_url}`);
      pending.delete(ev.shot_id);
      if (pending.size === 0) {
        controller.abort();
        break;
      }
    }
  }
  console.log("all dependent shots re-rendered.");
}

main().catch((e) => {
  console.error("example failed:", e);
  process.exitCode = 1;
});
