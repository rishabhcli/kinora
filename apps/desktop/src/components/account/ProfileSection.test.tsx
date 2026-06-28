import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const updateProfile = vi.hoisted(() => vi.fn());
vi.mock("../../lib/api/account", () => ({ updateProfile }));

import ProfileSection from "./ProfileSection";
import type { Profile } from "../../lib/account";

const profile: Profile = { id: "u1", email: "ada@x.com", displayName: "Ada" };

beforeEach(() => {
  updateProfile.mockReset().mockImplementation((cur: Profile, patch: Record<string, unknown>) =>
    Promise.resolve({ ...cur, ...patch }),
  );
});

describe("ProfileSection", () => {
  it("disables Save until something changes", () => {
    render(<ProfileSection profile={profile} onSaved={vi.fn()} />);
    expect(screen.getByRole("button", { name: /save changes/i })).toBeDisabled();
  });

  it("saves a changed display name and reports it", async () => {
    const onSaved = vi.fn();
    render(<ProfileSection profile={profile} onSaved={onSaved} />);
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Ada Lovelace" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    await waitFor(() => expect(updateProfile).toHaveBeenCalled());
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(screen.getByText(/^saved$/i)).toBeInTheDocument();
  });

  it("blocks save on an invalid handle", () => {
    render(<ProfileSection profile={profile} onSaved={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/handle/i), { target: { value: "no spaces!" } });
    expect(screen.getByRole("button", { name: /save changes/i })).toBeDisabled();
  });
});
