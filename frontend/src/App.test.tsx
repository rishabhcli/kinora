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

  it("exposes the core landmarks and a skip link", () => {
    render(<App />);
    expect(screen.getByRole("banner")).toBeTruthy();
    expect(screen.getByRole("main")).toBeTruthy();
    expect(screen.getByRole("contentinfo")).toBeTruthy();
    expect(screen.getByRole("link", { name: /skip to content/i })).toBeTruthy();
  });

  it("renders the two-pane reading preview with a play control", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: /two-pane reading workspace/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /play preview/i })).toBeTruthy();
  });

  it("uses a single top-level h1", () => {
    render(<App />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});
