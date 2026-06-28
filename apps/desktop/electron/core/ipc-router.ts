/**
 * Typed, validated, least-privilege IPC routing — pure, Electron-free.
 *
 * `ipcMain.handle` gives you one untyped callback per channel and no central
 * place to enforce a contract. This router sits *underneath* that: main.ts
 * registers strongly-typed handlers here, and a single `ipcMain.handle` shim
 * dispatches every `kinora:*` invoke through `IpcRouter.dispatch`, which:
 *   1. rejects any channel not on the {@link INVOKE_CHANNELS} allowlist,
 *   2. rejects payloads that fail the channel's validator,
 *   3. catches handler throws and returns a structured `IpcResult` error
 *      (so a bug in one handler can never reject the renderer's promise in a
 *      way that leaks a stack trace or hangs the call).
 *
 * Because none of this imports `electron`, the entire routing + validation
 * layer is unit-testable with plain objects.
 */
import type { InvokeChannel, InvokeChannels } from "../shared/ipc-contract.js";
import { isInvokeChannel } from "../shared/ipc-contract.js";

/** Per-call provenance the router forwards to handlers (filled by the shim). */
export interface IpcCallContext {
  /** WebContents id of the calling renderer (for per-window scoping). */
  senderId: number;
  /** The frame URL, used to reject calls from unexpected origins. */
  senderUrl: string;
}

export type IpcHandler<C extends InvokeChannel> = (
  payload: InvokeChannels[C]["request"],
  ctx: IpcCallContext,
) => InvokeChannels[C]["response"] | Promise<InvokeChannels[C]["response"]>;

/** A runtime validator for a channel payload. Return true if acceptable. */
export type IpcValidator<C extends InvokeChannel> = (payload: unknown) => payload is InvokeChannels[C]["request"];

export type IpcResult<T> = { ok: true; value: T } | { ok: false; error: IpcError };

export interface IpcError {
  code: "unknown-channel" | "invalid-payload" | "forbidden-origin" | "handler-error" | "no-handler";
  message: string;
}

export interface IpcRouterOptions {
  /**
   * Origins allowed to call the bridge. A frame URL is accepted if it starts
   * with one of these. Defaults allow the dev server + packaged `file:`/app
   * protocol. Pass your own to lock it down further.
   */
  allowedOriginPrefixes?: string[];
  /** Structured log hook (level, message, data). */
  onLog?: (level: "warn" | "error" | "info", message: string, data?: Record<string, unknown>) => void;
}

const DEFAULT_ORIGIN_PREFIXES = ["http://localhost:5173", "file://", "app://", "kinora://"];

export class IpcRouter {
  private readonly handlers = new Map<InvokeChannel, IpcHandler<InvokeChannel>>();
  private readonly validators = new Map<InvokeChannel, IpcValidator<InvokeChannel>>();
  private readonly originPrefixes: string[];
  private readonly onLog: NonNullable<IpcRouterOptions["onLog"]>;

  constructor(opts: IpcRouterOptions = {}) {
    this.originPrefixes = opts.allowedOriginPrefixes ?? DEFAULT_ORIGIN_PREFIXES;
    this.onLog = opts.onLog ?? (() => {});
  }

  /** Register the handler (and optional validator) for one invoke channel. */
  handle<C extends InvokeChannel>(channel: C, handler: IpcHandler<C>, validator?: IpcValidator<C>): this {
    if (!isInvokeChannel(channel)) {
      throw new Error(`IpcRouter.handle: "${channel}" is not an allowlisted invoke channel`);
    }
    if (this.handlers.has(channel)) {
      throw new Error(`IpcRouter.handle: duplicate handler for "${channel}"`);
    }
    this.handlers.set(channel, handler as unknown as IpcHandler<InvokeChannel>);
    if (validator) this.validators.set(channel, validator as unknown as IpcValidator<InvokeChannel>);
    return this;
  }

  /** True once every allowlisted invoke channel has a handler. */
  isComplete(allowlist: readonly InvokeChannel[]): boolean {
    return allowlist.every((c) => this.handlers.has(c));
  }

  /** Channels on the allowlist that still lack a handler. */
  missing(allowlist: readonly InvokeChannel[]): InvokeChannel[] {
    return allowlist.filter((c) => !this.handlers.has(c));
  }

  isOriginAllowed(url: string): boolean {
    if (!url) return false;
    return this.originPrefixes.some((p) => url.startsWith(p));
  }

  /**
   * The single dispatch path. Validates the channel, origin and payload, then
   * runs the handler, always returning a structured {@link IpcResult} (never
   * throws). The `electron`-bound shim unwraps `ok:false` into a rejected
   * promise carrying only `error.message`.
   */
  async dispatch(channel: unknown, payload: unknown, ctx: IpcCallContext): Promise<IpcResult<unknown>> {
    if (!isInvokeChannel(channel)) {
      this.onLog("warn", "ipc: rejected unknown channel", { channel: String(channel), sender: ctx.senderId });
      return { ok: false, error: { code: "unknown-channel", message: `Unknown channel: ${String(channel)}` } };
    }
    if (!this.isOriginAllowed(ctx.senderUrl)) {
      this.onLog("warn", "ipc: rejected forbidden origin", { channel, origin: ctx.senderUrl });
      return { ok: false, error: { code: "forbidden-origin", message: "Origin not allowed" } };
    }
    const validator = this.validators.get(channel);
    if (validator && !validator(payload)) {
      this.onLog("warn", "ipc: rejected invalid payload", { channel });
      return { ok: false, error: { code: "invalid-payload", message: `Invalid payload for ${channel}` } };
    }
    const handler = this.handlers.get(channel);
    if (!handler) {
      this.onLog("error", "ipc: no handler registered", { channel });
      return { ok: false, error: { code: "no-handler", message: `No handler for ${channel}` } };
    }
    try {
      const value = await handler(payload as never, ctx);
      return { ok: true, value };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.onLog("error", "ipc: handler threw", { channel, message });
      return { ok: false, error: { code: "handler-error", message } };
    }
  }
}

// ---------------------------------------------------------------------------
// Reusable validators — small, total, and side-effect free.
// ---------------------------------------------------------------------------

export const v = {
  void: (p: unknown): p is void => p === undefined || p === null,

  notify: (p: unknown): p is InvokeChannels["kinora:notify"]["request"] =>
    isObj(p) && isStr(p.title) && isStr(p.body),

  tokenSet: (p: unknown): p is InvokeChannels["kinora:token:set"]["request"] =>
    isObj(p) && (p.token === null || isStr(p.token)),

  prefsGet: (p: unknown): p is InvokeChannels["kinora:prefs:get"]["request"] => isObj(p) && isStr(p.key),

  prefsSet: (p: unknown): p is InvokeChannels["kinora:prefs:set"]["request"] =>
    isObj(p) && isStr(p.key) && "value" in p,

  logsTail: (p: unknown): p is InvokeChannels["kinora:logs:tail"]["request"] =>
    p === undefined || p === null || (isObj(p) && (p.limit === undefined || isFiniteNum(p.limit))),

  windowOpen: (p: unknown): p is InvokeChannels["kinora:window:open"]["request"] =>
    p === undefined || p === null || (isObj(p) && (p.route === undefined || isStr(p.route))),

  openExternal: (p: unknown): p is InvokeChannels["kinora:open-external"]["request"] =>
    isObj(p) && isStr(p.url),
} as const;

function isObj(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null;
}
function isStr(x: unknown): x is string {
  return typeof x === "string";
}
function isFiniteNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}
