import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Layers } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { api } from "@/api/client";
import { Badge, priorVariant } from "@/components/Badge";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { EmptyState, Skeleton } from "@/components/StatusBits";
import { priorShort } from "@/lib/labels";
import { fmtInt, fmtPrice } from "@/lib/utils";

/**
 * One Kalshi `event_ticker` (`Market.event_id`): every date/outcome leg that
 * shares the same event-level question. Trades and book history stay
 * per-market; this view is for navigation and comparison only.
 */
export default function EventGroupPage() {
  const { eventId = "" } = useParams<{ eventId: string }>();
  const decoded = eventId ? decodeURIComponent(eventId) : "";

  const q = useQuery({
    queryKey: ["eventGroup", decoded],
    queryFn: () => api.eventGroup(decoded),
    enabled: !!decoded,
    refetchInterval: 15_000,
  });

  if (q.isError) {
    return (
      <div className="space-y-4">
        <Link
          to="/markets"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> All markets
        </Link>
        <Card>
          <CardBody>
            <EmptyState>
              {String(q.error)}
            </EmptyState>
            <p className="text-sm text-muted-foreground mt-2">
              The API groups by the exchange{" "}
              <code className="font-mono text-xs">event_ticker</code> stored as{" "}
              <code className="font-mono text-xs">event_id</code> after REST
              hydration. If this event has no rows, try visiting a market
              detail page that belongs to the event first.
            </p>
          </CardBody>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Link
        to="/markets"
        className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> All markets
      </Link>

      <Card>
        <CardBody>
          {q.isPending ? (
            <>
              <Skeleton className="h-7 w-3/4" />
              <Skeleton className="h-4 w-1/2 mt-2" />
            </>
          ) : q.data ? (
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-muted-foreground text-xs mb-1">
                  <Layers className="h-3.5 w-3.5" />
                  <span>Kalshi event (all legs)</span>
                </div>
                <h1 className="text-xl font-semibold tracking-tight">
                  {q.data.title}
                </h1>
                <code className="mt-1.5 block font-mono text-[11px] text-muted-foreground break-all">
                  {q.data.event_id}
                </code>
                <p className="mt-2 text-sm text-muted-foreground max-w-2xl">
                  {q.data.market_count} contract{q.data.market_count === 1 ? "" : "s"} under
                  this event. Open a leg for charts and tape. Prices are not
                  merged — each row is a separate tradable market.
                </p>
              </div>
            </div>
          ) : null}
        </CardBody>
      </Card>

      <Card className="overflow-hidden">
        <CardHeader
          title="Contracts"
          subtitle="Sorted by close time, then ticker. Subtitle is usually the leg label (e.g. deadline date)."
        />
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border bg-card/40">
                <th className="text-left px-4 py-2.5 font-medium">Leg / subtitle</th>
                <th className="text-left px-3 py-2.5 font-medium">Market id</th>
                <th className="text-left px-3 py-2.5 font-medium">Priority</th>
                <th className="text-right px-3 py-2.5 font-medium">Last</th>
                <th className="text-right px-3 py-2.5 font-medium">Trades</th>
                <th className="text-right px-3 py-2.5 font-medium">Rule rows</th>
              </tr>
            </thead>
            <tbody>
              {q.isPending ? (
                <tr>
                  <td colSpan={6} className="px-4 py-6">
                    <Skeleton className="h-24" />
                  </td>
                </tr>
              ) : q.data && q.data.markets.length > 0 ? (
                q.data.markets.map((m) => (
                  <tr
                    key={m.market_id}
                    className="border-b border-border last:border-0 hover:bg-secondary/30 transition-colors"
                  >
                    <td className="px-4 py-2.5 align-top max-w-md">
                      <Link
                        to={`/markets/${encodeURIComponent(m.market_id)}`}
                        className="block text-sm font-medium text-primary hover:underline"
                      >
                        {m.subtitle || m.title}
                      </Link>
                      {m.subtitle ? (
                        <div className="text-xs text-muted-foreground mt-0.5 line-clamp-2">
                          {m.title}
                        </div>
                      ) : null}
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      <Link
                        to={`/markets/${encodeURIComponent(m.market_id)}`}
                        className="font-mono text-[11px] text-muted-foreground hover:text-primary"
                      >
                        {m.market_id}
                      </Link>
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      {m.manipulability_prior ? (
                        <Badge variant={priorVariant(m.manipulability_prior)}>
                          {priorShort(m.manipulability_prior)}
                        </Badge>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-sm">
                      {fmtPrice(m.last_price)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-sm">
                      {fmtInt(m.trade_count)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-sm text-muted-foreground">
                      {fmtInt(m.anomaly_count)}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6}>
                    <EmptyState>No contracts in this event.</EmptyState>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
