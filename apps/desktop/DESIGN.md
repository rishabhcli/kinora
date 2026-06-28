# DESIGN.md — Frontend Auth, Onboarding & Account (Agent: auth-account)

This is the living roadmap for the **account system** domain of the Kinora
desktop renderer. It is owned by a single agent working in an isolated worktree.

## Scope (owned files)

- `src/lib/account/**` — **NEW.** Pure, framework-free, DOM-free core logic
  (the project's signature pattern, mirrors `lib/api/collections.ts`,
  `lib/api/analytics.ts`, `lib/settings.ts`). Injectable KV stores, pure
  derivations, no React. Fully unit-tested under vitest.
- `src/lib/api/account.ts`, `src/lib/api/sessions.ts`, `src/lib/api/billing.ts`
  — **NEW.** Thin backend adapters composing against the shared `http`
  primitive exported from `lib/api.ts`. They never edit `lib/api.ts`.
- `src/components/auth/**` — auth screen pieces (additive).
- `src/components/account/**` — **NEW.** Account-management surface: profile,
  preferences, security (MFA, passkeys), sessions/devices, billing UI.
- `src/components/onboarding/**` — **NEW.** Guided first-run flow.
- `src/components/LoginPage.tsx` — auth entry (additive enhancements only;
  round-1 may also touch routing).

## Out of scope (DO NOT TOUCH — owned by other agents)

- `src/components/director/**`, `src/components/library/**`,
  `src/components/settings/**`, the reading room (`src/reading/**`).
- `src/lib/api.ts` is a **shared seam** (Agent 12). Additive only — we **import**
  `http`, `auth`, `ApiError`, `BASE`; we never edit it. No new exports added here.
- `src/App.tsx` routing — additive only, documented below.

## Additive shared-file changes

- **None.** No shared file was edited. All new API modules live in
  `lib/api/*.ts` and compose against the exported `http`/`auth`/`ApiError`/`BASE`
  from `lib/api.ts` (read-only import). `App.tsx`, `i18n/`, and the shared
  `styles/index.css` aggregator were **not** touched.
- i18n: the account/onboarding components use **literal English strings**, the
  same convention as the existing auth components (`Field.tsx`,
  `PasswordField.tsx`: "Show password", "Caps Lock is on"). The repo's i18n
  catalog is typed (`CustomTypeOptions` ties `t()` to `en.json`), so adding
  unreferenced keys would be dead weight — and editing `i18n/` is out of lane.
  When the host wires these panels in, it can lift strings into the catalog as
  an additive pass.
- CSS: `components/account/account.css` is a new owned partial imported directly
  by `AccountPage`/`OnboardingFlow` (the `SettingsPage.tsx` precedent of
  `import "./settings/settings.css"`), reusing the `--auth-*` tokens. The shared
  `styles/index.css` aggregator is untouched.

## Design principles (match the codebase)

1. **Pure core, thin shell.** All logic that *can* be pure + synchronously
   testable lives in `lib/account/*` with an injectable KV store. Components are
   thin and call into the core. This is exactly how `collections.ts`/`analytics.ts`
   are built.
2. **No network in the core.** Backend calls are isolated in `lib/api/*.ts`
   adapters that compose against `http`. The pure core is offline-deterministic.
3. **Graceful offline / demo mode.** Like `LoginPage.enter()`, every flow
   degrades to a local demo path when the backend is down — the app never blocks.
4. **a11y first.** Reuse the `Field`/`PasswordField` patterns: visible labels,
   `aria-invalid`+`aria-describedby`, reserved error lines, live regions.
5. **Tests are DOM-free where possible** (vitest, injectable stores). Component
   tests use @testing-library/react like the director tests.

## Status — all milestones complete

Full desktop gate green: **62 vitest files / 451 tests** + 24 node-test files,
`typecheck` clean, production `build` succeeds. (Baseline was 35 files / 233
tests — this domain added ~27 files / ~218 tests.)

| Milestone | Status | Key locations |
|---|---|---|
| M1 — pure account core | ✅ | `src/lib/account/*` (store, session, mfa, passkey, oauth, profile, preferences, password, onboarding, taste, billing, usage, audit) |
| M2 — API adapters | ✅ | `src/lib/api/{account,sessions,billing}.ts` |
| M3 — auth components | ✅ | `src/components/auth/*` (useAuth, SignInForm, SignUpForm, MfaChallenge, ForgotPassword, OAuthButtons, PasskeyButton, LoginPanel) |
| M4 — account surface | ✅ | `src/components/account/*` (AccountPage + Profile/Security/Sessions/Billing/Preferences sections, MfaEnrollDialog, PasskeysCard, RecentActivityCard, UsageCard, DangerZone, primitives) |
| M5 — onboarding | ✅ | `src/components/onboarding/*` (OnboardingFlow + 6 steps) |
| M6 — depth (audit/usage/danger) | ✅ | `lib/account/{audit,usage}.ts`, `RecentActivityCard`, `UsageCard`, `DangerZone` + adapter `listSecurityEvents`/`getUsage` |

## Milestones

### M1 — Pure account core (lib/account)
- `session.ts` — device/session model, parsing, revocation, current-session
  detection, sorting, relative-time formatting. Injectable store.
- `mfa.ts` — TOTP secret/URI modeling, recovery codes, MFA enrollment state
  machine, code-format validation (RFC 6238 shapes, no crypto).
- `passkey.ts` — WebAuthn capability detection + credential registry modeling.
- `oauth.ts` — OAuth/SSO provider registry, PKCE-state generation (pure,
  injectable RNG), state round-trip validation, callback parsing.
- `onboarding.ts` — first-run step machine: ordered steps, completion tracking,
  resumable progress, skip rules, persisted to KV.
- `profile.ts` — profile model + validation, initials/avatar-color derivation.
- `preferences.ts` — account-level preferences over an injectable store.
- `billing.ts` — subscription plans, entitlements, proration math, invoice
  formatting, trial countdown — all pure.
- `password.ts` — richer password policy, breach-shape heuristics, confirm-match.
- `index.ts` — barrel.

### M2 — API adapters (lib/api)
- `account.ts` · `sessions.ts` · `billing.ts` (compose against `http`).

### M3 — Auth components
- `useAuth.ts` + `AuthProvider`, `SignInForm`/`SignUpForm`, `OAuthButtons`,
  `PasskeyButton`, `MfaChallenge`, `ForgotPassword`.

### M4 — Account management surface
- `AccountPage` shell + `ProfileSection`, `SecuritySection`, `SessionsSection`,
  `BillingSection`, `PreferencesSection`.

### M5 — Onboarding
- `OnboardingFlow` — multi-step guided first-run, resumable.

### Roadmap / future
- Wire `AccountPage`/`OnboardingFlow` into App routing once round-1 settles the
  router seam (additive route entry).
- Swap demo adapters for live endpoints when the backend grows
  `/auth/sessions`, `/auth/mfa`, `/me/billing`.
