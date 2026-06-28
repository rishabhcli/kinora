import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listSessions = vi.hoisted(() => vi.fn());
const revokeSession = vi.hoisted(() => vi.fn());
const revokeOtherSessions = vi.hoisted(() => vi.fn());
vi.mock("../../lib/api/sessions", () => ({ listSessions, revokeSession, revokeOtherSessions }));

import SessionsSection from "./SessionsSection";
import { SESSION_CACHE_KEY } from "../../lib/account";

beforeEach(() => {
  try {
    localStorage.removeItem(SESSION_CACHE_KEY);
  } catch {
    /* ignore */
  }
  listSessions.mockReset().mockResolvedValue([
    { id: "cur", kind: "desktop", label: "MacBook Pro", current: true, last_seen_at: Date.now() },
    { id: "other", kind: "mobile", label: "iPhone", last_seen_at: Date.now() - 3_600_000 },
  ]);
  revokeSession.mockReset().mockResolvedValue(true);
  revokeOtherSessions.mockReset().mockResolvedValue(true);
});

describe("SessionsSection", () => {
  it("lists devices and marks the current one", async () => {
    render(<SessionsSection />);
    await waitFor(() => expect(screen.getByText("MacBook Pro")).toBeInTheDocument());
    expect(screen.getByText("iPhone")).toBeInTheDocument();
    expect(screen.getByText(/this device/i)).toBeInTheDocument();
  });

  it("revokes another device (and offers sign-out-everywhere)", async () => {
    render(<SessionsSection />);
    await waitFor(() => expect(screen.getByText("iPhone")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /everywhere else/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^sign out$/i }));
    await waitFor(() => expect(revokeSession).toHaveBeenCalledWith("other"));
    await waitFor(() => expect(screen.queryByText("iPhone")).not.toBeInTheDocument());
  });
});
