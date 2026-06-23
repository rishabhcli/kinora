/**
 * Whether the renderer is hosted inside the native macOS shell (apps/desktop-native),
 * which paints its own ~60px glass title strip with the "Kinora" wordmark at the very
 * top of the window. When true, in-app chrome should drop its redundant web wordmark
 * and add a top inset so it sits below the native strip.
 *
 * The flag is a one-time boot value set by the native host on `window`; it never
 * changes for the lifetime of the renderer, so a plain read (no subscription) is enough.
 */
export function useNativeShell(): boolean {
  return (globalThis as { __KINORA_NATIVE__?: boolean }).__KINORA_NATIVE__ === true;
}

/** Top inset (px) that clears the native shell's glass title strip. */
export const NATIVE_TOP_INSET = 64;
