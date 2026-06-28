/**
 * Pagination helpers for the Kinora SDK.
 *
 * The Kinora list endpoints (`GET /books`, `GET /books/{id}/shots`, ...) return
 * a *bare array* today — there is no cursor/offset envelope. To give callers a
 * uniform, future-proof iteration API, the SDK wraps any array-returning fetch
 * in a {@link Page}: it exposes the items, an async iterator, and a
 * `collect()`. A `pageSize` + client-side slicing lets callers stream large
 * arrays in chunks without holding everything if they prefer; when the backend
 * grows real server-side pagination, `Page` is the seam to evolve.
 */

/** A lazily-iterable page of items over a fetched array. */
export class Page<T> implements AsyncIterable<T> {
  /** All items in this page (the full bare array the backend returned). */
  readonly items: readonly T[];
  /** The chunk size used by {@link chunks}. */
  readonly pageSize: number;

  constructor(items: readonly T[], pageSize = 100) {
    this.items = items;
    this.pageSize = Math.max(1, pageSize);
  }

  /** Number of items. */
  get length(): number {
    return this.items.length;
  }

  /** Async-iterate every item (so callers can `for await (const x of page)`). */
  async *[Symbol.asyncIterator](): AsyncIterator<T> {
    for (const item of this.items) yield item;
  }

  /** Synchronously iterate every item. */
  *[Symbol.iterator](): Iterator<T> {
    for (const item of this.items) yield item;
  }

  /** Collect every item into a plain array. */
  collect(): T[] {
    return [...this.items];
  }

  /** Yield fixed-size chunks of `pageSize` items. */
  *chunks(): Generator<T[]> {
    for (let i = 0; i < this.items.length; i += this.pageSize) {
      yield this.items.slice(i, i + this.pageSize);
    }
  }

  /** Map each item, preserving page metadata. */
  map<U>(fn: (item: T, index: number) => U): Page<U> {
    return new Page(this.items.map(fn), this.pageSize);
  }

  /** Filter items, preserving page metadata. */
  filter(fn: (item: T, index: number) => boolean): Page<T> {
    return new Page(this.items.filter(fn), this.pageSize);
  }

  /** The first item, or undefined. */
  first(): T | undefined {
    return this.items[0];
  }
}
