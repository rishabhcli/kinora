// The extended Playwright `test` for the Kinora E2E suite.
//
// Fixtures provided:
//   - api:     an ApiMock (installed by default; deterministic backend + SSE shim)
//   - login / home / library / upload / director: page objects bound to `page`
//   - app:     a one-call helper that signs in offline and returns the HomePage
//
// Two execution modes, chosen by env so the same specs run both ways:
//   - KINORA_E2E_MOCK !== "0"  (default): network is mocked — fully hermetic,
//     no backend, no Docker, no live Wan. This is what CI runs.
//   - KINORA_E2E_MOCK === "0": no mock installed; specs talk to whatever backend
//     the dev server is configured against (VITE_KINORA_API_URL). Useful for a
//     real-stack smoke. KINORA_LIVE_VIDEO must still be OFF.
//
// Network-dependent specs (the real-backend smokes) are additionally gated by
// KINORA_E2E_LIVE=1 via the `liveOnly` tag check in those specs.

import { test as base, expect, type Page } from "@playwright/test";
import { ApiMock, type ApiMockOptions } from "../mocks/apiMock";
import {
  LoginScreen,
  HomePage,
  LibraryPage,
  UploadFlow,
  DirectorStudio,
} from "../pageobjects";
import { freezeMotion } from "../support/stabilize";
import { startPerfObserver } from "../support/perf";
import { DEMO_CREDENTIALS } from "./seed";

export const MOCK_ENABLED = process.env.KINORA_E2E_MOCK !== "0";
export const LIVE_ENABLED = process.env.KINORA_E2E_LIVE === "1";

interface Fixtures {
  apiOptions: ApiMockOptions;
  api: ApiMock | null;
  login: LoginScreen;
  home: HomePage;
  library: LibraryPage;
  upload: UploadFlow;
  director: DirectorStudio;
  /** Sign in (offline-safe) and return a ready HomePage. */
  app: HomePage;
  /** Whether motion/clock/random freezing is applied (default true). */
  frozen: boolean;
}

// Holder fixture: a tiny mutable box the `page` fixture fills with the installed
// mock, and the `api` fixture reads back. This lets the mock be installed inside
// the `page` setup (before any navigation) WITHOUT `page` depending on `api`
// (which would be a dependency cycle).
interface ApiHolder {
  mock: ApiMock | null;
}

export const test = base.extend<Fixtures & { _apiHolder: ApiHolder }>({
  // Per-test override point: a spec can do `test.use({ apiOptions: { offline: true } })`.
  apiOptions: [{}, { option: true }],
  frozen: [true, { option: true }],

  _apiHolder: async ({}, use) => {
    await use({ mock: null });
  },

  // The `page` fixture is the single setup seam: it installs the deterministic
  // mock + EventSource shim, the perf observer, and the motion freeze BEFORE any
  // navigation. The mock MUST be installed here (not in a separate `api` fixture
  // depending on `page`) so unmocked calls never leak to whatever real backend
  // happens to be on :8000 (e.g. a developer's running stack) — that would make
  // specs non-hermetic and flaky.
  page: async ({ page, apiOptions, frozen, _apiHolder }, use) => {
    if (MOCK_ENABLED) {
      _apiHolder.mock = new ApiMock(page, apiOptions);
      await _apiHolder.mock.install();
    }
    await startPerfObserver(page);
    if (frozen) await freezeMotion(page);
    await use(page);
  },

  api: async ({ _apiHolder }, use) => {
    await use(_apiHolder.mock);
  },

  login: async ({ page }, use) => use(new LoginScreen(page)),
  home: async ({ page }, use) => use(new HomePage(page)),
  library: async ({ page }, use) => use(new LibraryPage(page)),
  upload: async ({ page }, use) => use(new UploadFlow(page)),
  director: async ({ page }, use) => use(new DirectorStudio(page)),

  app: async ({ login, home }, use) => {
    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    await use(home);
  },
});

export { expect, DEMO_CREDENTIALS };
export type { Page };
