import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { AnomalyRow, NewsArticle, SearchNewsResult, TradePoint } from "@/api/types";
import { Badge, priorVariant, severityVariant } from "@/components/Badge";
import { categoryDisplay, layerDisplay, priorDisplay } from "@/lib/labels";
import { humanizeAnomalyReason } from "@/lib/reasonPhrases";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { type ChartNewsEvent, PriceChart } from "@/components/PriceChart";
import { EmptyState, Skeleton } from "@/components/StatusBits";
import { kalshiMarketUrl } from "@/lib/kalshi";
import {
  fmtAgo,
  fmtDollars,
  fmtInt,
  fmtPrice,
  fmtTime,
  tradeTimestampsForAudit,
} from "@/lib/utils";

export default function MarketDetailPage() {
  const { marketId = "" } = useParams<{ marketId: string }>();
  const [searchParams] = useSearchParams();
  const highlightedTradeTs = searchParams.get("trade_ts");
  const [newsAlign, setNewsAlign] = useState<"default" | "activity">("activity");

  const detail = useQuery({
    queryKey: ["marketDetail", marketId],
    queryFn: () => api.marketDetail(marketId),
    enabled: !!marketId,
    refetchInterval: 30_000,
    staleTime: 10_000,
  });
  const isActiveMarket = detail.data?.market_lifecycle === "active";
  const series = useQuery({
    queryKey: ["marketSeries", marketId],
    queryFn: () => api.marketSeries(marketId, 600),
    enabled: !!marketId,
    refetchInterval: isActiveMarket ? 15_000 : false,
    staleTime: 10_000,
  });
  const anomalies = useQuery({
    queryKey: ["marketAnomalies", marketId],
    queryFn: () => api.marketAnomalies(marketId, 50),
    enabled: !!marketId,
    refetchInterval: isActiveMarket ? 30_000 : false,
    staleTime: 15_000,
  });
  const news = useQuery({
    queryKey: ["marketNews", marketId, newsAlign],
    queryFn: () => api.marketNews(marketId, 12, { align: newsAlign }),
    enabled: !!marketId,
    staleTime: 5 * 60_000,
  });
  const relatedNewsSearchText =
    detail.data?.news_search_query ||
    [detail.data?.title, detail.data?.subtitle].filter(Boolean).join(" ");
  const relatedNews = useQuery({
    queryKey: ["marketRelatedNewsSearch", marketId, relatedNewsSearchText],
    queryFn: () => api.search(relatedNewsSearchText, "news", 8),
    enabled:
      !!relatedNewsSearchText &&
      !!news.data &&
      (news.data.provider === "unavailable" || news.data.articles.length === 0),
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
  const relatedSearchNews = useMemo(
    () =>
      (relatedNews.data?.news ?? []).filter((article) =>
        isRelatedSearchArticle(
          article,
          relatedNewsSearchText,
          detail.data?.market_id,
          detail.data?.event_id,
        ),
      ),
    [
      detail.data?.event_id,
      detail.data?.market_id,
      relatedNews.data?.news,
      relatedNewsSearchText,
    ],
  );
  const chartNewsEvents = useMemo(
    () => buildChartNewsEvents(news.data?.articles ?? [], relatedSearchNews),
    [news.data?.articles, relatedSearchNews],
  );
  const kalshiHref = kalshiMarketUrl(m?.market_id, m?.event_id);
  const { tradeHighlights, topSuspiciousTrades } = useMemo(() => {
    const trades = series.data?.trades;
    if (!trades?.length) {
      return {
        tradeHighlights: new Map<
          TradePoint,
          { bigSize: boolean; bigJump: boolean; delta: number | null }
        >(),
        topSuspiciousTrades: [] as TradePoint[],
      };
    }
    const highlights = buildTradeHighlights(trades);
    const topSuspiciousTrades = [...trades]
      .sort((a, b) => compareTradesBySuspiciousness(a, b, highlights))
      .slice(0, 30);
    return { tradeHighlights: highlights, topSuspiciousTrades };
  }, [series.data?.trades]);

  const alertWhyBullets = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const a of anomalies.data?.anomalies ?? []) {
      for (const r of a.reasons ?? []) {
        const h = humanizeAnomalyReason(r);
        if (!seen.has(h)) {
          seen.add(h);
          out.push(h);
        }
        if (out.length >= 10) break;
      }
      if (out.length >= 10) break;
    }
    return out;
  }, [anomalies.data?.anomalies]);

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
                  {kalshiHref ? (
                    <a
                      href={kalshiHref}
                      target="_blank"
                      rel="noreferrer"
                      className="group inline-flex max-w-full items-start gap-1.5 text-xl font-semibold tracking-tight hover:text-primary"
                    >
                      <span className="min-w-0 break-words">
                        {m.title || m.market_id}
                      </span>
                      <ExternalLink className="mt-1 h-4 w-4 flex-shrink-0 text-muted-foreground group-hover:text-primary" />
                    </a>
                  ) : (
                    <h1 className="text-xl font-semibold tracking-tight">
                      {m.title || m.market_id}
                    </h1>
                  )}
                  {m.subtitle ? (
                    <div className="text-sm text-primary mt-0.5">
                      {m.subtitle}
                    </div>
                  ) : null}
                  <div className="mt-2 flex items-center gap-1.5 flex-wrap">
                    <code
                      className="font-mono text-xs text-muted-foreground"
                      title="Kalshi market ticker (this contract’s tape and chart)"
                    >
                      {m.market_id}
                    </code>
                    {m.status ? (
                      <Badge variant="outline">{m.status}</Badge>
                    ) : null}
                    <Badge
                      variant={m.market_lifecycle === "active" ? "success" : "outline"}
                      className="normal-case tracking-normal"
                    >
                      {m.market_lifecycle === "active"
                        ? "active/open market"
                        : "historical market"}
                    </Badge>
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
                  {m.event_id ? (
                    <div className="mt-2 text-xs text-muted-foreground">
                      <span className="text-[11px] uppercase tracking-wider">
                        Event (Kalshi event_ticker)
                      </span>
                      <div className="mt-0.5 flex items-center gap-2 flex-wrap">
                        <code className="font-mono text-[11px] break-all">
                          {m.event_id}
                        </code>
                        <Link
                          to={`/events/${encodeURIComponent(m.event_id)}`}
                          className="text-primary hover:underline whitespace-nowrap"
                        >
                          All contracts in this event
                        </Link>
                      </div>
                    </div>
                  ) : null}
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
                  label="Alert history"
                  title="Saved market-level alert rows for this market. Mostly quote/book snapshots that met the score floor, not individual trades."
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
              <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 gap-3 text-sm border-t border-border pt-3">
                <Stat
                  label="Watch priority"
                  title="Classifier bucket for how sensitive this market type may be to news or manipulation. It is not a suspicious-activity verdict."
                  value={priorDisplay(m.market_priority ?? "unclassified")}
                />
                <Stat
                  label="Topic"
                  title="Automatically assigned from exchange tags and market text."
                  value={m.category ? categoryDisplay(m.category) : "-"}
                />
                <Stat
                  label="Classifier"
                  title="Confidence and rule source for the stored market labels."
                  value={[
                    m.classifier_confidence ?? null,
                    layerDisplay(m.classifier_layer) || null,
                  ]
                    .filter(Boolean)
                    .join(" / ") || "-"}
                />
              </div>
              {m.reasons && m.reasons.length > 0 ? (
                <div
                  className="mt-2 text-xs text-muted-foreground font-mono"
                  title="Machine-readable reason slugs from materialized alert rows (deduped)"
                >
                  Reason codes: {m.reasons.join(" · ")}
                </div>
              ) : null}
            </>
          ) : null}
        </CardBody>
      </Card>

      {/* Chart */}
      <Card>
        <CardHeader
          title="Price and volume over time"
          subtitle="Each point is a trade’s yes price. Curve: spline through prints (peaks are real prints, not a bid/ask band). Crosshair: your browser’s local time, plus ET and UTC. Compare to Kalshi in the same contract ticker and time zone. If several prints share one second, the x-axis nudges +1s so every print is visible. Bars: contracts in that print. Arrows: saved alert rows on quotes (volume uses cumulative exchange volume, not bar height). Pan and zoom."
          right={
            series.data
              ? [
                  `${fmtInt(series.data.trades.length)} trades shown`,
                  series.data.tape_cluster
                    ? ` · tape burst ${series.data.tape_cluster.burst_score_0_10.toFixed(1)}/10 (${series.data.tape_cluster.largest_window_count} in ${series.data.tape_cluster.window_sec}s${
                        series.data.tape_cluster.dominant_side
                          ? `, ${series.data.tape_cluster.dominant_side}`
                          : ""
                      })`
                    : "",
                ]
                  .filter(Boolean)
                  .join("")
              : ""
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
                highlightTs={highlightedTradeTs}
                newsEvents={chartNewsEvents}
              />
            </div>
          )}
        </CardBody>
      </Card>

      {/* Anomalies + news side-by-side */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader
            title="Alert history"
            subtitle="Saved market-level quote, volume, spread, and order-book alerts kept for review after raw data is compacted."
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
              <EmptyState>No alert history for this market.</EmptyState>
            ) : (
              <>
                {alertWhyBullets.length > 0 ? (
                  <div className="px-4 py-3 border-b border-border bg-secondary/20 text-xs text-muted-foreground">
                    <div className="font-medium text-foreground mb-2">
                      Why you might see an alert
                    </div>
                    <ul className="list-disc pl-4 space-y-1.5 leading-snug">
                      {alertWhyBullets.map((t) => (
                        <li key={t}>{t}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
                <ul className="divide-y divide-border max-h-[480px] overflow-auto">
                  {anomalies.data.anomalies.map((a) => (
                    <AnomalyRowItem key={a.id} a={a} />
                  ))}
                </ul>
              </>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Linked news signals"
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
              relatedNews.isPending ? (
                <div className="p-4">
                  <Skeleton className="h-32" />
                </div>
              ) : relatedSearchNews.length ? (
                <RelatedNewsSearchList articles={relatedSearchNews} />
              ) : (
                <EmptyState>
                  No linked news found for this market yet.{" "}
                  <span className="block text-[11px] mt-1">
                    The external headline source may be unavailable, but the local
                    news index also has no relevant stored article for this market.
                  </span>
                </EmptyState>
              )
            ) : !news.data?.articles.length ? (
              relatedNews.isPending ? (
                <div className="p-4">
                  <Skeleton className="h-32" />
                </div>
              ) : relatedSearchNews.length ? (
                <RelatedNewsSearchList articles={relatedSearchNews} />
              ) : (
                <EmptyState>
                  No linked news found for this market yet.{" "}
                  <span className="block text-[11px] mt-1">
                    Nothing stored or returned for the current news window.
                  </span>
                </EmptyState>
              )
            ) : (
              <ul className="divide-y divide-border max-h-[480px] overflow-auto">
                {news.data.articles.map((article, i) => {
                  const score = article.pre_news_trade_score ?? 0;
                  const leakageMinutes =
                    article.leakage_window_seconds != null
                      ? Math.round(article.leakage_window_seconds / 60)
                      : null;
                  return (
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
                      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
                        {score > 0 ? (
                          <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-foreground">
                            news/trade {score.toFixed(1)}
                          </span>
                        ) : null}
                        {article.direction_label ? (
                          <span className="rounded border border-border px-1.5 py-0.5 text-muted-foreground">
                            {article.direction_label}
                          </span>
                        ) : null}
                        {leakageMinutes != null ? (
                          <span className="rounded border border-border px-1.5 py-0.5 text-muted-foreground">
                            {fmtInt(leakageMinutes)}m before news
                          </span>
                        ) : null}
                        <NewsExplainHover article={article} />
                        {(article.reasons ?? []).slice(0, 3).map((reason) => (
                          <code
                            key={reason}
                            className="rounded bg-secondary/70 px-1.5 py-0.5 font-mono text-muted-foreground"
                          >
                            {reason}
                          </code>
                        ))}
                      </div>
                    </a>
                  </li>
                  );
                })}
              </ul>
            )}
          </div>
        </Card>
      </section>

      {/* Recent trades — useful for grading the chart visually */}
      {series.data?.trades.length ? (
        <Card>
          <CardHeader
            title="Local trade outliers"
            subtitle="Top 30 recent trades in this market ranked by local tape behavior: size, price jump, same-side clustering, then time. This is for chart review and is narrower than the global trade flags."
          />
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border">
                  <th
                    className="text-left px-4 py-2 font-medium"
                    title="Local browser time, US Eastern, and UTC for the same exchange timestamp — use when comparing to Kalshi’s site."
                  >
                    Time
                  </th>
                  <th className="text-left px-4 py-2 font-medium">Side</th>
                  <th className="text-right px-4 py-2 font-medium">Yes price</th>
                  <th className="text-right px-4 py-2 font-medium">Δ from prev</th>
                  <th
                    className="text-right px-3 py-2 font-medium"
                    title="Local trade outlier score: size and |Δ yes price| vs recent prints on this market (0–10, not a verdict)"
                  >
                    Outlier
                  </th>
                  <th
                    className="text-right px-3 py-2 font-medium"
                    title="0–10: many prints in a short window with skewed taker side (behavioral cluster, not an account id)"
                  >
                    Cluster
                  </th>
                  <th className="text-right px-4 py-2 font-medium">Contracts</th>
                  <th
                    className="text-right px-4 py-2 font-medium"
                    title="Estimated dollars paid in this print: contracts times the yes/no side price."
                  >
                    Est $
                  </th>
                </tr>
              </thead>
              <tbody>
                {topSuspiciousTrades.map((t, i) => {
                  const h = tradeHighlights?.get(t) ?? { bigSize: false, bigJump: false, delta: null as number | null };
                  const sus = t.suspicion;
                  const cl = t.cluster_0_10;
                  const susHigh = sus != null && sus >= 3.5;
                  const clHigh = cl != null && cl >= 3.5;
                  const selected = highlightedTradeTs && t.ts === highlightedTradeTs;
                  const rowFlash =
                    selected || h.bigSize || h.bigJump || susHigh || clHigh
                      ? "bg-[hsl(var(--severity-medium))]/15 ring-1 ring-inset ring-[hsl(var(--severity-medium))]/40"
                      : "";
                  const ts3 = tradeTimestampsForAudit(t.ts);
                  return (
                    <tr
                      key={`${t.ts ?? ""}-${i}-${t.suspicion ?? 0}`}
                      className={`border-b border-border last:border-0 text-sm ${rowFlash}`}
                    >
                      <td
                        className="px-4 py-1.5 text-muted-foreground"
                        title={`Local: ${ts3.local}\nET: ${ts3.eastern}\nUTC: ${ts3.utc}`}
                      >
                        <div className="text-foreground text-xs num">{ts3.local}</div>
                        <div className="text-[10px] num leading-tight">
                          {ts3.eastern} · {ts3.utc}
                        </div>
                      </td>
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
                      <td className="px-3 py-1.5 text-right num text-muted-foreground">
                        {cl == null ? "—" : cl.toFixed(2)}
                      </td>
                      <td className="px-4 py-1.5 text-right num">
                        {fmtInt(t.count != null ? Math.round(t.count) : null)}
                      </td>
                      <td className="px-4 py-1.5 text-right num">
                        {fmtDollars(t.trade_dollar_amount ?? estimateTradeDollars(t))}
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

/** Sort descending: API outlier, burst cluster, then heuristic flags, then newest. */
function compareTradesBySuspiciousness(
  a: TradePoint,
  b: TradePoint,
  h: Map<TradePoint, { bigSize: boolean; bigJump: boolean; delta: number | null }>,
): number {
  const sa = a.suspicion ?? 0;
  const sb = b.suspicion ?? 0;
  if (sb !== sa) return sb - sa;
  const ca = a.cluster_0_10 ?? 0;
  const cb = b.cluster_0_10 ?? 0;
  if (cb !== ca) return cb - ca;
  const ha = (h.get(a)?.bigSize ? 2 : 0) + (h.get(a)?.bigJump ? 1 : 0);
  const hb = (h.get(b)?.bigSize ? 2 : 0) + (h.get(b)?.bigJump ? 1 : 0);
  if (hb !== ha) return hb - ha;
  return String(b.ts ?? "").localeCompare(String(a.ts ?? ""));
}

function estimateTradeDollars(t: TradePoint): number | null {
  if (t.count == null) return null;
  const side = (t.taker_side ?? "").toLowerCase();
  const price =
    side === "no" && t.no_price != null ? t.no_price : t.yes_price ?? t.no_price;
  return price == null ? null : t.count * price;
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

function buildChartNewsEvents(
  exactArticles: NewsArticle[],
  searchArticles: SearchNewsResult[],
): ChartNewsEvent[] {
  const exact = exactArticles
    .map((article) => ({
      ts: article.first_seen_at ?? article.published_at ?? null,
      title: article.title,
      source: article.source,
      direction_label: article.direction_label,
      score: article.pre_news_trade_score ?? article.relevance_score ?? null,
    }))
    .filter((event) => event.ts);
  if (exact.length) return exact;
  return searchArticles
    .map((article) => ({
      ts: article.first_seen_at ?? article.published_at ?? null,
      title: article.title,
      source: article.source,
      direction_label: null,
      score: article.pre_news_trade_score ?? article.score ?? null,
    }))
    .filter((event) => event.ts);
}

function NewsExplainHover({
  article,
}: {
  article: NewsArticle | SearchNewsResult;
}) {
  const newsArticle = article as NewsArticle;
  const direction = recordValue(newsArticle.market_direction);
  const relevance = recordValue(newsArticle.relevance_components);
  const candidate = recordValue(newsArticle.candidate_generation);
  const correlation = recordValue(newsArticle.news_trade_correlation);
  const evidenceTerms = stringList(direction.evidence_terms).slice(0, 5);
  const candidateReasons = stringList(candidate.candidate_reasons).slice(0, 5);
  const matchedTerms = stringList(candidate.matched_terms).slice(0, 5);
  const factorHits = recordValue(relevance.factor_hits);
  const correlationReasons = stringList(correlation.reasons).slice(0, 5);
  const directionLabel =
    textValue(newsArticle.direction_label) ||
    textValue(direction.label) ||
    "unknown";
  const directionConfidence =
    numberValue(newsArticle.direction_confidence) ?? numberValue(direction.confidence);
  const relevanceScore =
    numberValue(newsArticle.relevance_score) ??
    numberValue((article as SearchNewsResult).score);
  const newsTradeScore =
    numberValue(newsArticle.pre_news_trade_score) ??
    numberValue((article as SearchNewsResult).pre_news_trade_score);

  return (
    <span className="relative inline-flex group" tabIndex={0}>
      <span className="rounded border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground group-hover:text-foreground">
        Why
      </span>
      <span className="pointer-events-none absolute left-0 top-full z-30 mt-1 hidden w-[min(82vw,360px)] rounded-lg border border-border bg-card p-3 text-left text-[11px] leading-snug text-muted-foreground shadow-xl group-hover:block group-focus-within:block">
        <span className="block text-xs font-semibold text-foreground">
          Why this news is linked
        </span>
        <span className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1">
          <ExplainMetric label="Relevance" value={formatExplainScore(relevanceScore)} />
          <ExplainMetric label="News/trade" value={formatExplainScore(newsTradeScore)} />
          <ExplainMetric label="Direction" value={directionLabel} />
          <ExplainMetric
            label="Confidence"
            value={formatExplainPercent(directionConfidence)}
          />
        </span>
        <ExplainLine
          label="Orientation"
          value={textValue(direction.market_orientation)}
        />
        <ExplainLine
          label="Underlier"
          value={textValue(direction.underlier_direction)}
        />
        <ExplainLine label="Rationale" value={textValue(direction.rationale)} />
        <ExplainLine
          label="Evidence terms"
          value={evidenceTerms.length ? evidenceTerms.join(", ") : null}
        />
        <ExplainLine
          label="Candidate reasons"
          value={candidateReasons.length ? candidateReasons.join(", ") : null}
        />
        <ExplainLine
          label="Matched terms"
          value={matchedTerms.length ? matchedTerms.join(", ") : null}
        />
        <ExplainLine
          label="Relevance pieces"
          value={relevancePieces(relevance, factorHits)}
        />
        <ExplainLine
          label="Trade timing"
          value={
            textValue(correlation.status) ||
            (correlationReasons.length ? correlationReasons.join(", ") : null)
          }
        />
        {"linked_market_count" in article ? (
          <ExplainLine
            label="Search links"
            value={`${fmtInt(article.linked_market_count)} related markets in search`}
          />
        ) : null}
      </span>
    </span>
  );
}

function ExplainMetric({ label, value }: { label: string; value: string }) {
  return (
    <span>
      <span className="block uppercase tracking-wider text-muted-foreground/80">
        {label}
      </span>
      <span className="block truncate font-medium text-foreground">{value}</span>
    </span>
  );
}

function ExplainLine({
  label,
  value,
}: {
  label: string;
  value: string | null;
}) {
  if (!value) return null;
  return (
    <span className="mt-2 block">
      <span className="font-medium text-foreground">{label}: </span>
      {value}
    </span>
  );
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && !!item)
    : [];
}

const FALLBACK_NEWS_GENERIC_TERMS = new Set([
  "above",
  "after",
  "before",
  "below",
  "director",
  "event",
  "leave",
  "leaves",
  "market",
  "price",
  "resolve",
  "trade",
  "trades",
  "trading",
  "will",
  "year",
]);

const FALLBACK_NEWS_ANCHORS = new Set([
  "ai",
  "bnb",
  "btc",
  "cpi",
  "doj",
  "eth",
  "fbi",
  "fed",
  "gdp",
  "mlb",
  "nba",
  "nfl",
  "nhl",
  "nfp",
  "pce",
  "sec",
  "sol",
  "ufc",
  "wti",
  "xrp",
]);

function isRelatedSearchArticle(
  article: SearchNewsResult,
  query: string,
  marketId?: string | null,
  eventId?: string | null,
): boolean {
  if (
    article.linked_markets.some(
      (market) =>
        market.market_id === marketId ||
        (!!eventId && market.event_id === eventId),
    )
  ) {
    return true;
  }

  const terms = fallbackSearchTerms(query);
  if (!terms.length) return false;
  const tokens = new Set(
    `${article.title ?? ""} ${article.summary ?? ""}`
      .toLowerCase()
      .match(/[a-z0-9][a-z0-9_.-]*/g) ?? [],
  );
  const hits = terms.filter((term) => tokens.has(term));
  if (hits.length >= Math.min(2, terms.length)) return true;
  return hits.some((term) => FALLBACK_NEWS_ANCHORS.has(term));
}

function fallbackSearchTerms(query: string): string[] {
  const seen = new Set<string>();
  for (const raw of query.toLowerCase().match(/[a-z0-9][a-z0-9_.-]*/g) ?? []) {
    if (FALLBACK_NEWS_GENERIC_TERMS.has(raw)) continue;
    if (/^\d+$/.test(raw)) continue;
    if (raw.length <= 2 && !FALLBACK_NEWS_ANCHORS.has(raw)) continue;
    seen.add(raw);
  }
  return [...seen].slice(0, 8);
}

function formatExplainScore(value: number | null): string {
  return value == null ? "n/a" : value.toFixed(value >= 1 ? 1 : 2);
}

function formatExplainPercent(value: number | null): string {
  return value == null ? "n/a" : `${Math.round(value * 100)}%`;
}

function relevancePieces(
  relevance: Record<string, unknown>,
  factorHits: Record<string, unknown>,
): string | null {
  const pieces = [
    ["lex", numberValue(relevance.lexical_relevance)],
    ["entity", numberValue(relevance.entity_relevance)],
    ["alias", numberValue(relevance.alias_relevance)],
    ["factor", numberValue(relevance.factor_relevance)],
  ]
    .filter(([, value]) => value != null)
    .map(([label, value]) => `${label} ${Number(value).toFixed(2)}`);
  const factors = Object.keys(factorHits).slice(0, 4);
  if (factors.length) pieces.push(`factors ${factors.join(", ")}`);
  return pieces.length ? pieces.join(" / ") : null;
}

function RelatedNewsSearchList({ articles }: { articles: SearchNewsResult[] }) {
  return (
    <div>
      <div className="px-4 py-2 border-b border-border bg-secondary/20 text-[11px] uppercase tracking-wider text-muted-foreground">
        Search-backed related news
      </div>
      <ul className="divide-y divide-border max-h-[480px] overflow-auto">
        {articles.map((article, i) => (
          <li
            key={`${article.article_id ?? "related"}-${i}`}
            className="px-4 py-3 hover:bg-secondary/30 transition-colors"
          >
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
                {article.source ? <span>{article.source}</span> : null}
                {article.first_seen_at ?? article.published_at ? (
                  <>
                    <span>/</span>
                    <span>{fmtAgo(article.first_seen_at ?? article.published_at)}</span>
                  </>
                ) : null}
                {article.linked_market_count ? (
                  <>
                    <span>/</span>
                    <span>{fmtInt(article.linked_market_count)} linked markets</span>
                  </>
                ) : null}
              </div>
            </a>
            {article.linked_markets.length ? (
              <div className="mt-2 flex flex-wrap gap-1.5">
                <NewsExplainHover article={article} />
                {article.linked_markets.slice(0, 4).map((market) => (
                  <Link
                    key={market.market_id}
                    to={`/markets/${encodeURIComponent(market.market_id)}`}
                    className="rounded border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    {market.market_id}
                  </Link>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
