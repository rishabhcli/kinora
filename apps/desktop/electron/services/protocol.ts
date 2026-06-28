/**
 * `kinora://` deep-linking, single-instance enforcement, and OS file-open.
 *
 * Parsing lives in the pure `deep-link` core; this service wires the three OS
 * entry points to it and forwards results to a sink. Because the renderer might
 * not be ready when a deep link arrives at launch, links are *queued* until the
 * renderer signals ready (see {@link ProtocolService.flush}).
 *
 * Single-instance: a second `kinora://…` launch should focus the existing
 * window and forward the URL rather than spawning a new app.
 */
import { app } from "electron";
import path from "node:path";
import { KINORA_PROTOCOL, type AddBookSource, type DeepLink } from "../shared/ipc-contract.js";
import { findDeepLinkInArgv, parseDeepLink } from "../core/deep-link.js";
import { findBookInArgv, isBook } from "../core/book-files.js";
import { DeepLinkQueue } from "../core/deep-link-queue.js";
import type { ScopedLogger } from "../core/logger.js";

export { findBookInArgv, isBook };

export interface ProtocolDeps {
  log: ScopedLogger;
  onDeepLink: (link: DeepLink) => void;
  onOpenBook: (filePath: string, source: AddBookSource) => void;
  /** Called when a second instance wants the primary window focused. */
  onFocusRequested: () => void;
}

export class ProtocolService {
  private readonly deps: ProtocolDeps;
  private readonly queue: DeepLinkQueue;

  constructor(deps: ProtocolDeps) {
    this.deps = deps;
    this.queue = new DeepLinkQueue((link) => deps.onDeepLink(link));
  }

  /**
   * Register Kinora as the default `kinora://` handler. In dev (unpackaged) we
   * must pass the path to the Electron binary + the entry script so the OS
   * relaunches the right process.
   */
  registerProtocol(): void {
    try {
      if (process.defaultApp && process.argv.length >= 2) {
        app.setAsDefaultProtocolClient(KINORA_PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
      } else {
        app.setAsDefaultProtocolClient(KINORA_PROTOCOL);
      }
    } catch (err) {
      this.deps.log.warn("protocol: registration failed", { message: String(err) });
    }
  }

  /**
   * Acquire the single-instance lock. Returns false when another instance owns
   * it (the caller should quit). Wires `second-instance` to forward any deep
   * link / book path the second launch carried.
   */
  acquireSingleInstance(): boolean {
    const gotLock = app.requestSingleInstanceLock();
    if (!gotLock) return false;
    app.on("second-instance", (_event, argv) => {
      this.deps.onFocusRequested();
      const link = findDeepLinkInArgv(argv);
      if (link) {
        this.deliver(link);
        return;
      }
      const book = findBookInArgv(argv);
      if (book) this.deps.onOpenBook(book, "cli");
    });
    return true;
  }

  /** macOS delivers deep links + file-opens via app events, not argv. */
  wireMacEvents(): void {
    app.on("open-url", (event, url) => {
      event.preventDefault();
      const link = parseDeepLink(url);
      if (link) this.deliver(link);
      else this.deps.log.warn("protocol: ignored malformed url", { url });
    });
    app.on("open-file", (event, filePath) => {
      event.preventDefault();
      if (isBook(filePath)) this.deps.onOpenBook(filePath, "file-open");
      else this.deps.log.warn("protocol: ignored non-book open-file", { filePath });
    });
  }

  /** Inspect the initial launch argv (Win/Linux) for a deep link or book. */
  consumeLaunchArgs(argv: readonly string[]): void {
    const link = findDeepLinkInArgv(argv);
    if (link) {
      this.deliver(link);
      return;
    }
    const book = findBookInArgv(argv);
    if (book) this.deps.onOpenBook(book, "cli");
  }

  /** Renderer signalled ready: flush any queued deep links. */
  markRendererReady(): void {
    this.queue.markReady();
  }

  private deliver(link: DeepLink): void {
    this.deps.log.info("deep-link", { action: link.action });
    this.queue.offer(link);
  }
}
