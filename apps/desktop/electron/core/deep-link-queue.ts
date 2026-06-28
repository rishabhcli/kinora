/**
 * Deep-link delivery queue — pure, Electron-free.
 *
 * Deep links can arrive before the renderer has mounted (e.g. a `kinora://`
 * link that *launches* the app). This queue holds links until the renderer
 * signals ready, then flushes them in order. Extracted from the protocol
 * service so the (subtle) ordering + ready-gating logic is unit-testable.
 */
import type { DeepLink } from "../shared/ipc-contract.js";

export class DeepLinkQueue {
  private readonly pending: DeepLink[] = [];
  private ready = false;
  private readonly deliver: (link: DeepLink) => void;

  constructor(deliver: (link: DeepLink) => void) {
    this.deliver = deliver;
  }

  get isReady(): boolean {
    return this.ready;
  }

  get size(): number {
    return this.pending.length;
  }

  /** Offer a link: delivered immediately if ready, else queued. */
  offer(link: DeepLink): void {
    if (this.ready) {
      this.deliver(link);
    } else {
      this.pending.push(link);
    }
  }

  /** Mark the renderer ready and flush anything queued (FIFO). */
  markReady(): void {
    this.ready = true;
    while (this.pending.length > 0) {
      this.deliver(this.pending.shift() as DeepLink);
    }
  }

  /** Reset to the not-ready state (e.g. on a full reload). */
  reset(): void {
    this.ready = false;
    this.pending.length = 0;
  }
}
