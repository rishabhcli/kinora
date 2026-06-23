/**
 * A tiny bounded LRU map — a `Map` that evicts its least-recently-used entry once
 * it exceeds `maxSize`. The SyncEngine caches per-shot clips, per-beat keyframes,
 * page illustrations, sync segments and stitched scenes in these so a backward
 * seek is an instant cache hit (§4.8) — but an unbounded cache would grow without
 * limit over a long book. Reading an entry (`get`) refreshes its recency, so the
 * assets *near the playhead* (re-read every recompute, and on every backward
 * seek) stay hot and survive eviction; only far-away, untouched assets are shed.
 *
 * Extends `Map` so it is a drop-in for the engine's existing `Map` fields and
 * keeps every `Map` method available.
 */
export class LruMap<K, V> extends Map<K, V> {
  private readonly maxSize: number;

  constructor(maxSize: number) {
    super();
    this.maxSize = Math.max(1, Math.floor(maxSize));
  }

  /** Read a value, marking it most-recently-used so it resists eviction. */
  override get(key: K): V | undefined {
    if (!super.has(key)) return undefined;
    const value = super.get(key) as V;
    super.delete(key);
    super.set(key, value);
    return value;
  }

  /** Read a value **without** touching recency (for internal scans / prefetch). */
  peek(key: K): V | undefined {
    return super.get(key);
  }

  /** Insert/update, marking it most-recent and evicting the oldest over the cap. */
  override set(key: K, value: V): this {
    super.delete(key);
    super.set(key, value);
    while (super.size > this.maxSize) {
      const oldest = super.keys().next().value as K | undefined;
      if (oldest === undefined) break;
      super.delete(oldest);
    }
    return this;
  }
}
