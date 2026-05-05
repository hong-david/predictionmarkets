import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Info } from "lucide-react";
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
import type {
  BreakdownEntry,
  HistoricalSignalQaMarket,
  MarketScope,
  NewsDiagnostics,
  NewsSignal,
  PipelineComponentStatus,
  PipelineHealth,
  PipelineSummaryStatus,
  SuspiciousTrade,
} from "@/api/types";
import { Badge, severityVariant } from "@/components/Badge";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { MarketCell } from "@/components/MarketCell";
import { categoryDisplay, priorDisplay, priorShort } from "@/lib/labels";
import { EmptyState, Skeleton, StatusDot } from "@/components/StatusBits";
import { fmtAgo, fmtDollars, fmtInt, fmtPrice, fmtTime } from "@/lib/utils";

/** Match `GET /api/dashboard/overview` defaults used on first paint and in `warm_dashboard_cache_once`. */
const OVERVIEW_TOP = 10;
const OVERVIEW_ANOMALIES = 10;

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
  p.set("sort", "news_linked_trade_flag");
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
  const [marketScope, setMarketScope] = useState<MarketScope>("active");
  const overview = useQuery({
    queryKey: ["overview", marketScope, OVERVIEW_TOP, OVERVIEW_ANOMALIES],
    queryFn: () =>
      api.overview({
        top: OVERVIEW_TOP,
        anomalies: OVERVIEW_ANOMALIES,
        market_scope: marketScope,
      }),
    refetchInterval: 25_000,
    staleTime: 12_000,
  });
  const st = overview.data?.stats;
  const br = overview.data?.breakdown;
  const topM = overview.data?.top_markets;
  const recentFlags = overview.data?.recent_anomalies;
  const suspiciousTrades = overview.data?.suspicious_trades;
  const newsSignals = overview.data?.news_signals;
  const secondaryReady = overview.isSuccess;
  const newsDiagnostics = useQuery({
    queryKey: ["news-diagnostics"],
    queryFn: () => api.newsDiagnostics(),
    enabled: secondaryReady,
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: 1,
  });
  const historicalQa = useQuery({
    queryKey: ["historical-signal-qa"],
    queryFn: () => api.historicalSignalQa({ limit: 6, min_flag_score: 5 }),
    enabled: secondaryReady,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: 1,
  });

  const pageUpdatedAt = overview.dataUpdatedAt;
  const pagePending = overview.isPending;
  const pageError = overview.error;

  const updated = pageUpdatedAt
    ? `updated ${fmtAgo(new Date(pageUpdatedAt).toISOString())}`
    : "connecting…";
  const tone = pageError
    ? "error"
    : overview.isFetching
      ? "stale"
      : "live";
  const errMsg =
    pageError instanceof Error
      ? pageError.message
      : String(pageError ?? "unknown error");

  if (pageError && !st && !br && !topM) {
    return (
      <div className="space-y-4">
        <section className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
          <div className="flex flex-col items-end gap-2">
            <PipelineHealthWidget />
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <StatusDot tone="error" />
              <span>Overview API error</span>
            </div>
          </div>
        </section>
        <Card>
          <CardBody>
            <EmptyState>
              <div className="max-w-lg space-y-3 text-left">
                <p className="text-sm font-medium">The dashboard data did not load.</p>
                <p className="text-xs text-muted-foreground break-words font-mono bg-secondary/50 rounded px-2 py-1.5">
                  {errMsg}
                </p>
                <p className="text-xs text-muted-foreground leading-relaxed">
                  <strong className="text-foreground font-medium">Local dev:</strong> run the API on port 8000 (e.g.{" "}
                  <code className="text-[11px]">uvicorn app.main:app --reload</code>
                  ) while <code className="text-[11px]">npm run dev</code> proxies <code className="text-[11px]">/api</code> to it.{" "}
                  <strong className="text-foreground font-medium">Prod:</strong> set{" "}
                  <code className="text-[11px]">DATABASE_URL</code>, run migrations, and build the front end with{" "}
                  <code className="text-[11px]">cd frontend &amp;&amp; npm run build</code> before starting uvicorn.
                </p>
                <button
                  type="button"
                  onClick={() => {
                    void overview.refetch();
                  }}
                  className="text-sm rounded-md border border-border bg-card px-3 py-1.5 hover:bg-secondary transition-colors"
                >
                  Retry
                </button>
              </div>
            </EmptyState>
          </CardBody>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
          <p className="text-sm text-muted-foreground">
            Snapshot of {scopeLabel(marketScope).toLowerCase()} markets and activity · {fmtInt(st?.markets_in_scope ?? st?.markets)} in view
          </p>
        </div>
        <div className="flex flex-col items-start gap-2 sm:items-end">
          <PipelineHealthWidget />
          <ScopeToggle value={marketScope} onChange={setMarketScope} />
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <StatusDot tone={tone} />
            <span>{updated}</span>
          </div>
        </div>
      </section>

      <section className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-3 gap-3">
        {pagePending ? (
          Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-[88px]" />
          ))
        ) : st ? (
          <>
            <StatTile
              label={`${scopeLabel(marketScope)} markets`}
              value={fmtInt(st.markets)}
              sub={`${fmtInt(st.markets_active)} active/open · ${fmtInt(st.markets_historical)} historical retained · ${fmtInt(st.markets_status_unknown)} still hydrating.`}
            />
            <StatTile
              label="High watch-priority"
              value={fmtInt(st.markets_high_prior)}
              sub="Strict high-priority bucket only. Broad macro data, earnings, and team outcomes stay medium-high so the top watchlist remains reviewable."
              tone="primary"
            />
            <StatTile
              label="News kept"
              value={fmtInt(st.news_articles)}
              sub="Deduped article records saved after ingest. The pipeline keeps headlines, summaries, timing, entities, and links when they look usable."
            />
            <StatTile
              label="Trades stored"
              value={fmtInt(st.trades)}
              sub="Public exchange execution prints retained from the live feed. These are trades, not orders or quotes."
            />
            <StatTile
              label="Order-book updates"
              value={fmtInt(st.book_events)}
              sub="Retained snapshots and price-level changes showing bid/ask depth. Low-value book noise may be dropped by retention policy."
            />
            <StatTile
              label="Saved market alerts"
              value={fmtInt(st.anomalies)}
              sub={`${fmtInt(st.anomalies_high_severity)} high severity. Saved quote/book alert history for later review after raw data is compacted.`}
              tone={st.anomalies_high_severity > 0 ? "danger" : "default"}
            />
          </>
        ) : null}
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader
            title="Markets by watch priority"
            subtitle="Priority is a classifier bucket from the market topic, wording, and contract type. It tells us what deserves closer monitoring; actual alerts and news links are separate signals."
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
          </CardBody>
        </Card>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-stretch">
        <Card className="h-[32rem] overflow-hidden flex flex-col">
          <CardHeader
            title="Most traded markets"
            subtitle="Active/open markets with the most retained execution prints, 24h retained trade dollars, and exchange-reported lifetime volume."
          />
          <div className="flex-1 overflow-x-auto">
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
                    <th className="text-right px-4 py-2 font-medium">24h $ est.</th>
                    <th className="text-right px-4 py-2 font-medium">Total vol.</th>
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
                      <td className="px-4 py-2.5 text-right num text-sm">
                        {fmtDollars(m.trade_dollar_volume)}
                      </td>
                      <td className="px-4 py-2.5 text-right num text-sm">
                        {fmtInt(m.volume_total)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </Card>

        <Card className="h-[32rem] overflow-hidden flex flex-col">
          <CardHeader
            title="Market activity alerts"
            subtitle="Active/open markets with saved quote, volume, spread, and order-book alert history. These are market alerts, not individual trade accusations."
            right={
              recentFlags
                ? `${fmtInt(recentFlags.count)} shown`
                : undefined
            }
          />
          <div className="flex-1 overflow-x-auto">
            {overview.isPending ? (
              <div className="p-4">
                <Skeleton className="h-32" />
              </div>
            ) : !recentFlags?.anomalies.length ? (
              <EmptyState>No market activity alerts saved yet.</EmptyState>
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

      <Card>
        <CardHeader
          title="News-linked signals"
          subtitle="Vetted market/news links, with trade-aligned rows ranked first. Directional links are strongest; ambiguous rows are included when timing and relevance make them worth review."
          right={
            newsSignals
              ? newsSignals.min_score > 0
                ? `${fmtInt(newsSignals.count)} at score ${newsSignals.min_score.toFixed(1)}+`
                : `${fmtInt(newsSignals.count)} links`
              : undefined
          }
        />
        <div className="overflow-x-auto">
          {overview.isPending ? (
            <div className="p-4">
              <Skeleton className="h-32" />
            </div>
          ) : !newsSignals?.signals?.length ? (
            <EmptyState>
              No news-linked signals to show yet. Stored articles may exist, but none
              currently pass the market-link relevance and trade-timing filters.
            </EmptyState>
          ) : (
            <table className="w-full">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border bg-card/40">
                  <th className="text-left px-4 py-2.5 font-medium">Market</th>
                  <th className="text-left px-3 py-2.5 font-medium">Article</th>
                  <th className="text-right px-3 py-2.5 font-medium">Score</th>
                  <th className="text-left px-3 py-2.5 font-medium">Direction</th>
                  <th className="text-left px-3 py-2.5 font-medium">Why</th>
                </tr>
              </thead>
              <tbody>
                {newsSignals.signals.map((signal) => (
                  <NewsSignalRow key={signal.event_id} signal={signal} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Card>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-stretch">
        <NewsDiagnosticsCard
          data={newsDiagnostics.data}
          loading={newsDiagnostics.isPending}
          error={newsDiagnostics.error}
        />
        <HistoricalSignalQaCard
          rows={historicalQa.data?.markets ?? []}
          loading={historicalQa.isPending}
          error={historicalQa.error}
        />
      </section>

      <Card>
        <CardHeader
          title="Top trade flags"
          subtitle="Individual trade candidates ranked by the stronger of local outlier score and context score. Low-dollar one-offs are heavily discounted unless they arrive in a strong immediate cluster."
          right={
            suspiciousTrades
              ? `${fmtInt(suspiciousTrades.count)} shown from latest ${fmtInt(suspiciousTrades.sample)} prints`
              : undefined
          }
        />
        <div className="overflow-x-auto">
          {overview.isPending ? (
            <div className="p-4">
              <Skeleton className="h-32" />
            </div>
          ) : !suspiciousTrades?.trades.length ? (
            <EmptyState>No trade flags found in the recent sample.</EmptyState>
          ) : (
            <table className="w-full">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border bg-card/40">
                  <th className="text-left px-4 py-2.5 font-medium">Market</th>
                  <th className="text-left px-3 py-2.5 font-medium">Time</th>
                  <th className="text-right px-3 py-2.5 font-medium">Score</th>
                  <th className="text-right px-3 py-2.5 font-medium">Yes</th>
                  <th className="text-right px-3 py-2.5 font-medium">Contracts</th>
                  <th
                    className="text-right px-3 py-2.5 font-medium"
                    title="Estimated dollars paid in this print: contracts times the side price."
                  >
                    Est $
                  </th>
                  <th className="text-left px-3 py-2.5 font-medium">Why</th>
                </tr>
              </thead>
              <tbody>
                {suspiciousTrades.trades.map((t) => (
                  <SuspiciousTradeRow key={`${t.market_id}-${t.trade_id}`} trade={t} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      </Card>
    </div>
  );
}

function scopeLabel(scope: MarketScope): string {
  if (scope === "historical") return "Historical";
  if (scope === "all") return "All tracked";
  return "Active/open";
}

function ScopeToggle({
  value,
  onChange,
}: {
  value: MarketScope;
  onChange: (scope: MarketScope) => void;
}) {
  const options: { value: MarketScope; label: string; title: string }[] = [
    {
      value: "active",
      label: "Active",
      title: "Markets still open or active according to status/close time.",
    },
    {
      value: "historical",
      label: "Historical",
      title: "Closed, finalized, settled, or past-close markets kept for review.",
    },
    {
      value: "all",
      label: "All",
      title: "Active and historical retained markets.",
    },
  ];
  return (
    <div className="flex items-center gap-1 rounded-md border border-border p-0.5 text-[11px]">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          title={option.title}
          onClick={() => onChange(option.value)}
          className={
            value === option.value
              ? "rounded px-2 py-0.5 bg-secondary text-foreground"
              : "rounded px-2 py-0.5 text-muted-foreground hover:text-foreground"
          }
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function PipelineHealthWidget() {
  const [expanded, setExpanded] = useState(false);
  const health = useQuery({
    queryKey: ["pipeline-health"],
    queryFn: () => api.pipelineHealth(),
    enabled: expanded,
    staleTime: 45_000,
    refetchInterval: expanded ? 120_000 : false,
    retry: 1,
  });
  const data = health.data;
  const summaryTone = health.isError
    ? "error"
    : !expanded
      ? "empty"
      : pipelineSummaryTone(data?.summary.status);
  const summaryText = health.isError
    ? "Health unavailable"
    : !expanded
      ? "Expand for status"
      : health.isPending && !data
        ? "Checking…"
        : pipelineSummaryText(data);
  const errorMessage =
    health.error instanceof Error
      ? health.error.message
      : String(health.error ?? "Pipeline health could not be loaded.");

  return (
    <div className="relative">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
        className="inline-flex items-center gap-2 rounded-md border border-border bg-card/80 px-2.5 py-1.5 text-xs text-muted-foreground shadow-sm hover:bg-secondary/60 hover:text-foreground transition-colors"
      >
        <StatusDot tone={summaryTone} />
        <span className="font-medium text-foreground">Pipeline health</span>
        <span>{summaryText}</span>
        <ChevronDown
          className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-180" : ""}`}
        />
      </button>
      {expanded ? (
        <div className="absolute right-0 top-full z-30 mt-2 w-[min(92vw,440px)] overflow-hidden rounded-lg border border-border bg-card text-card-foreground shadow-lg">
          <div className="border-b border-border px-3 py-2">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-xs font-semibold">Pipeline health</div>
                <div className="text-[11px] text-muted-foreground">
                  Jobs, materializers, feeds, and projections
                </div>
              </div>
              {data ? (
                <div className="text-[11px] text-muted-foreground">
                  {fmtAgo(data.generated_at)}
                </div>
              ) : null}
            </div>
          </div>
          {health.isError ? (
            <div className="px-3 py-3 text-xs text-muted-foreground">
              <div className="font-medium text-foreground">Health unavailable</div>
              <div className="mt-1 break-words">{errorMessage}</div>
            </div>
          ) : !data ? (
            <div className="px-3 py-3">
              <Skeleton className="h-24" />
            </div>
          ) : (
            <ul className="max-h-[420px] overflow-y-auto divide-y divide-border">
              {data.components.map((component) => (
                <li key={component.key} className="px-3 py-2.5">
                  <div className="flex items-start gap-2">
                    <div className="pt-1">
                      <StatusDot tone={pipelineComponentTone(component.status)} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <div className="truncate text-xs font-medium text-foreground">
                          {component.label}
                        </div>
                        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                          {component.status}
                        </div>
                      </div>
                      <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                        {component.detail || component.description}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                        <span>latest {fmtAgo(component.latest_at)}</span>
                        {component.heartbeat_at ? (
                          <span>heartbeat {fmtAgo(component.heartbeat_at)}</span>
                        ) : null}
                        {component.last_success_at ? (
                          <span>success {fmtAgo(component.last_success_at)}</span>
                        ) : null}
                        {component.count != null ? (
                          <span>
                            {fmtInt(component.count)}{" "}
                            {component.source === "heartbeat" ? "last batch" : "rows"}
                          </span>
                        ) : null}
                        {component.component_type ? (
                          <span>{component.component_type}</span>
                        ) : null}
                      </div>
                      {component.last_error ? (
                        <div className="mt-1 text-[11px] leading-snug text-[hsl(var(--severity-high))]">
                          {component.last_error}
                        </div>
                      ) : null}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}

function pipelineSummaryTone(status: PipelineSummaryStatus | undefined) {
  if (status === "healthy") return "live";
  if (status === "error") return "error";
  if (status === "empty") return "empty";
  return "stale";
}

function pipelineComponentTone(status: PipelineComponentStatus) {
  if (status === "healthy") return "live";
  if (status === "error") return "error";
  if (status === "empty") return "empty";
  return "stale";
}

function pipelineSummaryText(data: PipelineHealth | undefined): string {
  if (!data) return "Checking";
  const s = data.summary;
  if (s.status === "healthy") return "All live";
  if (s.error > 0) return `${s.error} error${s.error === 1 ? "" : "s"}`;
  if (s.stale > 0) return `${s.stale} stale`;
  if (s.empty > 0) return `${s.empty} empty`;
  return "Degraded";
}

function NewsDiagnosticsCard({
  data,
  loading,
  error,
}: {
  data: NewsDiagnostics | undefined;
  loading?: boolean;
  error?: unknown;
}) {
  const err = error instanceof Error ? error.message : String(error ?? "");
  const topFeeds = (data?.rss_feed_details ?? []).slice(0, 6);
  return (
    <Card>
      <CardHeader
        title="News ingest diagnostics"
        subtitle="What the last global news sweep saw before relevance filtering and market linking."
        right={data?.provider_status ?? undefined}
      />
      <CardBody>
        {loading && !data ? (
          <Skeleton className="h-40" />
        ) : error ? (
          <EmptyState>
            <div className="text-left">
              <div className="font-medium text-foreground">Diagnostics unavailable</div>
              <div className="mt-1 text-xs break-words">{err}</div>
            </div>
          </EmptyState>
        ) : !data ? (
          <EmptyState>No news ingest heartbeat yet.</EmptyState>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <MiniMetric
                label="stored"
                value={fmtInt(data.summary.articles_stored)}
                help="Total deduped article records currently saved after ingest."
              />
              <MiniMetric
                label="seen run"
                value={fmtInt(data.summary.articles_seen_last_run)}
                help="Articles fetched or seen during the latest news sweep before dedupe and market-link filtering."
              />
              <MiniMetric
                label="linked"
                value={fmtInt(data.summary.news_events_linked)}
                help="Article-to-market links kept after relevance scoring, market-direction scoring, and category precision gates."
              />
              <MiniMetric
                label="correlated"
                value={fmtInt(data.summary.positive_correlations)}
                help="Linked news rows with a positive pre-news trade correlation score."
              />
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>latest {fmtAgo(data.latest_at)}</span>
              <span>{fmtInt(data.source_registry.rss_available)} RSS feeds available</span>
              {data.source_counts.gdelt != null ? (
                <span>GDELT {fmtInt(Number(data.source_counts.gdelt))}</span>
              ) : null}
              {data.source_counts.congress_api_disabled ? (
                <span>Congress API disabled</span>
              ) : null}
            </div>
            {data.fetch_error ? (
              <div className="rounded-md border border-[hsl(var(--severity-medium))]/30 bg-secondary/40 px-2 py-1.5 text-xs text-muted-foreground">
                {data.fetch_error}
              </div>
            ) : null}
            <div className="space-y-1">
              <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
                Top feeds last run
              </div>
              {!topFeeds.length ? (
                <div className="text-xs text-muted-foreground">No per-feed counts recorded yet.</div>
              ) : (
                <ul className="divide-y divide-border rounded-md border border-border">
                  {topFeeds.map((feed) => (
                    <li key={feed.source ?? feed.label ?? "feed"} className="flex items-center justify-between gap-3 px-2 py-1.5 text-xs">
                      <div className="min-w-0">
                        <div className="truncate text-foreground">
                          {feed.label ?? feed.source ?? "Feed"}
                        </div>
                        <div className="truncate text-[11px] text-muted-foreground">
                          {feed.authority_tier ?? feed.source_tier ?? "source"}
                          {feed.error ? ` · ${feed.error}` : ""}
                        </div>
                      </div>
                      <div className="num text-muted-foreground">{fmtInt(feed.count)}</div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function HistoricalSignalQaCard({
  rows,
  loading,
  error,
}: {
  rows: HistoricalSignalQaMarket[];
  loading?: boolean;
  error?: unknown;
}) {
  const err = error instanceof Error ? error.message : String(error ?? "");
  return (
    <Card>
      <CardHeader
        title="Historical signal QA"
        subtitle="Closed or aged-out markets kept for post-mortem review of trade flags, pre-news timing, and quote/book alerts."
      />
      <CardBody>
        {loading ? (
          <Skeleton className="h-40" />
        ) : error ? (
          <EmptyState>
            <div className="text-left">
              <div className="font-medium text-foreground">Historical QA unavailable</div>
              <div className="mt-1 text-xs break-words">{err}</div>
            </div>
          </EmptyState>
        ) : !rows.length ? (
          <EmptyState>No historical signal sample yet.</EmptyState>
        ) : (
          <ul className="divide-y divide-border">
            {rows.map((row) => (
              <li key={row.market_id} className="py-2.5 first:pt-0 last:pb-0">
                <Link to={`/markets/${encodeURIComponent(row.market_id)}`} className="block hover:text-primary">
                  <div className="line-clamp-1 text-sm font-medium">{row.title}</div>
                </Link>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <span>{row.qa_label.replace(/_/g, " ")}</span>
                  <span>{fmtInt(row.trade_count)} trades</span>
                  <span>best flag {row.flags.best_score.toFixed(1)}</span>
                  {row.pre_news.best_score > 0 ? (
                    <span>pre-news {row.pre_news.best_score.toFixed(1)}</span>
                  ) : null}
                  <span>{fmtAgo(row.close_time)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

function MiniMetric({
  label,
  value,
  help,
}: {
  label: string;
  value: string;
  help: string;
}) {
  return (
    <div className="rounded-md border border-border bg-secondary/30 px-2 py-1.5">
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>{label}</span>
        <span
          title={help}
          aria-label={help}
          className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full text-muted-foreground hover:text-foreground"
        >
          <Info className="h-3 w-3" aria-hidden="true" />
        </span>
      </div>
      <div className="mt-0.5 num text-sm font-semibold">{value}</div>
    </div>
  );
}

function NewsSignalRow({ signal }: { signal: NewsSignal }) {
  const href = `/markets/${encodeURIComponent(signal.market_id)}`;
  const leakageMinutes =
    signal.leakage_window_seconds != null
      ? Math.round(signal.leakage_window_seconds / 60)
      : null;
  return (
    <tr className="border-b border-border last:border-0 hover:bg-secondary/30 transition-colors">
      <td className="px-4 py-2.5">
        <Link to={href}>
          <MarketCell market={signal} showPrior link={false} />
        </Link>
      </td>
      <td className="px-3 py-2.5 max-w-[420px]">
        <a
          href={signal.article_url ?? "#"}
          target="_blank"
          rel="noreferrer"
          className="block text-sm font-medium leading-tight hover:text-primary line-clamp-2"
        >
          {signal.article_title}
        </a>
        <div className="mt-1 text-xs text-muted-foreground">
          {signal.article_source ?? "unknown source"}
          {signal.first_seen_at ? (
            <>
              <span> · </span>
              <span>{fmtAgo(signal.first_seen_at)}</span>
            </>
          ) : null}
          {leakageMinutes != null ? (
            <>
              <span> · </span>
              <span>{fmtInt(leakageMinutes)}m lead</span>
            </>
          ) : null}
        </div>
      </td>
      <td className="px-3 py-2.5 text-right">
        {signal.pre_news_trade_score > 0 ? (
          <div className="num font-semibold text-[hsl(var(--severity-high))]">
            {signal.pre_news_trade_score.toFixed(2)}
          </div>
        ) : (
          <div className="flex-1 overflow-x-auto">
            <div className="num font-semibold text-foreground">
              {signal.relevance_score.toFixed(2)}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              relevance
            </div>
          </div>
        )}
      </td>
      <td className="px-3 py-2.5 text-xs text-muted-foreground">
        {signal.direction_label ?? "ambiguous"}
      </td>
      <td className="px-3 py-2.5">
        <div className="flex flex-wrap gap-1">
          {(signal.reasons.length ? signal.reasons : [signal.status]).slice(0, 4).map((r) => (
            <code
              key={r}
              className="text-[10px] rounded bg-secondary px-1.5 py-0.5 font-mono text-muted-foreground"
            >
              {r}
            </code>
          ))}
        </div>
      </td>
    </tr>
  );
}

function SuspiciousTradeRow({ trade }: { trade: SuspiciousTrade }) {
  const href = `/markets/${encodeURIComponent(trade.market_id)}?trade_ts=${encodeURIComponent(
    trade.ts ?? "",
  )}`;
  return (
    <tr className="border-b border-border last:border-0 hover:bg-secondary/30 transition-colors">
      <td className="px-4 py-2.5">
        <Link to={href}>
          <MarketCell market={trade} showPrior link={false} />
        </Link>
      </td>
      <td className="px-3 py-2.5 text-xs text-muted-foreground">
        {fmtTime(trade.ts)}
      </td>
      <td className="px-3 py-2.5 text-right num font-semibold text-[hsl(var(--severity-medium))]">
        {trade.suspicion.toFixed(2)}
      </td>
      <td className="px-3 py-2.5 text-right num text-sm">
        {fmtPrice(trade.yes_price)}
      </td>
      <td className="px-3 py-2.5 text-right num text-sm">
        {fmtInt(trade.count != null ? Math.round(trade.count) : null)}
      </td>
      <td className="px-3 py-2.5 text-right num text-sm">
        {fmtDollars(trade.trade_dollar_amount)}
      </td>
      <td className="px-3 py-2.5">
        <div className="flex flex-wrap gap-1">
          {(trade.reasons.length ? trade.reasons : ["statistical_outlier"]).map((r) => (
            <code
              key={r}
              className="text-[10px] rounded bg-secondary px-1.5 py-0.5 font-mono text-muted-foreground"
            >
              {r}
            </code>
          ))}
        </div>
      </td>
    </tr>
  );
}

type BreakdownChartDatum = BreakdownEntry & {
  color?: string;
  label?: string;
  displayKey?: string;
};

type BreakdownBarChartProps = {
  data: BreakdownChartDatum[];
  loading?: boolean;
  yTick: (key: string) => string;
  onBarClick: (row: BreakdownEntry) => void;
};

function BreakdownBarChart({
  data,
  loading,
  yTick,
  onBarClick,
}: BreakdownBarChartProps) {
  if (loading) return <Skeleton className="h-[200px]" />;
  if (!data.length) return <EmptyState>No data.</EmptyState>;
  return (
    <div className="h-[240px] w-full">
      <ResponsiveContainer>
        <BarChart
          data={data}
          layout="vertical"
          margin={{ left: 4, right: 56, top: 4, bottom: 4 }}
          className="cursor-pointer"
          onClick={(state: { activePayload?: Array<{ payload?: typeof data[0] }> }) => {
            const row = state?.activePayload?.[0]?.payload;
            if (row) onBarClick(row);
          }}
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
            width={200}
            interval={0}
            tick={
              <BreakdownYAxisTick
                data={data}
                yTick={yTick}
                onBarClick={onBarClick}
              />
            }
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

function BreakdownYAxisTick({
  x,
  y,
  payload,
  data,
  yTick,
  onBarClick,
}: {
  x?: number;
  y?: number;
  payload?: { value?: string };
  data: BreakdownBarChartProps["data"];
  yTick: (key: string) => string;
  onBarClick: (row: BreakdownEntry) => void;
}) {
  const key = String(payload?.value ?? "");
  const row = data.find((d) => d.key === key);
  return (
    <g
      transform={`translate(${x ?? 0},${y ?? 0})`}
      className={row ? "cursor-pointer" : undefined}
      onClick={() => {
        if (row) onBarClick(row);
      }}
    >
      <text
        x={0}
        y={0}
        dy={4}
        textAnchor="end"
        fill="hsl(var(--muted-foreground))"
        fontSize={10}
      >
        {yTick(key)}
      </text>
    </g>
  );
}
