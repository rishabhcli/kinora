# Audit notes for Agent 11 (Login) — from Agent 06 (a11y)

**Good news:** an axe-core scan of the login screen (`/`) at base `4863a0c` reported
**0 violations** (report: `artifacts/agent-08/axe-login-full.json`). `autoFocus` on the
email field (`LoginPage.tsx:76`) and the frosted card are fine.

Keep it that way as you iterate:
- Maintain associated `<label>`s for every field; if you add inline validation, link
  errors with `aria-describedby` and announce them with
  `announce(msg, "assertive")` from `@/a11y/announce`.
- The login input had a bare `:focus { outline:none }` (`index.css:906`); the new
  global `:focus-visible` ring (a11y.css) now covers keyboard focus — don’t add a new
  outline-suppressing rule.
- The app-wide skip link + `?` cheat-sheet now render on the login screen via
  `A11yProvider` (main.tsx) — no action needed; just don’t intercept Tab/`?`.
- Re-run `pnpm --filter @kinora/desktop test:a11y` (the `login-*` specs) after changes.
