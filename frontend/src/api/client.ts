import type {
  Breakdown,
  EventGroup,
  MarketAnomalies,
  MarketDetail,
  MarketNews,
  MarketSeries,
  MarketsList,
  NewsSignalsList,
  DashboardOverview,
  RecentAnomaliesList,
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
  /** Single round-trip for the home page (stats + charts + two side lists). */
  overview: (params?: { top?: number; anomalies?: number }) =>
    get<DashboardOverview>("/api/dashboard/overview", {
      top: params?.top,
      anomalies: params?.anomalies,
    }),
  stats: () => get<SystemStats>("/api/dashboard/stats"),
  breakdown: () => get<Breakdown>("/api/dashboard/breakdown"),
  markets: (params: {
    q?: string;
    category?: string;
    prior?: string;
    confidence?: string;
    status?: string;
    include_unhydrated?: boolean;
    sort?:
      | "trades_desc"
      | "trades_asc"
      | "priority"
      | "surveillance_urgency"
      | "recent"
      | "title"
      | "anomalies"
      | string;
    limit?: number;
    offset?: number;
  }) => get<MarketsList>("/api/dashboard/markets", params),
  /** Kalshi `event_ticker` = `Market.event_id`: all leg contracts in one event. */
  eventGroup: (eventId: string) =>
    get<EventGroup>(`/api/dashboard/events/${encodeURIComponent(eventId)}`),
  marketDetail: (id: string) =>
    get<MarketDetail>(`/api/dashboard/markets/${encodeURIComponent(id)}`),
  marketSeries: (id: string, limit = 2000) =>
    get<MarketSeries>(`/api/dashboard/markets/${encodeURIComponent(id)}/series`, {
      limit,
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
  topMarkets: (limit = 15) =>
    get<TopMarketsList>("/api/dashboard/top-markets", { limit }),
  recentAnomalies: (limit = 20, severity?: string) =>
    get<RecentAnomaliesList>("/api/dashboard/anomalies", { limit, severity }),
  suspiciousTrades: (limit = 25) =>
    get<SuspiciousTradesList>("/api/dashboard/suspicious-trades", { limit }),
  newsSignals: (limit = 25, minScore = 4) =>
    get<NewsSignalsList>("/api/dashboard/news-signals", {
      limit,
      min_score: minScore,
    }),
};
