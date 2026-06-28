/**
 * `kinora://` deep-link parsing — pure, dependency-free, unit-testable.
 *
 * Deep links arrive three ways: (1) macOS `open-url`, (2) Windows/Linux as the
 * last `argv` element on a second-instance launch, (3) the CLI. All three feed
 * the same parser so behaviour is identical and the routing logic can be tested
 * without launching Electron.
 *
 * Grammar: `kinora://<action>/<seg>/<seg>?k=v&k2=v2`
 *   action   → the URL host (e.g. `book`, `open`, `auth`)
 *   segments → decoded path segments after the host
 *   params   → query string as a flat map (last wins on duplicate keys)
 */
import type { DeepLink } from "../shared/ipc-contract.js";
import { KINORA_PROTOCOL } from "../shared/ipc-contract.js";

const SCHEME = `${KINORA_PROTOCOL}:`;

/** True if `value` looks like a `kinora://…` URL (scheme match, case-insensitive). */
export function isDeepLink(value: unknown): value is string {
  return typeof value === "string" && value.trim().toLowerCase().startsWith(`${KINORA_PROTOCOL}://`);
}

/**
 * Parse a `kinora://` href into a structured {@link DeepLink}, or `null` if the
 * input is not a well-formed Kinora deep link. Never throws.
 */
export function parseDeepLink(href: unknown): DeepLink | null {
  if (!isDeepLink(href)) return null;
  const raw = href.trim();

  // The URL parser handles percent-decoding, query parsing, and host
  // extraction. We normalise the scheme to lowercase so `Kinora://` works.
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol.toLowerCase() !== SCHEME) return null;

  // `kinora://book/123` → host="book". `kinora:///open` (empty host) is allowed
  // and falls back to the first path segment as the action.
  let action = decodeSafe(url.hostname).toLowerCase();
  const pathSegments = splitPath(url.pathname);

  if (!action && pathSegments.length > 0) {
    action = (pathSegments.shift() as string).toLowerCase();
  }
  if (!action) return null;

  const params: Record<string, string> = {};
  url.searchParams.forEach((value, key) => {
    params[key] = value;
  });

  return {
    action,
    segments: pathSegments,
    params,
    href: raw,
  };
}

/**
 * Scan a process `argv` array and return the first valid deep link, or `null`.
 * Used on Windows/Linux where the OS appends the URL as a launch argument.
 */
export function findDeepLinkInArgv(argv: readonly string[]): DeepLink | null {
  for (const arg of argv) {
    const link = parseDeepLink(arg);
    if (link) return link;
  }
  return null;
}

/**
 * Classify a deep link into a renderer route string the UI can navigate to.
 * Kept here (pure) so the mapping is testable. Returns `null` for unknown
 * actions so the caller can decide whether to ignore or log them.
 */
export function deepLinkToRoute(link: DeepLink): string | null {
  switch (link.action) {
    case "book":
    case "open": {
      const id = link.segments[0] ?? link.params.id;
      return id ? `/book/${encodeURIComponent(id)}` : "/library";
    }
    case "library":
      return "/library";
    case "settings":
      return "/settings";
    case "auth": {
      // kinora://auth/callback?token=… — surfaces to the renderer as a route;
      // the token itself is NOT embedded in the path (it stays in params).
      const sub = link.segments[0] ?? "callback";
      return `/auth/${encodeURIComponent(sub)}`;
    }
    case "diagnostics":
      return "/diagnostics";
    default:
      return null;
  }
}

function splitPath(pathname: string): string[] {
  return pathname
    .split("/")
    .filter((s) => s.length > 0)
    .map(decodeSafe);
}

function decodeSafe(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}
