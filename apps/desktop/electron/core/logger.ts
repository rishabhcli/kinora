/**
 * Structured logging for the main process.
 *
 * The core is a pure in-memory ring buffer + level filter (no Electron, no fs)
 * so it is unit-testable and safe to import from anywhere. A separate file sink
 * (see {@link createFileSink}) is wired in by main.ts when a writable log dir is
 * known. The diagnostics panel reads {@link Logger.tail} over IPC.
 */
import type { LogEntry, LogLevel } from "../shared/ipc-contract.js";

export type { LogEntry, LogLevel };

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

/** A destination for finalized log lines (console, file, remote, …). */
export interface LogSink {
  write(entry: LogEntry): void;
  flush?(): void | Promise<void>;
}

export interface LoggerOptions {
  /** Minimum level to record. Default `info` (or `debug` when `KINORA_DEBUG`). */
  level?: LogLevel;
  /** Max entries kept in the in-memory ring (for the diagnostics tail). */
  ringSize?: number;
  /** A clock injected for deterministic tests. */
  now?: () => number;
  /** Initial sinks. More can be added later via {@link Logger.addSink}. */
  sinks?: LogSink[];
}

export class Logger {
  private level: LogLevel;
  private readonly ringSize: number;
  private readonly ring: LogEntry[] = [];
  private readonly sinks: LogSink[];
  private readonly now: () => number;

  constructor(opts: LoggerOptions = {}) {
    this.level = opts.level ?? "info";
    this.ringSize = Math.max(16, opts.ringSize ?? 500);
    this.now = opts.now ?? Date.now;
    this.sinks = [...(opts.sinks ?? [])];
  }

  setLevel(level: LogLevel): void {
    this.level = level;
  }

  addSink(sink: LogSink): void {
    this.sinks.push(sink);
  }

  /** A namespaced logger that prefixes every entry with `scope`. */
  scoped(scope: string): ScopedLogger {
    return {
      debug: (m, d) => this.log("debug", scope, m, d),
      info: (m, d) => this.log("info", scope, m, d),
      warn: (m, d) => this.log("warn", scope, m, d),
      error: (m, d) => this.log("error", scope, m, d),
    };
  }

  log(level: LogLevel, scope: string, message: string, data?: Record<string, unknown>): void {
    if (LEVEL_ORDER[level] < LEVEL_ORDER[this.level]) return;
    const entry: LogEntry = {
      ts: this.now(),
      level,
      scope,
      message: String(message),
      ...(data && Object.keys(data).length > 0 ? { data: redact(data) } : {}),
    };
    this.ring.push(entry);
    if (this.ring.length > this.ringSize) this.ring.shift();
    for (const sink of this.sinks) {
      try {
        sink.write(entry);
      } catch {
        /* a broken sink must never crash the app */
      }
    }
  }

  /** Most-recent `limit` entries (default all), oldest→newest. */
  tail(limit?: number): LogEntry[] {
    if (limit == null || limit >= this.ring.length) return [...this.ring];
    return this.ring.slice(this.ring.length - Math.max(0, limit));
  }

  count(): number {
    return this.ring.length;
  }

  async flush(): Promise<void> {
    await Promise.all(this.sinks.map((s) => Promise.resolve(s.flush?.())));
  }
}

export interface ScopedLogger {
  debug(message: string, data?: Record<string, unknown>): void;
  info(message: string, data?: Record<string, unknown>): void;
  warn(message: string, data?: Record<string, unknown>): void;
  error(message: string, data?: Record<string, unknown>): void;
}

/**
 * A console sink that pretty-prints `[level] scope: message {data}`. Used in dev
 * and as a fallback. Pure w.r.t. Electron; takes the console to use.
 */
export function createConsoleSink(out: Pick<Console, "log" | "warn" | "error"> = console): LogSink {
  return {
    write(e) {
      const line = `[${e.level}] ${e.scope}: ${e.message}`;
      const args: unknown[] = e.data ? [line, e.data] : [line];
      if (e.level === "error") out.error(...args);
      else if (e.level === "warn") out.warn(...args);
      else out.log(...args);
    },
  };
}

/**
 * Keys whose values are masked before they ever reach a sink — tokens, secrets,
 * auth headers. Defence in depth: we never *intentionally* log these, but a
 * future caller might pass a payload that contains one.
 */
const SENSITIVE = /(token|secret|password|authorization|api[_-]?key|cookie|bearer)/i;

function redact(data: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    if (SENSITIVE.test(k)) {
      out[k] = "«redacted»";
    } else if (v && typeof v === "object" && !Array.isArray(v)) {
      out[k] = redact(v as Record<string, unknown>);
    } else {
      out[k] = v;
    }
  }
  return out;
}
