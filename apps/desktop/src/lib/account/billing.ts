// Subscription & billing (account domain) — the pure plan catalog, entitlement
// resolution, proration math, trial countdown, and money/invoice formatting
// behind the billing UI. This is scaffolding: there is no payment endpoint yet,
// so the catalog + math live client-side and the API adapter (lib/api/billing.ts)
// returns a demo subscription. Everything here is pure + deterministic.

// ---- Plan catalog --------------------------------------------------------- //

export type PlanId = "free" | "plus" | "studio";
export type BillingInterval = "month" | "year";

export interface Plan {
  id: PlanId;
  name: string;
  /** One-line positioning. */
  tagline: string;
  /** Price in cents, per interval. Free is 0. */
  priceCents: Record<BillingInterval, number>;
  /** Feature bullets for the pricing card. */
  features: string[];
  /** Machine-checkable entitlements (see EntitlementKey). */
  entitlements: Entitlements;
  /** Marketing highlight (the "most popular" ribbon). */
  highlighted?: boolean;
}

export interface Entitlements {
  /** Books that can be live-generating at once. Infinity = unlimited. */
  concurrentFilms: number;
  /** Monthly generated video-seconds budget. */
  monthlyVideoSeconds: number;
  /** Max render quality tier the plan unlocks. */
  maxQuality: "standard" | "high" | "cinema";
  /** Director-mode regenerations per month. */
  directorEdits: number;
  /** Whether the canon editor's advanced controls are unlocked. */
  advancedCanon: boolean;
  /** Whether offline downloads are allowed. */
  offlineDownloads: boolean;
}

const UNLIMITED = Number.POSITIVE_INFINITY;

/** The plan catalog. Prices are illustrative scaffolding. */
export const PLANS: Plan[] = [
  {
    id: "free",
    name: "Reader",
    tagline: "Try the magic, on us.",
    priceCents: { month: 0, year: 0 },
    features: [
      "1 live film at a time",
      "Standard quality",
      "Demo library + your first upload",
    ],
    entitlements: {
      concurrentFilms: 1,
      monthlyVideoSeconds: 600,
      maxQuality: "standard",
      directorEdits: 10,
      advancedCanon: false,
      offlineDownloads: false,
    },
  },
  {
    id: "plus",
    name: "Cinephile",
    tagline: "Your whole library, in motion.",
    priceCents: { month: 1200, year: 12000 },
    features: [
      "Up to 5 live films",
      "High quality",
      "Director mode + canon editor",
      "Offline downloads",
    ],
    entitlements: {
      concurrentFilms: 5,
      monthlyVideoSeconds: 7200,
      maxQuality: "high",
      directorEdits: 200,
      advancedCanon: true,
      offlineDownloads: true,
    },
    highlighted: true,
  },
  {
    id: "studio",
    name: "Studio",
    tagline: "Unlimited reels for the obsessed.",
    priceCents: { month: 3500, year: 35000 },
    features: [
      "Unlimited live films",
      "Cinema quality",
      "Unlimited director edits",
      "Priority rendering",
    ],
    entitlements: {
      concurrentFilms: UNLIMITED,
      monthlyVideoSeconds: UNLIMITED,
      maxQuality: "cinema",
      directorEdits: UNLIMITED,
      advancedCanon: true,
      offlineDownloads: true,
    },
  },
];

export function findPlan(id: string): Plan | undefined {
  return PLANS.find((p) => p.id === id);
}

export const FREE_PLAN = PLANS[0];

// ---- Subscription state --------------------------------------------------- //

export type SubscriptionStatus =
  | "active"
  | "trialing"
  | "past_due"
  | "canceled"
  | "incomplete";

export interface Subscription {
  planId: PlanId;
  interval: BillingInterval;
  status: SubscriptionStatus;
  /** Epoch ms the current period started. */
  periodStart: number;
  /** Epoch ms the current period ends (renewal or expiry). */
  periodEnd: number;
  /** True if set to cancel at periodEnd (still active until then). */
  cancelAtPeriodEnd: boolean;
  /** Epoch ms the trial ends, when status is "trialing". */
  trialEnd?: number;
}

/** The default (no subscription) state — everyone starts on Free. */
export function freeSubscription(now: number = Date.now()): Subscription {
  return {
    planId: "free",
    interval: "month",
    status: "active",
    periodStart: now,
    periodEnd: now + 30 * 86_400_000,
    cancelAtPeriodEnd: false,
  };
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v.length ? v : undefined;
}
function asMs(v: unknown, fallback: number): number {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const t = Date.parse(v);
    if (!Number.isNaN(t)) return t;
  }
  return fallback;
}

const STATUSES: SubscriptionStatus[] = ["active", "trialing", "past_due", "canceled", "incomplete"];
const INTERVALS: BillingInterval[] = ["month", "year"];

/** Parse a backend subscription row, defaulting unknowns to a Free sub. */
export function parseSubscription(row: unknown, now: number = Date.now()): Subscription {
  if (typeof row !== "object" || row === null) return freeSubscription(now);
  const r = row as Record<string, unknown>;
  const planId = (findPlan(str(r.planId ?? r.plan_id) ?? "free")?.id ?? "free") as PlanId;
  const interval = INTERVALS.includes(r.interval as BillingInterval)
    ? (r.interval as BillingInterval)
    : "month";
  const status = STATUSES.includes(r.status as SubscriptionStatus)
    ? (r.status as SubscriptionStatus)
    : "active";
  const periodStart = asMs(r.periodStart ?? r.period_start, now);
  return {
    planId,
    interval,
    status,
    periodStart,
    periodEnd: asMs(r.periodEnd ?? r.period_end, periodStart + 30 * 86_400_000),
    cancelAtPeriodEnd: r.cancelAtPeriodEnd === true || r.cancel_at_period_end === true,
    trialEnd: r.trialEnd != null || r.trial_end != null ? asMs(r.trialEnd ?? r.trial_end, now) : undefined,
  };
}

/** The active plan object for a subscription (Free fallback). */
export function planForSubscription(sub: Subscription): Plan {
  return findPlan(sub.planId) ?? FREE_PLAN;
}

/** The effective entitlements right now: a canceled/past_due sub falls back to
 *  Free immediately if its period has lapsed, else keeps its plan until period
 *  end. */
export function effectiveEntitlements(sub: Subscription, now: number = Date.now()): Entitlements {
  const lapsed = now >= sub.periodEnd && (sub.status === "canceled" || sub.cancelAtPeriodEnd);
  if (lapsed || sub.status === "incomplete") return FREE_PLAN.entitlements;
  return planForSubscription(sub).entitlements;
}

/** Whether a feature is unlocked under the current subscription. */
export function hasEntitlement(
  sub: Subscription,
  key: "advancedCanon" | "offlineDownloads",
  now: number = Date.now(),
): boolean {
  return effectiveEntitlements(sub, now)[key];
}

// ---- Trial / period countdown --------------------------------------------- //

const DAY = 86_400_000;

/** Whole days remaining in the trial (0 if not trialing or already ended). */
export function trialDaysRemaining(sub: Subscription, now: number = Date.now()): number {
  if (sub.status !== "trialing" || sub.trialEnd == null) return 0;
  return Math.max(0, Math.ceil((sub.trialEnd - now) / DAY));
}

/** Whole days until the current period ends/renews. */
export function periodDaysRemaining(sub: Subscription, now: number = Date.now()): number {
  return Math.max(0, Math.ceil((sub.periodEnd - now) / DAY));
}

export function isActive(sub: Subscription, now: number = Date.now()): boolean {
  if (sub.status === "active" || sub.status === "trialing") return now < sub.periodEnd || sub.status === "active";
  return false;
}

// ---- Pricing math --------------------------------------------------------- //

/** Monthly-equivalent price (yearly plans amortised) — drives "$10/mo billed
 *  yearly". Returns cents. */
export function monthlyEquivalentCents(plan: Plan, interval: BillingInterval): number {
  const price = plan.priceCents[interval];
  return interval === "year" ? Math.round(price / 12) : price;
}

/** Percent saved switching from monthly to yearly for a plan (0..100, rounded). */
export function annualSavingsPercent(plan: Plan): number {
  const monthlyYear = plan.priceCents.month * 12;
  if (monthlyYear <= 0) return 0;
  const saved = monthlyYear - plan.priceCents.year;
  return Math.max(0, Math.round((saved / monthlyYear) * 100));
}

/** Proration when changing plans/intervals mid-period: credit the unused
 *  portion of the current plan, charge the prorated portion of the new one.
 *  Returns the net charge in cents (can be negative = credit). */
export function prorationCents(
  current: { priceCents: number; periodStart: number; periodEnd: number },
  next: { priceCents: number },
  now: number = Date.now(),
): number {
  const span = Math.max(1, current.periodEnd - current.periodStart);
  const remaining = Math.max(0, Math.min(span, current.periodEnd - now));
  const fraction = remaining / span;
  const unusedCredit = Math.round(current.priceCents * fraction);
  const newCharge = Math.round(next.priceCents * fraction);
  return newCharge - unusedCredit;
}

// ---- Money formatting ----------------------------------------------------- //

/** Format cents as a currency string. Drops the ".00" on whole amounts for the
 *  big price display. Deterministic (no Intl locale variance in tests when
 *  currency is USD and no Intl present → manual fallback). */
export function formatMoney(cents: number, currency = "USD"): string {
  const amount = cents / 100;
  try {
    if (typeof Intl !== "undefined" && Intl.NumberFormat) {
      const whole = Number.isInteger(amount);
      return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency,
        minimumFractionDigits: whole ? 0 : 2,
        maximumFractionDigits: 2,
      }).format(amount);
    }
  } catch {
    /* fall through to manual */
  }
  const sign = amount < 0 ? "-" : "";
  const abs = Math.abs(amount);
  const body = Number.isInteger(abs) ? String(abs) : abs.toFixed(2);
  return `${sign}$${body}`;
}

/** "$12/mo" or "$120/yr" for a plan card. Free renders as "Free". */
export function priceLabel(plan: Plan, interval: BillingInterval): string {
  const cents = plan.priceCents[interval];
  if (cents === 0) return "Free";
  return `${formatMoney(cents)}/${interval === "year" ? "yr" : "mo"}`;
}

// ---- Invoices ------------------------------------------------------------- //

export interface Invoice {
  id: string;
  /** Epoch ms issued. */
  at: number;
  /** Amount in cents. */
  amountCents: number;
  status: "paid" | "open" | "void" | "refunded";
  /** Hosted PDF/receipt URL (optional). */
  url?: string;
  description?: string;
}

export function parseInvoice(row: unknown): Invoice | null {
  if (typeof row !== "object" || row === null) return null;
  const r = row as Record<string, unknown>;
  const id = str(r.id);
  if (!id) return null;
  const status = (["paid", "open", "void", "refunded"] as const).includes(r.status as never)
    ? (r.status as Invoice["status"])
    : "paid";
  return {
    id,
    at: asMs(r.at ?? r.created, Date.now()),
    amountCents: typeof r.amountCents === "number" ? r.amountCents
      : typeof r.amount_cents === "number" ? r.amount_cents
      : typeof r.amount === "number" ? r.amount : 0,
    status,
    url: str(r.url ?? r.hosted_invoice_url),
    description: str(r.description),
  };
}

export function parseInvoices(rows: unknown): Invoice[] {
  if (!Array.isArray(rows)) return [];
  return rows.map(parseInvoice).filter((i): i is Invoice => i !== null)
    .sort((a, b) => b.at - a.at);
}
