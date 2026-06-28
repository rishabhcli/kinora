/**
 * Test helpers: a scriptable mock `fetch` and SSE stream builders.
 *
 * Zero real network. Every test drives the SDK against a deterministic mock
 * fetch that records requests and replays canned responses.
 */
import type { FetchLike } from "../src/transport.js";

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface MockResponseSpec {
  status?: number;
  json?: unknown;
  text?: string;
  headers?: Record<string, string>;
  /** Throw this instead of responding (simulates a network failure). */
  throw?: Error;
  /** A ReadableStream body (for SSE). */
  stream?: ReadableStream<Uint8Array>;
  contentType?: string;
}

export class MockFetch {
  readonly requests: RecordedRequest[] = [];
  private queue: MockResponseSpec[] = [];
  private defaultSpec: MockResponseSpec | null = null;

  /** Enqueue the next response (FIFO). */
  enqueue(spec: MockResponseSpec): this {
    this.queue.push(spec);
    return this;
  }

  /** Set a fallback response when the queue is empty. */
  default(spec: MockResponseSpec): this {
    this.defaultSpec = spec;
    return this;
  }

  /** The bound `fetch`-compatible function. */
  get fetch(): FetchLike {
    return async (input, init) => {
      const headers: Record<string, string> = {};
      const h = init?.headers;
      if (h) {
        if (h instanceof Headers) h.forEach((v, k) => (headers[k] = v));
        else if (Array.isArray(h)) for (const [k, v] of h) headers[k] = v;
        else Object.assign(headers, h);
      }
      let body: unknown = init?.body;
      if (typeof body === "string") {
        try {
          body = JSON.parse(body);
        } catch {
          /* not json */
        }
      }
      this.requests.push({
        url: String(input),
        method: init?.method ?? "GET",
        headers,
        body,
      });
      const spec = this.queue.shift() ?? this.defaultSpec;
      if (!spec) throw new Error(`MockFetch: no response queued for ${init?.method} ${input}`);
      if (spec.throw) throw spec.throw;
      return this.toResponse(spec);
    };
  }

  private toResponse(spec: MockResponseSpec): Response {
    const status = spec.status ?? 200;
    const headers = new Headers(spec.headers ?? {});
    if (spec.stream) {
      if (!headers.has("content-type")) headers.set("content-type", spec.contentType ?? "text/event-stream");
      return new Response(spec.stream, { status, headers });
    }
    let bodyText: string | null;
    if (spec.text !== undefined) {
      bodyText = spec.text;
    } else if (spec.json !== undefined) {
      bodyText = JSON.stringify(spec.json);
      if (!headers.has("content-type")) headers.set("content-type", "application/json");
    } else {
      bodyText = null;
    }
    return new Response(bodyText, { status, headers });
  }

  /** The last recorded request. */
  last(): RecordedRequest | undefined {
    return this.requests[this.requests.length - 1];
  }
}

/** Build a ReadableStream that emits the given chunks (strings) then closes. */
export function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(encoder.encode(chunks[i]!));
        i++;
      } else {
        controller.close();
      }
    },
  });
}

/** Build a single SSE frame string. */
export function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** An immediate no-op sleep so retry tests don't wait on real timers. */
export const noSleep = (_ms: number): Promise<void> => Promise.resolve();
