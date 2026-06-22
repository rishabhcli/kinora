import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { ApiError, evalApi } from "../api/client";
import type { BufferTracePoint, EvalReport, MetricPair } from "../api/types";
import { HIGH_WATERMARK_S, LOW_WATERMARK_S } from "../lib/buffer";
import { CloseIcon, Spinner } from "../components/common/icons";

interface MetricsPanelProps {
  open: boolean;
  onClose: () => void;
  bookId: string;
  sessionId: string | null;
}

const TOOLTIP_STYLE = {
  background: "#14141f",
  border: "1px solid #272739",
  borderRadius: 12,
  color: "#e8e9f3",
  fontSize: 12,
};

function ComparisonBar({
  title,
  unit,
  pair,
  domainMax,
  betterIsHigher = true,
}: {
  title: string;
  unit: string;
  pair: MetricPair;
  domainMax: number;
  betterIsHigher?: boolean;
}) {
  const data = [
    { name: "Crew", value: pair.crew, fill: "#7c5cff" },
    { name: "Baseline", value: pair.baseline, fill: "#3f3f5a" },
  ];
  const delta = pair.crew - pair.baseline;
  const wins = betterIsHigher ? delta > 0 : delta < 0;
  return (
    <div className="glass rounded-2xl p-4">
      <div className="mb-1 flex items-baseline justify-between">
        <h4 className="text-sm font-semibold text-kinora-mist">{title}</h4>
        <span className={`text-xs font-medium ${wins ? "text-kinora-ok" : "text-kinora-muted"}`}>
          {wins ? "crew wins" : "—"} ({delta >= 0 ? "+" : ""}
          {delta.toFixed(2)}
          {unit})
        </span>
      </div>
      <div className="h-28 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16, top: 4, bottom: 4 }}>
            <XAxis type="number" domain={[0, domainMax]} hide />
            <YAxis
              type="category"
              dataKey="name"
              tick={{ fill: "#9aa0b5", fontSize: 12 }}
              width={64}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              contentStyle={TOOLTIP_STYLE}
              cursor={{ fill: "rgba(255,255,255,0.04)" }}
              formatter={(value: unknown) => [`${Number(value).toFixed(2)}${unit}`, title]}
            />
            <Bar dataKey="value" radius={[4, 4, 4, 4]} barSize={18}>
              {data.map((d) => (
                <Cell key={d.name} fill={d.fill} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function MetricsPanel({ open, onClose, bookId, sessionId }: MetricsPanelProps) {
  const [trace, setTrace] = useState<BufferTracePoint[] | null>(null);
  const [report, setReport] = useState<EvalReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    const ac = new AbortController();
    setLoading(true);
    setError(null);
    const tasks: Promise<unknown>[] = [
      evalApi
        .report(bookId, ac.signal)
        .then(setReport)
        .catch((e) => {
          if (!(e instanceof DOMException)) throw e;
        }),
    ];
    if (sessionId) {
      tasks.push(
        evalApi
          .bufferTrace(sessionId, ac.signal)
          .then(setTrace)
          .catch((e) => {
            if (!(e instanceof DOMException)) throw e;
          }),
      );
    }
    Promise.all(tasks)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load metrics."))
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, [open, bookId, sessionId]);

  if (!open) return null;

  const low = trace?.[0]?.low ?? LOW_WATERMARK_S;
  const high = trace?.[0]?.high ?? HIGH_WATERMARK_S;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        type="button"
        aria-label="Close metrics"
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
      />
      <aside className="glass-strong relative z-10 flex h-full w-full max-w-md flex-col overflow-y-auto p-5 animate-slide-in">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-kinora-mist">Metrics</h3>
            <p className="text-xs text-kinora-muted">Crew vs. single-agent baseline · the proof.</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-full p-2 text-kinora-muted hover:bg-white/5 hover:text-kinora-mist"
          >
            <CloseIcon className="h-4 w-4" />
          </button>
        </div>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-kinora-muted">
            <Spinner className="h-4 w-4" /> Loading metrics…
          </div>
        ) : null}
        {error ? (
          <p className="rounded-xl border border-kinora-danger/40 bg-kinora-danger/10 px-3 py-2 text-sm text-kinora-danger">
            {error}
          </p>
        ) : null}

        <section className="glass mb-4 rounded-2xl p-4">
          <h4 className="text-sm font-semibold text-kinora-mist">Buffer occupancy</h4>
          <p className="mb-2 text-xs text-kinora-muted">
            Committed video-seconds ahead of the reader — bursts to H, idles, drains to L.
          </p>
          <div className="h-44 w-full">
            {trace && trace.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trace} margin={{ left: -16, right: 8, top: 6, bottom: 0 }}>
                  <CartesianGrid stroke="#272739" strokeDasharray="3 3" />
                  <XAxis
                    dataKey="t"
                    tick={{ fill: "#9aa0b5", fontSize: 11 }}
                    tickFormatter={(v: number) => `${Math.round(v)}s`}
                  />
                  <YAxis tick={{ fill: "#9aa0b5", fontSize: 11 }} />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    formatter={(value: unknown) => [`${Number(value).toFixed(1)}s`, "ahead"]}
                    labelFormatter={(label: unknown) => `t=${Math.round(Number(label))}s`}
                  />
                  <ReferenceLine y={high} stroke="#34d399" strokeDasharray="4 4" label={{ value: "H", fill: "#34d399", fontSize: 11 }} />
                  <ReferenceLine y={low} stroke="#fbbf24" strokeDasharray="4 4" label={{ value: "L", fill: "#fbbf24", fontSize: 11 }} />
                  <Line
                    type="monotone"
                    dataKey="committed_seconds_ahead"
                    stroke="#a78bfa"
                    strokeWidth={2}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-kinora-muted">
                {sessionId ? "No buffer trace yet — start reading to populate it." : "No session."}
              </div>
            )}
          </div>
        </section>

        {report ? (
          <div className="space-y-3">
            <ComparisonBar title="Character consistency (CCS)" unit="" pair={report.ccs} domainMax={1} />
            <ComparisonBar
              title="Accepted-footage efficiency"
              unit="%"
              pair={report.efficiency}
              domainMax={100}
            />
            <div className="glass grid grid-cols-2 gap-3 rounded-2xl p-4 text-sm">
              <div>
                <p className="text-xs text-kinora-muted">Regen rate (lower better)</p>
                <p className="mt-1 text-kinora-mist">
                  <span className="text-kinora-ok">{report.regen_rate.crew.toFixed(2)}</span>
                  <span className="text-kinora-muted"> vs {report.regen_rate.baseline.toFixed(2)}</span>
                </p>
              </div>
              <div>
                <p className="text-xs text-kinora-muted">Style drift (lower better)</p>
                <p className="mt-1 text-kinora-mist">
                  <span className="text-kinora-ok">{report.style_drift.crew.toFixed(2)}</span>
                  <span className="text-kinora-muted"> vs {report.style_drift.baseline.toFixed(2)}</span>
                </p>
              </div>
            </div>
          </div>
        ) : null}
      </aside>
    </div>
  );
}
