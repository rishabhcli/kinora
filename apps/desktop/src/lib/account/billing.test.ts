import { describe, it, expect } from "vitest";
import {
  PLANS,
  FREE_PLAN,
  findPlan,
  freeSubscription,
  parseSubscription,
  planForSubscription,
  effectiveEntitlements,
  hasEntitlement,
  trialDaysRemaining,
  periodDaysRemaining,
  monthlyEquivalentCents,
  annualSavingsPercent,
  prorationCents,
  formatMoney,
  priceLabel,
  parseInvoices,
  type Subscription,
} from "./billing";

const DAY = 86_400_000;

describe("plan catalog", () => {
  it("has free/plus/studio with the free plan first", () => {
    expect(PLANS.map((p) => p.id)).toEqual(["free", "plus", "studio"]);
    expect(FREE_PLAN.id).toBe("free");
    expect(findPlan("plus")?.highlighted).toBe(true);
    expect(findPlan("nope")).toBeUndefined();
  });
  it("studio is unlimited", () => {
    expect(findPlan("studio")?.entitlements.concurrentFilms).toBe(Number.POSITIVE_INFINITY);
  });
});

describe("subscription parsing", () => {
  it("defaults to free for garbage", () => {
    expect(parseSubscription(null).planId).toBe("free");
    expect(parseSubscription({ plan_id: "ghost" }).planId).toBe("free");
  });
  it("maps snake_case and ISO dates", () => {
    const sub = parseSubscription({
      plan_id: "plus",
      interval: "year",
      status: "trialing",
      period_start: "2024-01-01T00:00:00Z",
      period_end: "2024-12-31T00:00:00Z",
      cancel_at_period_end: true,
      trial_end: "2024-01-15T00:00:00Z",
    });
    expect(sub).toMatchObject({ planId: "plus", interval: "year", status: "trialing", cancelAtPeriodEnd: true });
    expect(sub.trialEnd).toBe(Date.parse("2024-01-15T00:00:00Z"));
  });
});

describe("entitlements", () => {
  it("a plus sub grants plus entitlements", () => {
    const sub = parseSubscription({ planId: "plus", status: "active" });
    expect(planForSubscription(sub).id).toBe("plus");
    expect(hasEntitlement(sub, "offlineDownloads")).toBe(true);
  });
  it("a lapsed/canceled sub falls back to free", () => {
    const now = 10 * DAY;
    const sub: Subscription = {
      planId: "plus",
      interval: "month",
      status: "canceled",
      periodStart: 0,
      periodEnd: 5 * DAY, // already past
      cancelAtPeriodEnd: true,
    };
    expect(effectiveEntitlements(sub, now)).toEqual(FREE_PLAN.entitlements);
  });
  it("keeps the plan until period end when set to cancel", () => {
    const now = 3 * DAY;
    const sub: Subscription = {
      planId: "plus", interval: "month", status: "active",
      periodStart: 0, periodEnd: 5 * DAY, cancelAtPeriodEnd: true,
    };
    expect(effectiveEntitlements(sub, now).maxQuality).toBe("high");
  });
});

describe("countdowns", () => {
  it("trial + period days remaining", () => {
    const now = 0;
    const sub: Subscription = {
      planId: "plus", interval: "month", status: "trialing",
      periodStart: 0, periodEnd: 30 * DAY, cancelAtPeriodEnd: false, trialEnd: 7 * DAY,
    };
    expect(trialDaysRemaining(sub, now)).toBe(7);
    expect(periodDaysRemaining(sub, now)).toBe(30);
    expect(trialDaysRemaining({ ...sub, status: "active" }, now)).toBe(0);
  });
});

describe("pricing math", () => {
  it("monthly equivalent amortises yearly", () => {
    const plus = findPlan("plus")!;
    expect(monthlyEquivalentCents(plus, "month")).toBe(1200);
    expect(monthlyEquivalentCents(plus, "year")).toBe(Math.round(12000 / 12));
  });
  it("annual savings percent", () => {
    const plus = findPlan("plus")!; // 1200*12=14400 vs 12000 → 16.67% → 17
    expect(annualSavingsPercent(plus)).toBe(17);
    expect(annualSavingsPercent(FREE_PLAN)).toBe(0);
  });
  it("proration credits unused time and charges the new plan", () => {
    // half the period remains: credit half old, charge half new
    const net = prorationCents(
      { priceCents: 1000, periodStart: 0, periodEnd: 100 },
      { priceCents: 3000 },
      50,
    );
    expect(net).toBe(Math.round(3000 * 0.5) - Math.round(1000 * 0.5)); // 1000
  });
  it("proration is zero past period end", () => {
    expect(prorationCents({ priceCents: 1000, periodStart: 0, periodEnd: 100 }, { priceCents: 3000 }, 200)).toBe(0);
  });
});

describe("money formatting", () => {
  it("drops cents on whole amounts, keeps on fractional", () => {
    expect(formatMoney(1200)).toBe("$12");
    expect(formatMoney(1250)).toMatch(/\$12\.50/);
    expect(formatMoney(0)).toMatch(/\$0/);
  });
  it("priceLabel renders free + per-interval", () => {
    expect(priceLabel(FREE_PLAN, "month")).toBe("Free");
    expect(priceLabel(findPlan("plus")!, "month")).toBe("$12/mo");
    expect(priceLabel(findPlan("plus")!, "year")).toBe("$120/yr");
  });
});

describe("freeSubscription", () => {
  it("is active for 30 days", () => {
    const sub = freeSubscription(0);
    expect(sub.planId).toBe("free");
    expect(sub.periodEnd).toBe(30 * DAY);
  });
});

describe("parseInvoices", () => {
  it("parses, sorts newest-first, drops malformed", () => {
    const inv = parseInvoices([
      { id: "a", at: 100, amount_cents: 1200, status: "paid" },
      { nope: 1 },
      { id: "b", created: 500, amount: 1200, status: "weird" },
    ]);
    expect(inv.map((i) => i.id)).toEqual(["b", "a"]);
    expect(inv[0].status).toBe("paid"); // unknown → paid default
    expect(parseInvoices("x")).toEqual([]);
  });
});
