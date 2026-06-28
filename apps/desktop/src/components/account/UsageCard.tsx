// UsageCard — this period's consumption bars (video-seconds, director edits,
// concurrent films) against the active plan's entitlements. Pure math from
// lib/account/usage; reads usage from the graceful billing adapter. Surfaces an
// upgrade nudge when any meter is near its cap.
import { useEffect, useState } from "react";
import {
  type Subscription,
  type Meter,
  type UsageSnapshot,
  planForSubscription,
  usageMeters,
  formatSecondsMeter,
  formatCountMeter,
  shouldNudgeUpgrade,
} from "../../lib/account";
import { getUsage } from "../../lib/api/billing";

export default function UsageCard({ sub }: { sub: Subscription }) {
  const [usage, setUsage] = useState<UsageSnapshot>({ videoSeconds: 0, directorEdits: 0, concurrentFilms: 0 });

  useEffect(() => {
    let alive = true;
    void (async () => {
      const u = await getUsage();
      if (alive) setUsage(u);
    })();
    return () => {
      alive = false;
    };
  }, []);

  const ent = planForSubscription(sub).entitlements;
  const meters = usageMeters(usage, ent);
  const nudge = shouldNudgeUpgrade(meters);

  return (
    <div className="acct-card">
      <h3 className="acct-card-title">This period</h3>
      <Bar label="Video minutes" meter={meters.videoSeconds} text={formatSecondsMeter(meters.videoSeconds)} />
      <Bar label="Director edits" meter={meters.directorEdits} text={formatCountMeter(meters.directorEdits)} />
      <Bar label="Live films" meter={meters.concurrentFilms} text={formatCountMeter(meters.concurrentFilms)} />
      {nudge && (
        <p className="acct-card-desc" style={{ color: "var(--auth-gold-bright)", marginTop: 8 }}>
          You're close to a limit — a higher plan unlocks more.
        </p>
      )}
    </div>
  );
}

function Bar({ label, meter, text }: { label: string; meter: Meter; text: string }) {
  const pct = meter.unlimited ? 8 : Math.round(meter.fraction * 100);
  const color = meter.exhausted
    ? "var(--auth-danger)"
    : meter.nearLimit
      ? "var(--auth-gold)"
      : "var(--auth-good)";
  return (
    <div className="acct-row" style={{ display: "block" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <span className="acct-row-title">{label}</span>
        <span className="acct-row-meta">{text}</span>
      </div>
      <div
        className="onb-progress"
        style={{ marginBottom: 0, opacity: meter.unlimited ? 0.5 : 1 }}
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div className="onb-progress-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}
