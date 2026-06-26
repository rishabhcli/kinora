import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { motion, useMotionValue, animate } from "framer-motion";

type Billing = "monthly" | "yearly";

function AnimatedPrice({ value, billing, isPayAsYouGo }: { value: number; billing: Billing; isPayAsYouGo?: boolean }) {
  const mv = useMotionValue(value);
  const [display, setDisplay] = useState(value);

  useEffect(() => {
    if (value === 0) return;
    const controls = animate(mv, value, {
      duration: 0.5,
      ease: [0.22, 1, 0.36, 1],
      onUpdate: (v) => setDisplay(v),
    });
    return () => controls.stop();
  }, [value, billing, mv]);

  if (value === 0 && isPayAsYouGo) return <span>Custom</span>;
  if (value === 0) return <span>Free</span>;

  return (
    <motion.span key={billing}>
      ${display.toFixed(2)}
    </motion.span>
  );
}

export default function PricingPage() {
  const [billing, setBilling] = useState<Billing>("monthly");
  const monthlyRef = useRef<HTMLButtonElement>(null);
  const yearlyRef = useRef<HTMLButtonElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [pillStyle, setPillStyle] = useState({ left: 4, width: 0 });

  useLayoutEffect(() => {
    const active = billing === "monthly" ? monthlyRef.current : yearlyRef.current;
    const container = containerRef.current;
    if (active && container) {
      setPillStyle({
        left: active.offsetLeft,
        width: active.offsetWidth,
      });
    }
  }, [billing]);

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
        "Unlimited books in library",
        "100 MB cloud storage",
        "6 min HD / month",
        "HD streaming (720p)",
        "Basic notes & highlights",
        "Community access",
      ],
    },
    {
      name: "Kinora Plus",
      tagline: "For avid readers who want more",
      monthly: 19.99,
      yearly: 15.99,
      cta: "Upgrade to Plus",
      current: false,
      recommended: true,
      features: [
        "Unlimited books in library & cloud",
        "50 min HD + 20 min FHD / month",
        "HD & FHD streaming (1080p)",
        "Advanced notes & highlights",
        "Offline downloads",
      ],
    },
    {
      name: "Kinora Pro",
      tagline: "For power users and families",
      monthly: 79.99,
      yearly: 63.99,
      cta: "Go Pro",
      current: false,
      recommended: false,
      features: [
        "Unlimited books in library & cloud",
        "350 min HD + 200 min FHD / month",
        "Pay-as-you-go beyond limits",
        "Priority generation queue",
        "Family sharing (5 members)",
        "Early access to new features",
      ],
    },
    {
      name: "Enterprise",
      tagline: "Custom volume for teams & schools",
      monthly: 0,
      yearly: 0,
      cta: "Contact sales",
      current: false,
      recommended: false,
      features: [
        "Unlimited everything",
        "Dedicated cloud infrastructure",
        "SSO & advanced access controls",
        "Custom generation models",
        "Analytics & usage dashboard",
        "24/7 dedicated support",
      ],
    },
  ];

  return (
    <div className="pt-24 pb-8 px-6 max-w-[1280px] mx-auto relative z-10">
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
          ref={containerRef}
          className="inline-flex items-center rounded-full p-1 relative"
          style={{ background: "rgba(255, 255, 255, 0.04)", border: "1px solid rgba(255, 255, 255, 0.06)" }}
        >
          <motion.div
            className="absolute top-1 bottom-1 rounded-full"
            style={{ background: "rgba(255, 255, 255, 0.1)" }}
            animate={{ left: pillStyle.left, width: pillStyle.width }}
            transition={{ type: "spring", stiffness: 400, damping: 32 }}
          />
          <button
            ref={monthlyRef}
            onClick={() => setBilling("monthly")}
            className="relative z-10 px-5 py-2 rounded-full text-[12px] font-medium transition-colors"
            style={{
              color: billing === "monthly" ? "rgba(232, 226, 216, 0.9)" : "rgba(168, 158, 148, 0.6)",
            }}
          >
            Monthly
          </button>
          <button
            ref={yearlyRef}
            onClick={() => setBilling("yearly")}
            className="relative z-10 px-5 py-2 rounded-full text-[12px] font-medium transition-colors flex items-center gap-2"
            style={{
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
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 max-w-5xl mx-auto">
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
                <AnimatedPrice value={plan[billing]} billing={billing} isPayAsYouGo={plan.name === "Enterprise"} />
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
                : plan.name === "Enterprise"
                ? "Volume-based pricing"
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
