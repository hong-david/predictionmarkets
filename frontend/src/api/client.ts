import type {
  ArchiveStatus,
  Breakdown,
  EventGroup,
  HistoricalSignalQa,
  MarketAnomalies,
  MarketDetail,
  MarketNews,
  MarketSeries,
  MarketsList,
  NewsDiagnostics,
  NewsSignalsList,
  PipelineHealth,
  DashboardOverview,
  MarketScope,
  RecentAnomaliesList,
  SearchResponse,
  SuspiciousTradesList,
  SystemStats,
  TopMarketsList,
} from "./types";

/**
 * Tiny fetch wrapper. Throws on non-2xx so TanStack Query treats them
 * as errors and renders error states instead of pretending we got data.
 */
async function get<T>(
  path: string,
  params?: Record<string, string | number | boolean | undefined | null>,
): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v == null || v === "") continue;
      url.searchParams.set(k, String(v));
    }
  }
  const r = await fetch(url.toString(), { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let detail = "";
    try {
      const body = await r.json();
      detail = body?.detail ?? JSON.stringify(body);
    } catch {
      detail = await r.text().catch(() => "");
    }
    throw new Error(`${r.status} ${r.statusText}: ${detail || path}`);
  }
  return (await r.json()) as T;
}

export const api = {
  archiveStatus: () => get<ArchiveStatus>("/api/archive-status"),
  /** Bundled overview: stats, breakdown, top markets, alerts, trade flags, news signals. */
  overview: (params?: { top?: number; anomalies?: number; market_scope?: MarketScope }) =>
    get<DashboardOverview>("/api/dashboard/overview", {
      top: params?.top,
      anomalies: params?.anomalies,
      market_scope: params?.market_scope,
    }),
  stats: (params?: { market_scope?: MarketScope }) =>
    get<SystemStats>("/api/dashboard/stats", {
      market_scope: params?.market_scope,
    }),
  breakdown: (params?: { market_scope?: MarketScope }) =>
    get<Breakdown>("/api/dashboard/breakdown", {
      market_scope: params?.market_scope,
    }),
  markets: (params: {
    q?: string;
    category?: string;
    prior?: string;
    confidence?: string;
    market_scope?: MarketScope;
    status?: string;
    include_unhydrated?: boolean;
    sort?:
      | "trades_desc"
      | "trades_asc"
      | "priority"
      | "surveillance_urgency"
      | "top_trade_flag"
      | "news_linked_trade_flag"
      | "recent"
      | "title"
      | "anomalies"
      | string;
    limit?: number;
    offset?: number;
    include_counts?: boolean;
  }) => get<MarketsList>("/api/dashboard/markets", params),
  /** Kalshi `event_ticker` = `Market.event_id`: all leg contracts in one event. */
  eventGroup: (eventId: string) =>
    get<EventGroup>(`/api/dashboard/events/${encodeURIComponent(eventId)}`),
  marketDetail: (id: string) =>
    get<MarketDetail>(`/api/dashboard/markets/${encodeURIComponent(id)}`),
  marketSeries: (
    id: string,
    limit = 600,
    opts?: { since?: string | null; includeContext?: boolean },
  ) =>
    get<MarketSeries>(`/api/dashboard/markets/${encodeURIComponent(id)}/series`, {
      limit,
      since: opts?.since,
      include_context: opts?.includeContext,
    }),
  marketAnomalies: (id: string, limit = 50) =>
    get<MarketAnomalies>(
      `/api/dashboard/markets/${encodeURIComponent(id)}/anomalies`,
      { limit },
    ),
  marketNews: (
    id: string,
    limit = 10,
    opts?: { align?: "default" | "activity" },
  ) =>
    get<MarketNews>(`/api/dashboard/markets/${encodeURIComponent(id)}/news`, {
      limit,
      align: opts?.align,
    }),
  topMarkets: (limit = 15, marketScope?: MarketScope) =>
    get<TopMarketsList>("/api/dashboard/top-markets", {
      limit,
      market_scope: marketScope,
    }),
  recentAnomalies: (limit = 20, severity?: string, marketScope?: MarketScope) =>
    get<RecentAnomaliesList>("/api/dashboard/anomalies", {
      limit,
      severity,
      market_scope: marketScope,
    }),
  suspiciousTrades: (limit = 25, marketScope?: MarketScope) =>
    get<SuspiciousTradesList>("/api/dashboard/suspicious-trades", {
      limit,
      market_scope: marketScope,
    }),
  newsSignals: (limit = 25, minScore = 4, marketScope?: MarketScope) =>
    get<NewsSignalsList>("/api/dashboard/news-signals", {
      limit,
      min_score: minScore,
      market_scope: marketScope,
    }),
  newsDiagnostics: () => get<NewsDiagnostics>("/api/dashboard/news-diagnostics"),
  historicalSignalQa: (params?: {
    limit?: number;
    min_flag_score?: number;
    category?: string;
    market_id?: string;
  }) => get<HistoricalSignalQa>("/api/dashboard/historical-signal-qa", params),
  pipelineHealth: () => get<PipelineHealth>("/api/dashboard/pipeline-health"),
  search: (q: string, scope: "all" | "markets" | "news" = "all", limit = 10) =>
    get<SearchResponse>("/api/dashboard/search", { q, scope, limit }),
};
