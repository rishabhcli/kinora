// Single source of truth for the persisted JWT. Kept in its own tiny module so
// both the auth store and the API client can read/write it without a circular
// import. The token is mirrored to localStorage so a refresh keeps the session.
const TOKEN_KEY = "kinora.jwt";

let cached: string | null | undefined;

export function getToken(): string | null {
  if (cached === undefined) {
    try {
      cached = window.localStorage.getItem(TOKEN_KEY);
    } catch {
      cached = null;
    }
  }
  return cached ?? null;
}

export function setToken(token: string | null): void {
  cached = token;
  try {
    if (token) {
      window.localStorage.setItem(TOKEN_KEY, token);
    } else {
      window.localStorage.removeItem(TOKEN_KEY);
    }
  } catch {
    // Storage may be unavailable (private mode / SSR); the in-memory cache
    // still keeps the token alive for the lifetime of the page.
  }
}
