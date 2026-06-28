// A fetch wrapper that reports each completed clip download as a throughput
// sample (bytes + ms) to a BandwidthEstimator. The ClipCache already fetches each
// clip's bytes once and holds them as a Blob; wrapping its fetch transport with
// this lets the adaptive-quality controller learn the link speed from the exact
// transfers the reader is already doing — no synthetic probes, no extra requests.
//
// Pure-ish: it depends only on the injected `fetch` and a sink callback, so a test
// drives it with a fake fetch and asserts the sample. It tees the response body so
// the caller still gets a normal Response to `.blob()`.
import type { BandwidthEstimator, ThroughputSample } from "./bandwidth";

export interface InstrumentOptions {
  /** the underlying transport (default: global fetch) */
  fetchImpl?: typeof fetch;
  /** time source in ms (default: performance.now / Date.now) */
  now?: () => number;
  /** called with each completed transfer's {bytes, durationMs} */
  onSample: (sample: ThroughputSample) => void;
}

/** Build a `fetch`-shaped function that measures byte count + duration and reports
 *  a sample on completion. Falls back to the Content-Length header when the body
 *  isn't a readable stream (jsdom / older runtimes). Never changes the response
 *  the caller sees (success or failure passes through). */
export function makeInstrumentedFetch(opts: InstrumentOptions): typeof fetch {
  const base = opts.fetchImpl ?? (typeof fetch === "function" ? fetch.bind(globalThis) : null);
  const clock = opts.now ?? (typeof performance !== "undefined" ? () => performance.now() : () => Date.now());
  if (!base) {
    // No transport — return a fetch that rejects, matching the platform behaviour.
    return (() => Promise.reject(new Error("no fetch transport"))) as unknown as typeof fetch;
  }

  const instrumented = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const start = clock();
    const res = await base(input, init);
    // Prefer the actual transferred size when we can stream it; otherwise trust
    // Content-Length. We must not consume the caller's body, so clone first.
    const len = Number(res.headers?.get?.("content-length") ?? "");
    const emit = (bytes: number) => {
      const durationMs = clock() - start;
      if (bytes > 0 && durationMs > 0) opts.onSample({ bytes, durationMs });
    };

    if (res.body && typeof res.clone === "function") {
      // Measure the clone's bytes off the critical path so the caller's
      // res.blob()/arrayBuffer() is unaffected.
      void measureClone(res.clone(), len, emit);
    } else if (Number.isFinite(len) && len > 0) {
      emit(len);
    }
    return res;
  };
  return instrumented as unknown as typeof fetch;
}

async function measureClone(clone: Response, headerLen: number, emit: (bytes: number) => void): Promise<void> {
  try {
    const reader = clone.body?.getReader?.();
    if (!reader) {
      if (Number.isFinite(headerLen) && headerLen > 0) emit(headerLen);
      return;
    }
    let bytes = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value?.byteLength ?? 0;
    }
    emit(bytes || headerLen || 0);
  } catch {
    if (Number.isFinite(headerLen) && headerLen > 0) emit(headerLen);
  }
}

/** Convenience: wire an estimator directly. */
export function instrumentForEstimator(
  estimator: BandwidthEstimator,
  fetchImpl?: typeof fetch,
  now?: () => number,
): typeof fetch {
  return makeInstrumentedFetch({
    fetchImpl,
    now,
    onSample: (s) => estimator.addSample(s),
  });
}
