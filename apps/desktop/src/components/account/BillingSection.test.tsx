import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";

const getSubscription = vi.hoisted(() => vi.fn());
const startCheckout = vi.hoisted(() => vi.fn());
const changePlan = vi.hoisted(() => vi.fn());
const cancelSubscription = vi.hoisted(() => vi.fn());
const getUsage = vi.hoisted(() => vi.fn());
vi.mock("../../lib/api/billing", () => ({
  getSubscription,
  startCheckout,
  changePlan,
  cancelSubscription,
  getUsage,
}));

import BillingSection from "./BillingSection";
import { freeSubscription, parseSubscription } from "../../lib/account";

beforeEach(() => {
  getSubscription.mockReset().mockResolvedValue(freeSubscription());
  startCheckout.mockReset().mockResolvedValue({ demo: true });
  changePlan.mockReset().mockImplementation((plan: string, interval: string) =>
    Promise.resolve(parseSubscription({ plan_id: plan, interval, status: "active" })),
  );
  cancelSubscription.mockReset().mockResolvedValue({ ...freeSubscription(), cancelAtPeriodEnd: true });
  getUsage.mockReset().mockResolvedValue({ videoSeconds: 60, directorEdits: 1, concurrentFilms: 0 });
});

describe("BillingSection", () => {
  it("shows the three plans and the current (free) plan", async () => {
    render(<BillingSection />);
    await waitFor(() => expect(screen.getByText("Cinephile")).toBeInTheDocument());
    expect(screen.getByText("Studio")).toBeInTheDocument();
    expect(screen.getByText(/free Reader plan/i)).toBeInTheDocument();
    // current plan button is disabled
    expect(screen.getByRole("button", { name: /current plan/i })).toBeDisabled();
  });

  it("choosing a paid plan in demo mode updates locally + shows a notice", async () => {
    render(<BillingSection />);
    await waitFor(() => expect(screen.getByText("Cinephile")).toBeInTheDocument());
    // the Cinephile card's CTA — scope to its plan card so we don't hit Studio's.
    const cinephileCard = screen.getByText("Cinephile").closest(".acct-plan") as HTMLElement;
    fireEvent.click(within(cinephileCard).getByRole("button", { name: /^choose$/i }));
    await waitFor(() => expect(startCheckout).toHaveBeenCalledWith("plus", "month"));
    await waitFor(() => expect(screen.getByText(/isn't connected/i)).toBeInTheDocument());
  });

  it("toggling to yearly surfaces the savings", async () => {
    render(<BillingSection />);
    await waitFor(() => expect(screen.getByText("Cinephile")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /yearly/i }));
    expect(screen.getAllByText(/save \d+%/i).length).toBeGreaterThan(0);
  });
});
