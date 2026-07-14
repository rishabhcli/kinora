// App-wide store singletons for the Library + Director domain. The Director
// Studio mounts/unmounts repeatedly; if each mount made its own store the data
// would still survive (it's localStorage-backed) but subscribers wouldn't see
// cross-mount updates within a single session. These lazy singletons give the
// whole app ONE instance per store, so the reading room recording a reading
// event and the Director Studio's analytics tab observe the same data live.
//
// They are lazy (created on first access) so importing this module is cheap and
// SSR/test-safe. Tests that want isolation still pass their own injected store
// to the components/factories directly — these singletons are the *default*.
import { createAnnotationStore, type AnnotationStore } from "./annotations";
import { createAnalyticsStore, type AnalyticsStore, type ReadingEvent } from "./analytics";
import { createCollectionStore, type CollectionStore } from "./collections";

let _annotations: AnnotationStore | null = null;
let _analytics: AnalyticsStore | null = null;
let _collections: CollectionStore | null = null;

export function annotationStore(): AnnotationStore {
  if (!_annotations) _annotations = createAnnotationStore();
  return _annotations;
}

export function analyticsStore(): AnalyticsStore {
  if (!_analytics) _analytics = createAnalyticsStore();
  return _analytics;
}

export function collectionStore(): CollectionStore {
  if (!_collections) _collections = createCollectionStore();
  return _collections;
}

/** Cross-domain integration point for reading analytics.
 *
 * The reading room (a different domain) should call this on each session tick
 * with the words advanced + wall-clock seconds spent, so the Director Studio's
 * reading-analytics dashboard reflects real pace/time/streaks. It writes to the
 * shared analytics singleton — zero coupling beyond this one function.
 *
 * Safe to call with garbage: non-positive `seconds` is dropped, words clamp ≥0.
 */
export function recordReading(bookId: string, words: number, seconds: number, at: number = Date.now()): void {
  const event: ReadingEvent = { book_id: bookId, words, seconds, at };
  analyticsStore().record(event);
}

/** Test/utility hook: reset the singletons (e.g. between integration tests). */
export function __resetStoresForTests(): void {
  _annotations = null;
  _analytics = null;
  _collections = null;
}
