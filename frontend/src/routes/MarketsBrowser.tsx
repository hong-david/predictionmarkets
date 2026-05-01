import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { MarketScope } from "@/api/types";
import { Badge, priorVariant } from "@/components/Badge";
import { Card } from "@/components/Card";
import { MarketCell } from "@/components/MarketCell";
import { EmptyState, Skeleton } from "@/components/StatusBits";
import { categoryDisplay, priorHelp, priorShort } from "@/lib/labels";
import { cn, fmtInt, fmtPrice } from "@/lib/utils";

const SORT_OPTIONS = [
  { value: "news_linked_trade_flag", label: "News/trade signals first" },
  { value: "top_trade_flag", label: "Highest trade flag" },
  {
    value: "surveillance_urgency",
    label: "Activity alerts first",
  },
  { value: "priority", label: "Watch priority, then trades" },
  { value: "trades_desc", label: "Most trades" },
  { value: "trades_asc", label: "Fewest trades" },
  { value: "anomalies", label: "Most alert history" },
  { value: "recent", label: "Newest in database" },
  { value: "title", label: "Title A–Z" },
];

const PAGE_SIZE = 50;

function parseMarketScope(value: string | null): MarketScope {
  return value === "historical" || value === "all" ? value : "active";
}

/**
 * Markets browser. Server-side filtering, sorting, and pagination keep
 * the wire payload small (one page = 50 rows) and let the URL drive
 * everything: a copy-pasted link reproduces the same filtered view.
 *
 * URL params (all optional):
 *   ?q=...      - search title/subtitle/market_id
 *   ?category   - exact category match
 *   ?prior      - exact manipulability_prior match
 *   ?sort       - one of SORT_OPTIONS
 *   ?offset     - pagination offset
 */
export default function MarketsBrowserPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [searchInput, setSearchInput] = useState(searchParams.get("q") ?? "");

  const params = {
    q: searchParams.get("q") || undefined,
    category: searchParams.get("category") || undefined,
    prior: searchParams.get("prior") || undefined,
    confidence: searchParams.get("confidence") || undefined,
    market_scope: parseMarketScope(searchParams.get("market_scope")),
    sort: searchParams.get("sort") || "news_linked_trade_flag",
    offset: Number(searchParams.get("offset") || 0),
    limit: PAGE_SIZE,
  };

  const breakdown = useQuery({
    queryKey: ["breakdown", params.market_scope],
    queryFn: () => api.breakdown({ market_scope: params.market_scope }),
    staleTime: 60_000,
  });

  const list = useQuery({
    queryKey: ["markets", params],
    queryFn: () => api.markets(params),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (value == null || value === "") next.delete(key);
    else next.set(key, value);
    if (key !== "offset") next.delete("offset");
    setSearchParams(next, { replace: false });
  };

  const submitSearch = () => setParam("q", searchInput.trim() || null);

  const activeFilterCount = useMemo(() => {
    let n = 0;
    if (params.q) n++;
    if (params.category) n++;
    if (params.prior) n++;
    if (params.confidence) n++;
    if (params.market_scope !== "active") n++;
    return n;
  }, [
    params.q,
    params.category,
    params.prior,
    params.confidence,
    params.market_scope,
  ]);

  const totalLabel = list.data
    ? list.data.counts_exact === false
      ? `showing ${fmtInt(list.data.markets.length)} markets from this page`
      : activeFilterCount > 0
      ? `${fmtInt(list.data.filtered)} of ${fmtInt(list.data.total)} markets`
      : `${fmtInt(list.data.total)} markets`
    : "—";

  const page = Math.floor(params.offset / PAGE_SIZE) + 1;
  const totalPages = list.data && list.data.counts_exact !== false
    ? Math.max(1, Math.ceil(list.data.filtered / PAGE_SIZE))
    : 1;

  return (
    <div className="space-y-4">
      <section className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Markets</h1>
          <p className="text-sm text-muted-foreground">{totalLabel}</p>
        </div>
        <div className="flex items-center gap-2">
          <SearchBox
            value={searchInput}
            onChange={setSearchInput}
            onSubmit={submitSearch}
          />
          <SortSelect
            value={params.sort}
            onChange={(v) => setParam("sort", v)}
          />
        </div>
      </section>

      <FilterChips
        params={params}
        breakdown={breakdown.data}
        onChange={setParam}
      />

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border bg-card/40">
                <th className="text-left px-4 py-2.5 font-medium">Market</th>
                <th
                  className="text-left px-3 py-2.5 font-medium"
                  title="Live/open versus retained historical markets."
                >
                  State
                </th>
                <th
                  className="text-left px-3 py-2.5 font-medium"
                  title={priorHelp()}
                >
                  Watch priority
                </th>
                <th
                  className="text-left px-3 py-2.5 font-medium"
                  title="How sure the pipeline was about the category/priority tags"
                >
                  Classifier
                </th>
                <th
                  className="text-right px-3 py-2.5 font-medium"
                  title="Latest stored quote or trade price from the newest snapshot/projection."
                >
                  Last
                </th>
                <th className="text-right px-3 py-2.5 font-medium">Trades</th>
                <th
                  className="text-right px-3 py-2.5 font-medium"
                  title="Saved market-level quote/book alert history. A market can have many alert rows without every row being a separate trade."
                >
                  Alert history
                </th>
                <th
                  className="text-right px-3 py-2.5 font-medium w-28"
                  title="Kalshi event: all date/outcome legs that share the same event_ticker"
                >
                  Event contracts
                </th>
              </tr>
            </thead>
            <tbody>
              {list.isPending ? (
                <SkeletonRows />
              ) : list.isError ? (
                <tr>
                  <td colSpan={8}>
                    <EmptyState>Failed to load markets: {String(list.error)}</EmptyState>
                  </td>
                </tr>
              ) : list.data && list.data.markets.length > 0 ? (
                list.data.markets.map((m) => (
                  <tr
                    key={m.market_id}
                    className="border-b border-border last:border-0 hover:bg-secondary/30 transition-colors"
                  >
                    <td className="px-4 py-2.5">
                      <MarketCell market={m} showCategory />
                    </td>
                    <td className="px-3 py-2.5 align-top">
                      <StateBadge lifecycle={m.market_lifecycle} status={m.status} />
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
                    <td className="px-3 py-2.5 align-top">
                      <span className="text-xs text-muted-foreground capitalize">
                        {m.classifier_confidence ?? "—"}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right num text-sm align-top">
                      {fmtPrice(m.last_price)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-sm align-top">
                      {fmtInt(m.trade_count)}
                    </td>
                    <td className="px-3 py-2.5 text-right num text-sm align-top">
                      {m.anomaly_count > 0 ? (
                        <span className="text-[hsl(var(--severity-medium))] font-semibold">
                          {fmtInt(m.anomaly_count)}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">0</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-right align-top">
                      {m.event_id ? (
                        <Link
                          to={`/events/${encodeURIComponent(m.event_id)}`}
                          className="text-xs text-primary hover:underline"
                          title={m.event_id}
                        >
                          {m.event_market_count && m.event_market_count > 1
                            ? `${fmtInt(m.event_market_count)} contracts`
                            : "1 contract"}
                        </Link>
                      ) : (
                        <span className="text-xs text-muted-foreground">single</span>
                      )}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={8}>
                    <EmptyState>No markets match these filters.</EmptyState>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {list.data && list.data.filtered > 0 ? (
          <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-t border-border bg-card/40 text-xs text-muted-foreground">
            <div>
              {list.data.counts_exact === false
                ? `Page ${page} · showing ${list.data.markets.length}`
                : `Page ${page} of ${fmtInt(totalPages)} · showing ${list.data.markets.length} of ${fmtInt(list.data.filtered)}`}
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() =>
                  setParam(
                    "offset",
                    String(Math.max(0, params.offset - PAGE_SIZE)),
                  )
                }
                disabled={params.offset === 0}
                className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronLeft className="h-3.5 w-3.5" /> Prev
              </button>
              <button
                onClick={() =>
                  setParam("offset", String(params.offset + PAGE_SIZE))
                }
                disabled={
                  list.data.counts_exact === false
                    ? !list.data.has_more
                    : params.offset + PAGE_SIZE >= list.data.filtered
                }
                className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Next <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  );
}

function SearchBox({
  value,
  onChange,
  onSubmit,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="relative">
      <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSubmit();
        }}
        onBlur={onSubmit}
        placeholder="Search title, subtitle, ticker…"
        className="w-[260px] rounded-md border border-input bg-card pl-8 pr-3 py-1.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
      />
    </div>
  );
}

function SortSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="appearance-none rounded-md border border-input bg-card pl-3 pr-8 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
      >
        {SORT_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            Sort: {o.label}
          </option>
        ))}
      </select>
      <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
    </div>
  );
}

function StateBadge({
  lifecycle,
  status,
}: {
  lifecycle: string | null | undefined;
  status: string | null | undefined;
}) {
  const state = lifecycle || "other";
  const label =
    state === "active" ? "active" : state === "historical" ? "historical" : state;
  const title =
    state === "active"
      ? "This market is open/active and has not passed its close time."
      : state === "historical"
        ? "This market is closed, finalized, settled, or past its close time."
        : `Exchange status: ${status ?? "unknown"}`;
  return (
    <span
      title={title}
      className={cn(
        "inline-flex rounded border px-1.5 py-0.5 text-[11px] uppercase tracking-wider",
        state === "active"
          ? "border-[hsl(var(--severity-low))]/50 text-[hsl(var(--severity-low))]"
          : state === "historical"
            ? "border-border text-muted-foreground"
            : "border-border text-muted-foreground",
      )}
    >
      {label}
    </span>
  );
}

function FilterChips({
  params,
  breakdown,
  onChange,
}: {
  params: {
    q?: string;
    category?: string;
    prior?: string;
    confidence?: string;
    market_scope: MarketScope;
  };
  breakdown:
    | {
        by_category: { key: string; count: number }[];
        by_prior: { key: string; count: number }[];
        by_confidence: { key: string; count: number }[];
        category_x_prior: { category: string; prior: string; count: number }[];
      }
    | undefined;
  onChange: (key: string, value: string | null) => void;
}) {
  const chip = (active: boolean, label: string, onClick: () => void, count?: number) => (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition-colors",
        active
          ? "border-primary bg-primary/15 text-primary"
          : "border-border bg-card text-muted-foreground hover:text-foreground hover:border-muted",
      )}
    >
      {label}
      {count != null ? (
        <span className="num text-[10px] opacity-70">{count.toLocaleString()}</span>
      ) : null}
      {active ? <X className="h-3 w-3" /> : null}
    </button>
  );

  const crossCount = (category: string, prior: string) =>
    breakdown?.category_x_prior.find(
      (row) => row.category === category && row.prior === prior,
    )?.count ?? 0;

  const priorityCount = (prior: string, fallback: number) =>
    params.category ? crossCount(params.category, prior) : fallback;

  const topicCount = (category: string, fallback: number) =>
    params.prior ? crossCount(category, params.prior) : fallback;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground mr-1">
          State
        </span>
        {[
          ["active", "Active/open"],
          ["historical", "Historical"],
          ["all", "All retained"],
        ].map(([value, label]) =>
          chip(
            params.market_scope === value,
            label,
            () => onChange("market_scope", value === "active" ? null : value),
          ),
        )}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className="text-[11px] uppercase tracking-wider text-muted-foreground mr-1"
          title={priorHelp()}
        >
          Priority
        </span>
        {(breakdown?.by_prior ?? [])
          .filter((b) => b.key !== "unclassified")
          .map((b) =>
            chip(
              params.prior === b.key,
              priorShort(b.key),
              () => onChange("prior", params.prior === b.key ? null : b.key),
              priorityCount(b.key, b.count),
            ),
          )}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground mr-1">
          Topic
        </span>
        {(breakdown?.by_category ?? [])
          .filter((b) => b.key !== "unclassified")
          .slice(0, 12)
          .map((b) =>
            chip(
              params.category === b.key,
              categoryDisplay(b.key),
              () => onChange("category", params.category === b.key ? null : b.key),
              topicCount(b.key, b.count),
            ),
          )}
      </div>
    </div>
  );
}

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 8 }).map((_, i) => (
        <tr key={i} className="border-b border-border last:border-0">
          <td className="px-4 py-3">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2 mt-1.5" />
          </td>
          <td className="px-3 py-3">
            <Skeleton className="h-4 w-12" />
          </td>
          <td className="px-3 py-3">
            <Skeleton className="h-4 w-16" />
          </td>
          <td className="px-3 py-3">
            <Skeleton className="h-4 w-12" />
          </td>
          <td className="px-3 py-3">
            <Skeleton className="h-4 w-10 ml-auto" />
          </td>
          <td className="px-3 py-3">
            <Skeleton className="h-4 w-10 ml-auto" />
          </td>
          <td className="px-3 py-3">
            <Skeleton className="h-4 w-10 ml-auto" />
          </td>
          <td className="px-2 py-3">
            <Skeleton className="h-4 w-8 mx-auto" />
          </td>
        </tr>
      ))}
    </>
  );
}
