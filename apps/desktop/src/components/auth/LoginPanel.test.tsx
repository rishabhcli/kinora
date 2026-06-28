import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const apiMock = vi.hoisted(() => ({
  isAuthed: vi.fn(() => false),
  loginOrRegister: vi.fn(),
  register: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
}));
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: apiMock };
});

import LoginPanel from "./LoginPanel";

beforeEach(() => {
  apiMock.isAuthed.mockReturnValue(false);
  apiMock.loginOrRegister.mockReset().mockResolvedValue(undefined);
});

describe("LoginPanel", () => {
  it("renders the sign-in view by default with OAuth + demo", () => {
    render(<LoginPanel onEnter={vi.fn()} />);
    expect(screen.getByText(/welcome back/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /continue with google/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /explore the demo/i })).toBeInTheDocument();
  });

  it("switches to sign-up and back", () => {
    render(<LoginPanel onEnter={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /create one/i }));
    expect(screen.getByText(/create your account/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(screen.getByText(/welcome back/i)).toBeInTheDocument();
  });

  it("opens the forgot-password view", () => {
    render(<LoginPanel onEnter={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /forgot password/i }));
    expect(screen.getByText(/reset password/i)).toBeInTheDocument();
  });

  it("calls onEnter after a successful sign-in", async () => {
    const onEnter = vi.fn();
    render(<LoginPanel onEnter={onEnter} />);
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "a@x.com" } });
    fireEvent.change(screen.getByLabelText(/^password$/i), { target: { value: "secretpw" } });
    fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    await waitFor(() => expect(onEnter).toHaveBeenCalled());
  });

  it("enters demo mode immediately", async () => {
    const onEnter = vi.fn();
    render(<LoginPanel onEnter={onEnter} />);
    fireEvent.click(screen.getByRole("button", { name: /explore the demo/i }));
    await waitFor(() => expect(onEnter).toHaveBeenCalled());
  });
});
