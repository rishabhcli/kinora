import { useState } from "react";

type Billing = "monthly" | "yearly";

export default function PricingPage() {
  const [billing, setBilling] = useState<Billing>("monthly");

  const plans = [
    {
      name: "Reader",
      tagline: "For casual readers getting started",
      monthly: 0,
      yearly: 0,
      cta: "Current Plan",
      current: true,
      recommended: false,
      features: [
        "Up to 10 books in library",
        "Basic reading tools",
        "Notes & highlights (limited)",
        "Community access",
        "1 device",
      ],
    },
    {
      name: "Kinora Plus",
      tagline: "For avid readers who want more",
      monthly: 9.99,
      yearly: 7.99,
      cta: "Upgrade to Plus",
      current: false,
      recommended: true,
      features: [
        "Unlimited books in library",
        "AI cinematic mode",
        "Advanced notes & highlights",
        "Priority email support",
        "3 devices",
      ],
    },
    {
      name: "Kinora Pro",
      tagline: "For power users and families",
      monthly: 19.99,
      yearly: 15.99,
      cta: "Go Pro",
      current: false,
      recommended: false,
      features: [
        "FHD cinematic streaming",
        "Family sharing (5 members)",
        "Early access to new features",
        "24/7 priority support",
        "Unlimited devices",
      ],
    },
  ];

  const formatPrice = (price: number) =>
    price === 0 ? "Free" : `$${price.toFixed(2)}`;

  return (
    <div className="pt-12 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
      {/* Header */}
      <div className="text-center mb-10 pt-4">
        <p className="text-[11px] font-medium text-kinora-muted mb-3 tracking-wide uppercase">
          Pricing
        </p>
        <h1 className="font-serif text-3xl font-semibold text-kinora-text mb-3">
          Choose your plan
        </h1>
        <p className="text-[13px] text-kinora-muted max-w-md mx-auto">
          Start free, upgrade when you're ready. Cancel anytime.
        </p>
      </div>

      {/* Billing toggle */}
      <div className="flex justify-center mb-10">
        <div
          className="inline-flex items-center rounded-full p-1"
          style={{ background: "rgba(255, 255, 255, 0.04)", border: "1px solid rgba(255, 255, 255, 0.06)" }}
        >
          <button
            onClick={() => setBilling("monthly")}
            className="px-5 py-2 rounded-full text-[12px] font-medium transition-colors"
            style={{
              background: billing === "monthly" ? "rgba(255, 255, 255, 0.1)" : "transparent",
              color: billing === "monthly" ? "rgba(232, 226, 216, 0.9)" : "rgba(168, 158, 148, 0.6)",
            }}
          >
            Monthly
          </button>
          <button
            onClick={() => setBilling("yearly")}
            className="px-5 py-2 rounded-full text-[12px] font-medium transition-colors flex items-center gap-2"
            style={{
              background: billing === "yearly" ? "rgba(255, 255, 255, 0.1)" : "transparent",
              color: billing === "yearly" ? "rgba(232, 226, 216, 0.9)" : "rgba(168, 158, 148, 0.6)",
            }}
          >
            Yearly
            <span
              className="text-[9px] font-semibold px-1.5 py-0.5 rounded"
              style={{ background: "rgba(255, 255, 255, 0.08)", color: "rgba(232, 226, 216, 0.7)" }}
            >
              -20%
            </span>
          </button>
        </div>
      </div>

      {/* Plan cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 max-w-4xl mx-auto">
        {plans.map((plan) => (
          <div
            key={plan.name}
            className="rounded-2xl p-6 flex flex-col relative"
            style={{
              background: plan.recommended
                ? "rgba(255, 255, 255, 0.04)"
                : "rgba(255, 255, 255, 0.02)",
              border: plan.recommended
                ? "1px solid rgba(255, 255, 255, 0.12)"
                : "1px solid rgba(255, 255, 255, 0.05)",
            }}
          >
            {plan.recommended && (
              <div
                className="absolute -top-px left-1/2 -translate-x-1/2 px-3 py-1 rounded-b-lg text-[9px] font-semibold tracking-wider uppercase"
                style={{
                  background: "rgba(232, 226, 216, 0.1)",
                  color: "rgba(232, 226, 216, 0.8)",
                }}
              >
                Recommended
              </div>
            )}

            {/* Plan name */}
            <h3 className="font-serif text-xl font-semibold text-kinora-text mb-1">
              {plan.name}
            </h3>
            <p className="text-[11px] text-kinora-muted mb-6 min-h-[16px]">
              {plan.tagline}
            </p>

            {/* Price */}
            <div className="flex items-baseline gap-1.5 mb-1">
              <span className="text-4xl font-bold text-kinora-text tracking-tight">
                {formatPrice(plan[billing])}
              </span>
              {plan[billing] !== 0 && (
                <span className="text-[13px] text-kinora-muted font-medium">/mo</span>
              )}
            </div>
            <p className="text-[10px] text-kinora-muted/50 mb-6 min-h-[14px]">
              {plan[billing] !== 0 && billing === "yearly"
                ? "Billed annually"
                : plan[billing] !== 0
                ? "Billed monthly"
                : "Free forever"}
            </p>

            {/* CTA */}
            <button
              disabled={plan.current}
              className="w-full py-2.5 rounded-xl text-[13px] font-medium transition-colors mb-6"
              style={{
                background: plan.current
                  ? "rgba(255, 255, 255, 0.03)"
                  : plan.recommended
                  ? "rgba(232, 226, 216, 0.12)"
                  : "rgba(255, 255, 255, 0.06)",
                color: plan.current
                  ? "rgba(168, 158, 148, 0.4)"
                  : "rgba(232, 226, 216, 0.9)",
                border: plan.recommended && !plan.current
                  ? "1px solid rgba(232, 226, 216, 0.15)"
                  : "1px solid transparent",
                cursor: plan.current ? "default" : "pointer",
              }}
            >
              {plan.cta}
            </button>

            {/* Divider */}
            <div className="h-px mb-4" style={{ background: "rgba(255, 255, 255, 0.05)" }} />

            {/* Features */}
            <ul className="space-y-2.5 flex-1">
              {plan.features.map((f) => (
                <li
                  key={f}
                  className="flex items-start gap-2.5 text-[12px] text-kinora-muted"
                >
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="shrink-0 mt-0.5 opacity-40"
                  >
                    <path d="M5 12l5 5L20 7" />
                  </svg>
                  <span>{f}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {/* Bottom note */}
      <p className="text-center text-[11px] text-kinora-muted/50 mt-8">
        All plans include access to the Kinora library. No hidden fees.
      </p>
    </div>
  );
}
