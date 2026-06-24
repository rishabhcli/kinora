/** How long auth calls may hang before we tell the user the backend is unreachable. */
export const AUTH_TIMEOUT_MS = 12_000;

/**
 * Race a promise against a timeout. Resolves `null` when the deadline passes —
 * used so login and boot flows never spin forever on a dead backend.
 */
export async function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), ms);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** User-facing copy when the API is unreachable (login, shelf, boot). */
export const CONNECTION_ERROR =
  "Can't reach Kinora. Make sure the backend is running and try again.";
