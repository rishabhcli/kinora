import net from "node:net";

/**
 * A tiny, dependency-free Redis PUBLISH client (raw RESP over a TCP socket).
 *
 * The events / director specs assert that the workspace reacts to backend
 * generation events (`clip_ready`, `keyframe_ready`, `regen_done`, …). The real
 * producer is the render worker; in e2e we publish those events directly onto
 * the session/book pub-sub channel the SSE stream is subscribed to — exactly the
 * §5.6 fan-out path — and assert the UI reacts. Speaking RESP directly avoids a
 * redis client dependency and works wherever the Redis port is reachable
 * (the throwaway container locally, the service container in CI).
 *
 * `PUBLISH` replies with the number of subscribers that received the message,
 * which lets a spec retry until the SSE connection is actually live (no
 * arbitrary sleeps): re-publishing is harmless because the event handlers are
 * idempotent.
 */

const HOST = process.env.KINORA_REDIS_HOST ?? "127.0.0.1";
const PORT = Number.parseInt(process.env.KINORA_REDIS_PORT ?? "6379", 10);

export function sessionChannel(sessionId: string): string {
  return `kinora:events:session:${sessionId}`;
}

export function bookChannel(bookId: string): string {
  return `kinora:events:book:${bookId}`;
}

function respCommand(args: string[]): Buffer {
  const parts: Buffer[] = [Buffer.from(`*${args.length}\r\n`, "utf8")];
  for (const arg of args) {
    const buf = Buffer.from(arg, "utf8");
    parts.push(Buffer.from(`$${buf.length}\r\n`, "utf8"), buf, Buffer.from("\r\n", "utf8"));
  }
  return Buffer.concat(parts);
}

/** PUBLISH a raw string; resolves with the subscriber count from the reply. */
export function publishRaw(channel: string, message: string): Promise<number> {
  return new Promise<number>((resolve, reject) => {
    const socket = net.createConnection({ host: HOST, port: PORT });
    let buf = "";
    const done = (err: Error | null, count = 0) => {
      socket.destroy();
      if (err) reject(err);
      else resolve(count);
    };
    socket.setTimeout(5000, () => done(new Error("redis publish timed out")));
    socket.on("error", (err) => done(err));
    socket.on("connect", () => socket.write(respCommand(["PUBLISH", channel, message])));
    socket.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      // Integer reply: ":<n>\r\n". Error reply: "-<msg>\r\n".
      if (buf.startsWith(":") && buf.includes("\r\n")) {
        done(null, Number.parseInt(buf.slice(1), 10) || 0);
      } else if (buf.startsWith("-") && buf.includes("\r\n")) {
        done(new Error(`redis error: ${buf.trim()}`));
      }
    });
  });
}

/** JSON-encode and publish a §5.6 event; resolves with the subscriber count. */
export function publishEvent(
  channel: string,
  payload: Record<string, unknown>,
): Promise<number> {
  return publishRaw(channel, JSON.stringify(payload));
}
