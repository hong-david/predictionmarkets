import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { api } from "@/api/client";
import type { AnomalyRow, TradePoint } from "@/api/types";
import { Badge, priorVariant, severityVariant } from "@/components/Badge";
import { layerDisplay, priorDisplay } from "@/lib/labels";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { PriceChart } from "@/components/PriceChart";
import { EmptyState, Skeleton } from "@/components/StatusBits";
import { fmtAgo, fmtInt, fmtPrice, fmtTime } from "@/lib/utils";

export default function MarketDetailPage() {
  const { marketId = "" } = useParams<{ marketId: string }>();
  const [newsAlign, setNewsAlign] = useState<"default" | "activity">("activity");

  const detail = useQuery({
    queryKey: ["marketDetail", marketId],
    queryFn: () => api.marketDetail(marketId),
    enabled: !!marketId,
    refetchInterval: 5_000,
  });
  const series = useQuery({
    queryKey: ["marketSeries", marketId],
    queryFn: () => api.marketSeries(marketId, 2000),
    enabled: !!marketId,
    refetchInterval: 5_000,
  });
  const anomalies = useQuery({
    queryKey: ["marketAnomalies", marketId],
    queryFn: () => api.marketAnomalies(marketId, 50),
    enabled: !!marketId,
    refetchInterval: 10_000,
  });
  const news = useQuery({
    queryKey: ["marketNews", marketId, newsAlign],
    queryFn: () => api.marketNews(marketId, 12, { align: newsAlign }),
    enabled: !!marketId,
    staleTime: 5 * 60_000,
  });

  if (detail.isError) {
    return (
      <Card>
        <CardBody>
          <EmptyState>
            Market not found:{" "}
            <code className="font-mono">{marketId}</code>
          </EmptyState>
          <div className="text-center mt-2">
            <Link to="/markets" className="text-sm text-primary hover:underline">
              ← back to markets
            </Link>
          </div>
        </CardBody>
      </Card>
    );
  }

  const m = detail.data;
  const tradeHighlights = useMemo(
    () => buildTradeHighlights(series.data?.trades),
    [series.data?.trades],
  );

  return (
    <div className="space-y-4">
      <Link
        to="/markets"
        className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> All markets
      </Link>

      {/* Header */}
      <Card>
        <CardBody>
          {detail.isPending ? (
            <>
              <Skeleton className="h-6 w-2/3" />
              <Skeleton className="h-4 w-1/3 mt-2" />
            </>
          ) : m ? (
            <>
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div className="min-w-0">
                  <h1 className="text-xl font-semibold tracking-tight">
                    {m.title || m.market_id}
                  </h1>
                  {m.subtitle ? (
                    <div className="text-sm text-primary mt-0.5">
                      {m.subtitle}
                    </div>
                  ) : null}
                  <div className="mt-2 flex items-center gap-1.5 flex-wrap">
                    <code className="font-mono text-xs text-muted-foreground">
                      {m.market_id}
                    </code>
                    {m.status ? (
                      <Badge variant="outline">{m.status}</Badge>
                    ) : null}
                    {m.category ? (
                      <Badge variant="primary" className="font-mono normal-case tracking-normal">
                        {m.category}
                        {m.subcategory ? ` · ${m.subcategory}` : ""}
                      </Badge>
                    ) : null}
                    {m.manipulability_prior ? (
                      <Badge variant={priorVariant(m.manipulability_prior)}>
                        {priorDisplay(m.manipulability_prior)}
                      </Badge>
                    ) : null}
                    {m.classifier_confidence ? (
                      <span title="How sure the automatic topic label is">
                        <Badge variant="outline">
                          label confidence: {m.classifier_confidence}
                        </Badge>
                      </span>
                    ) : null}
                    {m.classifier_layer ? (
                      <Badge variant="outline" className="normal-case tracking-normal">
                        {layerDisplay(m.classifier_layer)}
                      </Badge>
                    ) : null}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
                    Last
                  </div>
                  <div className="text-3xl font-semibold num text-primary">
                    {fmtPrice(m.last_price ?? m.latest_snapshot?.last_price_dollars)}
                  </div>
                  {m.latest_snapshot?.yes_bid_dollars != null ? (
                    <div className="text-xs text-muted-foreground num mt-1">
                      Best bid {fmtPrice(m.latest_snapshot.yes_bid_dollars)} · Best ask {fmtPrice(m.latest_snapshot.yes_ask_dollars)}
                    </div>
                  ) : null}
                  {m.latest_snapshot?.liquidity_dollars != null ? (
                    <div className="text-[11px] text-muted-foreground num mt-1" title="Reported by the exchange on the last quote update">
                      Book liquidity (reported) ~ {fmtInt(Math.round(m.latest_snapshot.liquidity_dollars))} USD
                    </div>
                  ) : null}
                </div>
              </div>

              <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
                <Stat label="Trades" value={fmtInt(m.stats.trade_count)} />
                <Stat
                  label="Rule rows"
                  title="Number of materialized anomaly rows: one per ticker quote snapshot that met the score floor — not one per trade. Hot markets can have many more of these than execution prints."
                  value={fmtInt(m.anomaly_count)}
                />
                <Stat
                  label="Range"
                  value={
                    m.stats.min_yes_price != null && m.stats.max_yes_price != null
                      ? `${fmtPrice(m.stats.min_yes_price)}–${fmtPrice(m.stats.max_yes_price)}`
                      : "—"
                  }
                />
                <Stat
                  label="Last trade"
                  value={fmtAgo(m.stats.last_trade_ts)}
                />
              </div>
            </>
          ) : null}
        </CardBody>
      </Card>

      {/* Chart */}
      <Card>
        <CardHeader
          title="Price and volume over time"
          subtitle="Step price. Bars: contracts in that print (green = taker yes, red = taker no). Arrows: rule scores on quote updates — ‘volume’ uses cumulative exchange volume between polls vs recent history, not bar height, so active minutes can show many flags with tiny bars. Nearest print time. X-axis shows seconds. Pan and zoom."
          right={
            series.data ? `${fmtInt(series.data.trades.length)} trades shown` : ""
          }
        />
        <CardBody className="p-0">
          {series.isPending ? (
            <Skeleton className="h-[420px]" />
          ) : !series.data?.trades.length ? (
            <EmptyState className="h-[420px] flex items-center justify-center">
              No trade data yet for this market.
            </EmptyState>
          ) : (
            <div className="px-2 pb-2">
              <PriceChart
                series={series.data}
                anomalies={anomalies.data?.anomalies}
              />
            </div>
          )}
        </CardBody>
      </Card>

      {/* Anomalies + news side-by-side */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader
            title="Unusual activity"
            subtitle="Heuristic flags from each quote snapshot (spread, move size, etc.)"
            right={
              anomalies.data ? `${fmtInt(anomalies.data.count)} total` : ""
            }
          />
          <div>
            {anomalies.isPending ? (
              <div className="p-4">
                <Skeleton className="h-32" />
              </div>
            ) : !anomalies.data?.anomalies.length ? (
              <EmptyState>No anomalies detected for this market.</EmptyState>
            ) : (
              <ul className="divide-y divide-border max-h-[480px] overflow-auto">
                {anomalies.data.anomalies.map((a) => (
                  <AnomalyRowItem key={a.id} a={a} />
                ))}
              </ul>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Public news (GDELT)"
            right={
              <div className="flex items-center gap-1 rounded-md border border-border p-0.5 text-[11px]">
                <button
                  type="button"
                  onClick={() => setNewsAlign("activity")}
                  className={
                    newsAlign === "activity"
                      ? "rounded px-2 py-0.5 bg-secondary text-foreground"
                      : "rounded px-2 py-0.5 text-muted-foreground hover:text-foreground"
                  }
                >
                  Near last activity
                </button>
                <button
                  type="button"
                  onClick={() => setNewsAlign("default")}
                  className={
                    newsAlign === "default"
                      ? "rounded px-2 py-0.5 bg-secondary text-foreground"
                      : "rounded px-2 py-0.5 text-muted-foreground hover:text-foreground"
                  }
                >
                    30d to close
                </button>
              </div>
            }
            subtitle={
              news.data
                ? (() => {
                    const a = news.data.anchors;
                    const mode =
                      a?.align === "activity"
                        ? "Window is ~7d before the latest of last trade or flag, plus 12h, clipped to the market’s life."
                        : "Window is 30d ending at the market’s close (or now if still open).";
                    const t =
                      a?.last_trade_ts && `Last print ${fmtTime(a.last_trade_ts)} · `;
                    const f =
                      a?.last_flag_ts && `Last flag ${fmtTime(a.last_flag_ts)} · `;
                    return `From ${news.data.provider} · “${news.data.query}” · ${t ?? ""}${f ?? ""}${mode} Compare with the chart; news is the open web, not vetted for causality.`;
                  })()
                : "Loading…"
            }
          />
          <div>
            {news.isPending ? (
              <div className="p-4">
                <Skeleton className="h-32" />
              </div>
            ) : news.data?.provider === "unavailable" ? (
              <EmptyState>
                Could not load public headlines from the news service on this run.{" "}
                <span className="block text-[11px] mt-1">
                  Often a network, firewall, or rate limit; the rest of the page still works.
                </span>
              </EmptyState>
            ) : !news.data?.articles.length ? (
              <EmptyState>No matching articles in the past 30 days.</EmptyState>
            ) : (
              <ul className="divide-y divide-border max-h-[480px] overflow-auto">
                {news.data.articles.map((article, i) => (
                  <li key={i} className="px-4 py-3 hover:bg-secondary/30 transition-colors">
                    <a
                      href={article.url ?? "#"}
                      target="_blank"
                      rel="noreferrer"
                      className="block group"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="text-sm font-medium leading-tight group-hover:text-primary line-clamp-2">
                          {article.title}
                        </div>
                        <ExternalLink className="h-3 w-3 mt-0.5 text-muted-foreground flex-shrink-0" />
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                        <span>{article.source}</span>
                        {article.published_at ? (
                          <>
                            <span>·</span>
                            <span title={fmtTime(article.published_at)}>{fmtAgo(article.published_at)}</span>
                            <span className="opacity-80"> @ {fmtTime(article.published_at)}</span>
                          </>
                        ) : null}
                      </div>
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      </section>

      {/* Recent trades — useful for grading the chart visually */}
      {series.data?.trades.length ? (
        <Card>
          <CardHeader
            title="Recent trades (latest 30)"
            subtitle="Amber rows: large or jumpy print (heuristic) or a high “unusual” score vs this market’s own recent history — not a fraud judgment. “Side” is who was aggressive on the feed."
          />
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border">
                  <th className="text-left px-4 py-2 font-medium">Time (local)</th>
                  <th className="text-left px-4 py-2 font-medium">Side</th>
                  <th className="text-right px-4 py-2 font-medium">Yes price</th>
                  <th className="text-right px-4 py-2 font-medium">Δ from prev</th>
                  <th
                    className="text-right px-3 py-2 font-medium"
                    title="Size and |Δ price| vs a rolling local window; higher = more atypical for this market only (0–20 cap)"
                  >
                    Unusual
                  </th>
                  <th className="text-right px-4 py-2 font-medium">Contracts</th>
                </tr>
              </thead>
              <tbody>
                {series.data.trades.slice(-30).reverse().map((t, i) => {
                  const h = tradeHighlights?.get(t) ?? { bigSize: false, bigJump: false, delta: null as number | null };
                  const sus = t.suspicion;
                  const susHigh = sus != null && sus >= 2.0;
                  const rowFlash =
                    h.bigSize || h.bigJump || susHigh
                      ? "bg-[hsl(var(--severity-medium))]/15 ring-1 ring-inset ring-[hsl(var(--severity-medium))]/40"
                      : "";
                  return (
                    <tr
                      key={i}
                      className={`border-b border-border last:border-0 text-sm ${rowFlash}`}
                    >
                      <td className="px-4 py-1.5 num text-muted-foreground">{fmtTime(t.ts)}</td>
                      <td className="px-4 py-1.5">
                        {t.taker_side === "yes" ? (
                          <span className="text-[hsl(var(--severity-low))] uppercase text-[11px] tracking-wider font-semibold" title="Taker bought yes">yes</span>
                        ) : t.taker_side === "no" ? (
                          <span className="text-[hsl(var(--severity-high))] uppercase text-[11px] tracking-wider font-semibold" title="Taker bought no">no</span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-1.5 text-right num">{fmtPrice(t.yes_price)}</td>
                      <td className="px-4 py-1.5 text-right num">
                        {h.delta == null || Number.isNaN(h.delta) ? "—" : h.delta === 0 ? "—" : `${h.delta > 0 ? "+" : ""}${h.delta.toFixed(3)}`}
                      </td>
                      <td className="px-3 py-1.5 text-right num text-muted-foreground">
                        {sus == null ? "—" : sus.toFixed(2)}
                      </td>
                      <td className="px-4 py-1.5 text-right num">
                        {fmtInt(t.count != null ? Math.round(t.count) : null)}
                        {h.bigSize ? (
                          <span className="ml-1.5 text-[10px] uppercase text-[hsl(var(--severity-medium))] font-semibold">large</span>
                        ) : null}
                        {h.bigJump && !h.bigSize ? (
                          <span className="ml-1.5 text-[10px] uppercase text-[hsl(var(--severity-medium))] font-semibold">move</span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  );
}

function buildTradeHighlights(
  trades: TradePoint[] | undefined,
): Map<TradePoint, { bigSize: boolean; bigJump: boolean; delta: number | null }> {
  const out = new Map<
    TradePoint,
    { bigSize: boolean; bigJump: boolean; delta: number | null }
  >();
  if (!trades?.length) return out;

  const rawSizes = trades.map((t) => t.count ?? 0).filter((c) => c > 0);
  const sorted = [...rawSizes].sort((a, b) => a - b);
  const p90 = sorted.length
    ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.9))]
    : 0;
  const sizeThreshold = Math.max(25, p90);

  for (let i = 0; i < trades.length; i++) {
    const t = trades[i];
    const prev = i > 0 ? trades[i - 1] : null;
    let delta: number | null = null;
    if (t.yes_price != null && prev?.yes_price != null) {
      delta = t.yes_price - prev.yes_price;
    }
    const sz = t.count ?? 0;
    const bigSize = sz >= sizeThreshold;
    const bigJump =
      delta != null && !Number.isNaN(delta) && Math.abs(delta) >= 0.08;
    out.set(t, { bigSize, bigJump, delta });
  }
  return out;
}

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div>
      <div
        className="text-[11px] uppercase tracking-wider text-muted-foreground"
        title={title}
      >
        {label}
      </div>
      <div className="num font-semibold mt-0.5">{value}</div>
    </div>
  );
}

function AnomalyRowItem({ a }: { a: AnomalyRow }) {
  return (
    <li className="px-4 py-3">
      <div className="flex items-start gap-3">
        <div className="flex flex-col items-center min-w-[48px]">
          <Badge variant={severityVariant(a.severity)}>{a.severity}</Badge>
          <div className="mt-1 num text-sm font-semibold">{a.score.toFixed(1)}</div>
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-xs text-muted-foreground">
            {fmtAgo(a.created_at)}
          </div>
          {a.reasons?.length ? (
            <div className="mt-1 flex flex-wrap gap-1">
              {a.reasons.map((r) => (
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
  );
}
