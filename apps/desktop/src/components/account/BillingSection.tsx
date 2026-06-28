// BillingSection — subscription status + plan picker (scaffolding; no live
// processor). Uses the pure billing catalog/math (lib/account/billing) and the
// graceful adapter (lib/api/billing). Plan changes route through startCheckout
// (which flags demo mode when there's no processor) so the UI is complete.
import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { Section, Segmented } from "./primitives";
import {
  PLANS,
  type Plan,
  type Subscription,
  type BillingInterval,
  type PlanId,
  freeSubscription,
  planForSubscription,
  priceLabel,
  annualSavingsPercent,
  trialDaysRemaining,
  periodDaysRemaining,
} from "../../lib/account";
import { getSubscription, startCheckout, changePlan, cancelSubscription } from "../../lib/api/billing";
import UsageCard from "./UsageCard";

export default function BillingSection() {
  const [sub, setSub] = useState<Subscription>(() => freeSubscription());
  const [interval, setInterval] = useState<BillingInterval>("month");
  const [notice, setNotice] = useState<string | null>(null);
  const [busyPlan, setBusyPlan] = useState<PlanId | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const s = await getSubscription();
      if (alive) {
        setSub(s);
        setInterval(s.interval);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const currentPlan = planForSubscription(sub);

  async function pick(plan: Plan) {
    if (plan.id === currentPlan.id) return;
    setBusyPlan(plan.id);
    setNotice(null);
    if (plan.id === "free") {
      const next = await cancelSubscription();
      setSub(next);
      setNotice("Your plan will switch to Reader at the end of the period.");
    } else {
      const intent = await startCheckout(plan.id, interval);
      if (intent.url) {
        window.open?.(intent.url, "_blank");
      } else if (intent.demo) {
        // No processor wired — reflect the chosen plan locally.
        const next = await changePlan(plan.id, interval);
        setSub(next);
        setNotice("Billing isn't connected in this build — plan updated locally for preview.");
      }
    }
    setBusyPlan(null);
  }

  const statusLine = (() => {
    if (sub.status === "trialing") return `Trial — ${trialDaysRemaining(sub)} days left`;
    if (sub.cancelAtPeriodEnd) return `Cancels in ${periodDaysRemaining(sub)} days`;
    if (sub.planId === "free") return "You're on the free Reader plan";
    return `Renews in ${periodDaysRemaining(sub)} days`;
  })();

  return (
    <Section title="Plan & billing" sub="Choose how much of Kinora you unlock.">
      <div className="acct-card" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <div>
          <div className="acct-row-title">
            Current plan: <strong>{currentPlan.name}</strong>
          </div>
          <div className="acct-row-meta">{statusLine}</div>
        </div>
        <Segmented<BillingInterval>
          ariaLabel="Billing interval"
          value={interval}
          onChange={setInterval}
          options={[
            { value: "month", label: "Monthly" },
            { value: "year", label: "Yearly" },
          ]}
        />
      </div>

      {notice && (
        <p className="auth-formmsg auth-formmsg--info" role="status">
          {notice}
        </p>
      )}

      <UsageCard sub={sub} />

      <div className="acct-plans">
        {PLANS.map((plan) => {
          const isCurrent = plan.id === currentPlan.id;
          const savings = interval === "year" ? annualSavingsPercent(plan) : 0;
          return (
            <div
              key={plan.id}
              className={`acct-plan${isCurrent ? " is-current" : ""}${plan.highlighted ? " is-highlighted" : ""}`}
            >
              {plan.highlighted && <span className="acct-plan-ribbon">Popular</span>}
              <div>
                <div className="acct-plan-name">{plan.name}</div>
                <div className="acct-plan-tagline">{plan.tagline}</div>
              </div>
              <div className="acct-plan-price">
                {priceLabel(plan, interval)}
                {savings > 0 && plan.priceCents.year > 0 && (
                  <small> · save {savings}%</small>
                )}
              </div>
              <ul className="acct-plan-features">
                {plan.features.map((f) => (
                  <li key={f}>
                    <Check size={14} strokeWidth={2} /> {f}
                  </li>
                ))}
              </ul>
              <button
                type="button"
                className={`acct-btn ${isCurrent ? "" : "acct-btn--primary"}`}
                disabled={isCurrent || busyPlan === plan.id}
                onClick={() => pick(plan)}
              >
                {isCurrent ? "Current plan" : busyPlan === plan.id ? "…" : plan.id === "free" ? "Downgrade" : "Choose"}
              </button>
            </div>
          );
        })}
      </div>
    </Section>
  );
}
