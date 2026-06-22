import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import { setToken } from "./api/token";
import { useAuthStore } from "./stores/authStore";

afterEach(cleanup);

beforeEach(() => {
  setToken(null);
  useAuthStore.setState({ token: null, user: null, status: "anonymous", error: null });
});

describe("App routing + auth guard", () => {
  it("redirects an unauthenticated visitor to the login screen", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: /welcome back/i })).toBeTruthy();
  });

  it("guards the workspace route too", async () => {
    render(
      <MemoryRouter initialEntries={["/book/abc"]}>
        <App />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: /welcome back/i })).toBeTruthy();
  });
});
