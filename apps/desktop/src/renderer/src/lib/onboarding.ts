/** First-run onboarding gate. Persisted in localStorage (independent of auth, so
 *  signing out never re-triggers the intro). Survives in the WKWebView too. */
const KEY = "kinora.onboarded.v1";

export function hasOnboarded(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function setOnboarded(): void {
  try {
    localStorage.setItem(KEY, "1");
  } catch {
    // best-effort
  }
}
