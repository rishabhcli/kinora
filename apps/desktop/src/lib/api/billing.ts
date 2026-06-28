// Billing API client (Account domain) — subscription state, plan changes, the
// checkout-intent handoff, and invoices. Built ONLY on the shared `http`
// primitive from `lib/api.ts`; never edits it.
//
// There is no payment processor wired yet (KINORA_LIVE_VIDEO stays off; this is
// UI scaffolding). Every call degrades to a local Free subscription / demo
// checkout so the billing UI renders end-to-end. The pure catalog + math lives
// in lib/account/billing.ts.
import { http, ApiError } from "../api";
import {
  type Subscription,
  type Invoice,
  type PlanId,
  type BillingInterval,
  parseSubscription,
  parseInvoices,
  freeSubscription,
} from "../account";

async function softCall<T>(fn: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 404 || e.status === 405 || e.status === 501 || e.status === 408) return fallback;
      throw e;
    }
    return fallback;
  }
}

// ---- Subscription --------------------------------------------------------- //

/** The user's current subscription. Falls back to a Free sub when no billing
 *  endpoint exists yet. */
export async function getSubscription(): Promise<Subscription> {
  return softCall<Subscription>(
    async () => parseSubscription(await http("/api/billing/subscription")),
    freeSubscription(),
  );
}

export interface CheckoutIntent {
  /** A hosted-checkout URL the shell opens, when the processor is live. */
  url?: string;
  /** A client secret for an embedded flow, when used. */
  clientSecret?: string;
  /** True when we synthesised a demo result (no processor). */
  demo: boolean;
}

/** Start a checkout for a plan + interval. Returns a checkout intent; in demo
 *  mode it flags `demo: true` so the UI shows a "billing coming soon" note. */
export async function startCheckout(
  planId: PlanId,
  interval: BillingInterval,
): Promise<CheckoutIntent> {
  return softCall<CheckoutIntent>(
    async () => {
      const res = await http<{ url?: string; client_secret?: string }>("/api/billing/checkout", {
        method: "POST",
        body: JSON.stringify({ plan: planId, interval }),
      });
      return { url: res?.url, clientSecret: res?.client_secret, demo: false };
    },
    { demo: true },
  );
}

/** Switch plan/interval on an existing subscription (proration is backend-side).
 *  Returns the updated subscription. */
export async function changePlan(
  planId: PlanId,
  interval: BillingInterval,
): Promise<Subscription> {
  return softCall<Subscription>(
    async () =>
      parseSubscription(
        await http("/api/billing/subscription", {
          method: "PATCH",
          body: JSON.stringify({ plan: planId, interval }),
        }),
      ),
    { ...freeSubscription(), planId, interval },
  );
}

/** Cancel at period end (keeps access until the period ends). */
export async function cancelSubscription(): Promise<Subscription> {
  const fallback = { ...freeSubscription(), cancelAtPeriodEnd: true };
  return softCall<Subscription>(
    async () =>
      parseSubscription(await http("/api/billing/subscription/cancel", { method: "POST" })),
    fallback,
  );
}

/** Undo a pending cancellation. */
export async function resumeSubscription(): Promise<Subscription> {
  return softCall<Subscription>(
    async () =>
      parseSubscription(await http("/api/billing/subscription/resume", { method: "POST" })),
    freeSubscription(),
  );
}

/** Open the billing portal (manage payment methods); returns a URL when live. */
export async function openBillingPortal(): Promise<{ url?: string }> {
  return softCall<{ url?: string }>(
    async () => {
      const res = await http<{ url?: string }>("/api/billing/portal", { method: "POST" });
      return { url: res?.url };
    },
    {},
  );
}

// ---- Usage ---------------------------------------------------------------- //

import type { UsageSnapshot } from "../account";

/** Current-period usage (video-seconds, director edits, concurrent films).
 *  Zeroed when no usage endpoint exists yet. */
export async function getUsage(): Promise<UsageSnapshot> {
  const zero: UsageSnapshot = { videoSeconds: 0, directorEdits: 0, concurrentFilms: 0 };
  return softCall<UsageSnapshot>(
    async () => {
      const r = (await http<Record<string, unknown>>("/api/billing/usage")) ?? {};
      const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
      return {
        videoSeconds: num(r.video_seconds ?? r.videoSeconds),
        directorEdits: num(r.director_edits ?? r.directorEdits),
        concurrentFilms: num(r.concurrent_films ?? r.concurrentFilms),
      };
    },
    zero,
  );
}

// ---- Invoices ------------------------------------------------------------- //

/** Invoice history (newest-first). Empty until billing is live. */
export async function listInvoices(): Promise<Invoice[]> {
  return softCall<Invoice[]>(async () => parseInvoices(await http("/api/billing/invoices")), []);
}
