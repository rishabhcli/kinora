import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import App from "./App";

// vitest globals are disabled, so register Testing Library cleanup explicitly.
afterEach(() => {
  cleanup();
});

describe("App", () => {
  it("renders the Kinora wordmark", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: /kinora/i })).toBeTruthy();
  });

  it("shows the phase-1 status pill", () => {
    render(<App />);
    expect(screen.getByText(/phase 1 scaffold online/i)).toBeTruthy();
  });
});
