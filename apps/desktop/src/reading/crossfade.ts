// Pure reducer behind <CrossfadeFilm>: the current clip stays visible until the
// next one decodes, then they cross-fade (opacity only — GPU-cheap), so the film
// NEVER hard-cuts to black. Capped at two concurrent <video> elements. The React
// component owns the key counter and the DOM; this owns the transitions.

export interface Layer {
  key: number;
  src: string;
  ready: boolean;
}

/** A new target src arrived. Empty src = "generating" → hold the current frame. */
export function pushSrc(layers: readonly Layer[], src: string, nextKey: number): Layer[] {
  if (!src) return layers as Layer[]; // keep the last frame on screen
  if (layers.length === 0) return [{ key: nextKey, src, ready: false }];
  const base = layers[0];
  if (base.src === src) return [base]; // scrolled back to the visible clip — drop incoming
  return [base, { key: nextKey, src, ready: false }]; // base stays until the new one fades in
}

/** The layer with `key` can play. Under reduced motion, promote it immediately. */
export function markReady(layers: readonly Layer[], key: number, reduce: boolean): Layer[] {
  const i = layers.findIndex((l) => l.key === key);
  if (i === -1) return layers as Layer[];
  const next = layers.map((l) => (l.key === key ? { ...l, ready: true } : l));
  if (reduce && i === 1) return [next[1]]; // no fade → swap instantly
  return next;
}

/** The incoming layer finished fading in → drop the base it covered. */
export function promote(layers: readonly Layer[], key: number): Layer[] {
  return layers.length === 2 && layers[1].key === key ? [layers[1]] : (layers as Layer[]);
}
