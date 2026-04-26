import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Link, useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import type { BreakdownEntry } from "@/api/types";
import { Badge, severityVariant } from "@/components/Badge";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { MarketCell } from "@/components/MarketCell";
import { categoryDisplay, priorDisplay, priorShort } from "@/lib/labels";
import { EmptyState, Skeleton, StatusDot } from "@/components/StatusBits";
import { fmtAgo, fmtInt } from "@/lib/utils";

/** Order priors high → low so chart bars line up with intuition. */
const PRIOR_ORDER: Record<string, number> = {
  high: 0,
  medium_high: 1,
  medium: 2,
  low: 3,
  very_low: 4,
  unclassified: 5,
};

const PRIOR_COLOR: Record<string, string> = {
  high: "hsl(var(--severity-high))",
  medium_high: "hsl(var(--severity-medium))",
  medium: "hsl(var(--primary))",
  low: "hsl(var(--severity-low))",
  very_low: "hsl(var(--muted-foreground))",
  unclassified: "hsl(var(--border))",
};

function buildMarketsQuery(kind: "category" | "prior", key: string): string {
  const p = new URLSearchParams();
  p.set(kind, key);
  p.set("sort", "surveillance_urgency");
  return `/markets?${p.toString()}`;
}

/** Charts hide the “unclassified / not set” bucket so the bars stay readable. */
function filterUnclassified<T extends { key: string }>(rows: T[] | undefined): T[] {
  return (rows ?? []).filter((r) => r.key !== "unclassified");
}

function StatTile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "primary" | "danger" | "default";
}) {
  const valueClass =
    tone === "danger"
      ? "text-[hsl(var(--severity-high))]"
      : tone === "primary"
        ? "text-primary"
        : "text-foreground";
  return (
    <Card>
      <CardBody className="py-4">
        <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
          {label}
        </div>
        <div className={`mt-1 text-2xl font-semibold num ${valueClass}`}>
          {value}
        </div>
        {sub ? (
          <div className="mt-1 text-xs text-muted-foreground leading-snug">{sub}</div>
        ) : null}
      </CardBody>
    </Card>
  );
}

export default function OverviewPage() {
  const navigate = useNavigate();
  const overview = useQuery({
    queryKey: ["overview"],
    queryFn: () => api.overview({ top: 15, anomalies: 15 }),
    refetchInterval: 8_000,
  });
  const st = overview.data?.stats;
  const br = overview.data?.breakdown;
  const topM = overview.data?.top_markets;
  const recentFlags = overview.data?.recent_anomalies;

  const updated = overview.dataUpdatedAt
    ? `updated ${fmtAgo(new Date(overview.dataUpdatedAt).toISOString())}`
    : "connecting…";
  const tone = overview.isError ? "error" : overview.isFetching ? "stale" : "live";

  return (
    <div className="space-y-6">
      <section className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
          <p className="text-sm text-muted-foreground">
            Snapshot of tracked markets and activity · {fmtInt(st?.markets)} in scope
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <StatusDot tone={tone} />
          <span>{updated}</span>
        </div>
      </section>

      <section className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-3 gap-3">
        {overview.isPending ? (
          Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-[88px]" />
          ))
        ) : st ? (
          <>
            <StatTile
              label="Markets"
              value={fmtInt(st.markets)}
              sub={`${fmtInt(st.markets_status_unknown)} still loading titles from the exchange`}
            />
            <StatTile
              label="Triage (top prior)"
              value={fmtInt(st.markets_high_prior)}
              sub="“High” + “elevated” classifier buckets only — equals the sum of those two bars below, not one bar and not by itself “suspicious trades”"
              tone="primary"
            />
            <StatTile
              label="Markets with risk flags"
              value={fmtInt(st.markets_with_flags)}
              sub="At least one stored rule score on the market (see list default sort)"
            />
            <StatTile
              label="Trades stored"
              value={fmtInt(st.trades)}
              sub="Executions we’ve recorded from the live feed"
            />
            <StatTile
              label="Order book events"
              value={fmtInt(st.book_events)}
              sub="Full book snapshots and price-level updates (how the order book changes)"
            />
            <StatTile
              label="Unusual activity rows"
              value={fmtInt(st.anomalies)}
              sub={`${fmtInt(st.anomalies_high_severity)} marked “high” — total stored scoring rows in the DB`}
              tone={st.anomalies_high_severity > 0 ? "danger" : "default"}
            />
          </>
        ) : null}
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader
            title="By surveillance priority"
            subtitle="Classifier triage only. The “high” bar is not the same as the *Triage (top prior)* stat — that stat is **high + elevated** combined. Markets with no prior label are hidden from the bars but still in the total market count."
          />
          <CardBody>
            <BreakdownBarChart
              data={
                filterUnclassified(br?.by_prior)
                  .sort(
                    (a, b) =>
                      (PRIOR_ORDER[a.key] ?? 99) - (PRIOR_ORDER[b.key] ?? 99),
                  )
                  .map((b) => ({
                    ...b,
                    label: priorShort(b.key),
                    displayKey: priorDisplay(b.key),
                    color: PRIOR_COLOR[b.key] ?? "hsl(var(--primary))",
                  }))
              }
              loading={overview.isPending}
              yTick={(v: string) => priorShort(v)}
              onBarClick={(row) =>
                navigate(
                  buildMarketsQuery("prior", row.key),
                )
              }
            />
            <BreakdownLinkList
              kind="prior"
              rows={filterUnclassified(br?.by_prior)
                .sort(
                  (a, b) =>
                    (PRIOR_ORDER[a.key] ?? 99) - (PRIOR_ORDER[b.key] ?? 99),
                )
                .map((b) => ({ ...b, displayKey: priorDisplay(b.key) }))}
            />
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="By topic (top 10)"
            subtitle="Auto-labeled from exchange data. “Not categorized” bucket excluded from this view."
          />
          <CardBody>
            <BreakdownBarChart
              data={
                filterUnclassified(br?.by_category)
                  .slice(0, 10)
                  .map((b, i) => ({
                    ...b,
                    label: categoryDisplay(b.key),
                    displayKey: categoryDisplay(b.key),
                    color: `hsl(var(--chart-${(i % 5) + 1}))`,
                  }))
              }
              loading={overview.isPending}
              yTick={(v: string) => categoryDisplay(v)}
              onBarClick={(row) =>
                navigate(
                  buildMarketsQuery("category", row.key),
                )
              }
            />
            <BreakdownLinkList
              kind="category"
              rows={filterUnclassified(br?.by_category)
                .slice(0, 10)
                .map((b) => ({
                  ...b,
                  displayKey: categoryDisplay(b.key),
                }))}
            />
          </CardBody>
        </Card>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader
            title="Most traded markets"
            subtitle="Where we’ve seen the most execution prints recently"
          />
          <div>
            {overview.isPending ? (
              <div className="p-4">
                <Skeleton className="h-32" />
              </div>
            ) : !topM?.markets.length ? (
              <EmptyState>No trades ingested yet.</EmptyState>
            ) : (
              <table className="w-full">
                <thead>
                  <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border">
                    <th className="text-left px-4 py-2 font-medium">Market</th>
                    <th className="text-right px-4 py-2 font-medium">Trades</th>
                  </tr>
                </thead>
                <tbody>
                  {topM.markets.map((m) => (
                    <tr
                      key={m.market_id}
                      className="border-b border-border last:border-0 hover:bg-secondary/30 transition-colors"
                    >
                      <td className="px-4 py-2.5">
                        <MarketCell market={m} showPrior />
                      </td>
                      <td className="px-4 py-2.5 text-right num text-sm">
                        {fmtInt(m.trade_count)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Recent activity flags"
            subtitle="Scored from spread, price moves, and volume vs. recent history"
            right={
              recentFlags
                ? `${fmtInt(recentFlags.count)} shown`
                : undefined
            }
          />
          <div>
            {overview.isPending ? (
              <div className="p-4">
                <Skeleton className="h-32" />
              </div>
            ) : !recentFlags?.anomalies.length ? (
              <EmptyState>No flags stored yet.</EmptyState>
            ) : (
              <ul className="divide-y divide-border">
                {recentFlags.anomalies.map((a) => (
                  <li
                    key={a.id}
                    className="px-4 py-3 hover:bg-secondary/30 transition-colors"
                  >
                    <div className="flex items-start gap-3">
                      <div className="flex flex-col items-center min-w-[48px]">
                        <Badge variant={severityVariant(a.severity)}>
                          {a.severity}
                        </Badge>
                        <div className="mt-1 num text-sm font-semibold">
                          {a.score.toFixed(1)}
                        </div>
                      </div>
                      <div className="flex-1 min-w-0">
                        <MarketCell
                          market={{
                            market_id: a.market_id,
                            title: a.title || a.market_id,
                            subtitle: a.subtitle,
                            category: a.category ?? null,
                            manipulability_prior: a.manipulability_prior ?? null,
                          }}
                          showPrior
                        />
                        <div className="mt-2 text-xs text-muted-foreground">
                          {fmtAgo(a.created_at)}
                        </div>
                        {a.reasons?.length ? (
                          <div className="mt-1.5 flex flex-wrap gap-1">
                            {a.reasons.slice(0, 4).map((r) => (
                              <code
                                key={r}
                                className="text-[10px] rounded bg-secondary px-1.5 py-0.5 font-mono text-muted-foreground"
                              >
                                {r}
                              </code>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </section>
    </div>
  );
}

function BreakdownLinkList({
  kind,
  rows,
}: {
  kind: "category" | "prior";
  rows:
    | Array<BreakdownEntry & { displayKey?: string }>
    | undefined;
}) {
  if (!rows?.length) return null;
  return (
    <div className="mt-3 border-t border-border pt-3">
      <p className="text-[11px] text-muted-foreground mb-2">
        Open a filtered list (same data as the bars) — all labels shown in full
      </p>
      <ul className="max-h-44 overflow-y-auto space-y-1.5 pr-1">
        {rows.map((r) => (
          <li key={r.key}>
            <Link
              to={buildMarketsQuery(kind, r.key)}
              className="flex items-center justify-between gap-3 rounded-md px-2 py-1 text-sm hover:bg-secondary/50 transition-colors"
            >
              <span
                className="min-w-0 truncate"
                title={r.displayKey ?? r.key}
              >
                {r.displayKey ?? r.key}
              </span>
              <span className="num text-muted-foreground flex-shrink-0">
                {r.count.toLocaleString()}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

function BreakdownBarChart({
  data,
  loading,
  yTick,
  onBarClick,
}: {
  data: Array<
    BreakdownEntry & { color?: string; label?: string; displayKey?: string }
  >;
  loading?: boolean;
  yTick: (key: string) => string;
  onBarClick: (row: BreakdownEntry) => void;
}) {
  if (loading) return <Skeleton className="h-[200px]" />;
  if (!data.length) return <EmptyState>No data.</EmptyState>;
  return (
    <div className="h-[240px] w-full">
      <ResponsiveContainer>
        <BarChart
          data={data}
          layout="vertical"
          margin={{ left: 4, right: 56, top: 4, bottom: 4 }}
        >
          <XAxis
            type="number"
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
            axisLine={false}
            tickLine={false}
          />
          <YAxis
            type="category"
            dataKey="key"
            tickFormatter={yTick as (v: string) => string}
            width={200}
            interval={0}
            tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 10 }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            cursor={{ fill: "hsl(var(--secondary))" }}
            contentStyle={{
              background: "hsl(var(--card))",
              border: "1px solid hsl(var(--border))",
              borderRadius: 8,
              fontSize: 12,
            }}
            labelFormatter={(_, p) => {
              const pl = p?.[0]?.payload;
              if (pl?.displayKey) return pl.displayKey;
              if (pl?.key) return pl.key;
              return "";
            }}
            formatter={(value: number) => [value.toLocaleString(), "markets"]}
          />
          <Bar
            dataKey="count"
            radius={[0, 4, 4, 0]}
            className="cursor-pointer"
            onClick={(barData: { payload?: typeof data[0] } & { key?: string }) => {
              const row = barData?.payload ?? data.find((d) => d.key === barData?.key);
              if (row) onBarClick(row);
            }}
          >
            {data.map((d, i) => (
              <Cell key={i} fill={d.color ?? "hsl(var(--primary))"} />
            ))}
            <LabelList
              dataKey="count"
              position="right"
              className="fill-muted-foreground"
              style={{ fontSize: 10 }}
              formatter={(v: number) => (typeof v === "number" ? v.toLocaleString() : "")}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
