// Vitest global setup: jest-dom matchers + automatic React Testing Library
// cleanup between tests. Importing the `/vitest` entry also augments vitest's
// `expect` types, so matchers like `.toBeInTheDocument()` typecheck project-wide.
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
