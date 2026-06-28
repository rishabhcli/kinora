import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const deleteAccount = vi.hoisted(() => vi.fn());
const requestDataExport = vi.hoisted(() => vi.fn());
vi.mock("../../lib/api/account", () => ({ deleteAccount, requestDataExport }));

import DangerZone from "./DangerZone";

beforeEach(() => {
  deleteAccount.mockReset().mockResolvedValue({ deletedAt: Date.UTC(2030, 0, 1) });
  requestDataExport.mockReset().mockResolvedValue({ jobId: "x" });
});

describe("DangerZone", () => {
  it("requests a data export", async () => {
    render(<DangerZone email="a@x.com" />);
    fireEvent.click(screen.getByRole("button", { name: /export/i }));
    await waitFor(() => expect(requestDataExport).toHaveBeenCalled());
    expect(screen.getByText(/preparing your export/i)).toBeInTheDocument();
  });

  it("gates deletion on typing the exact email", async () => {
    const onDeleted = vi.fn();
    render(<DangerZone email="a@x.com" onDeleted={onDeleted} />);
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));

    const confirmBtn = screen.getByRole("button", { name: /delete my account/i });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/confirm your email/i), { target: { value: "wrong@x.com" } });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/confirm your email/i), { target: { value: "A@X.com" } }); // case-insensitive
    expect(confirmBtn).toBeEnabled();

    fireEvent.click(confirmBtn);
    await waitFor(() => expect(deleteAccount).toHaveBeenCalledWith("a@x.com"));
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
    expect(screen.getByText(/scheduled for deletion/i)).toBeInTheDocument();
  });

  it("can cancel the delete confirmation", () => {
    render(<DangerZone email="a@x.com" />);
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByLabelText(/confirm your email/i)).not.toBeInTheDocument();
  });
});
