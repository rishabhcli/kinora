import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.fn();
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, http: (...args: unknown[]) => httpMock(...args) };
});

import { ApiError } from "../api";
import {
  getSubscription,
  startCheckout,
  changePlan,
  cancelSubscription,
  listInvoices,
  getUsage,
} from "./billing";

beforeEach(() => httpMock.mockReset());

describe("getSubscription", () => {
  it("parses a backend subscription", async () => {
    httpMock.mockResolvedValueOnce({ plan_id: "plus", interval: "year", status: "active" });
    const sub = await getSubscription();
    expect(sub).toMatchObject({ planId: "plus", interval: "year", status: "active" });
  });
  it("falls back to a Free subscription when unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect((await getSubscription()).planId).toBe("free");
  });
});

describe("startCheckout", () => {
  it("returns the hosted URL when live", async () => {
    httpMock.mockResolvedValueOnce({ url: "https://pay/x" });
    const intent = await startCheckout("plus", "month");
    expect(intent).toMatchObject({ url: "https://pay/x", demo: false });
    const [, init] = httpMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ plan: "plus", interval: "month" });
  });
  it("flags demo mode when no processor", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await startCheckout("plus", "month")).toEqual({ demo: true });
  });
});

describe("changePlan / cancel", () => {
  it("changePlan returns the updated sub, optimistic on failure", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(501, "soon"));
    const sub = await changePlan("studio", "year");
    expect(sub).toMatchObject({ planId: "studio", interval: "year" });
  });
  it("cancel marks cancelAtPeriodEnd when unavailable", async () => {
    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect((await cancelSubscription()).cancelAtPeriodEnd).toBe(true);
  });
});

describe("listInvoices", () => {
  it("parses + sorts, [] when unavailable", async () => {
    httpMock.mockResolvedValueOnce([
      { id: "a", at: 1, amount_cents: 1200, status: "paid" },
      { id: "b", at: 9, amount_cents: 1200, status: "paid" },
    ]);
    expect((await listInvoices()).map((i) => i.id)).toEqual(["b", "a"]);

    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await listInvoices()).toEqual([]);
  });
});

describe("getUsage", () => {
  it("maps snake_case usage, zeroes when unavailable", async () => {
    httpMock.mockResolvedValueOnce({ video_seconds: 300, director_edits: 4, concurrent_films: 1 });
    expect(await getUsage()).toEqual({ videoSeconds: 300, directorEdits: 4, concurrentFilms: 1 });

    httpMock.mockRejectedValueOnce(new ApiError(404, "x"));
    expect(await getUsage()).toEqual({ videoSeconds: 0, directorEdits: 0, concurrentFilms: 0 });
  });
});
