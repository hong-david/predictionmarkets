/** Types for /api/dashboard/* responses.
 *
 * These mirror the FastAPI response shapes in `app/api/routes/dashboard.py`
 * exactly. Keeping them in one file (rather than co-located with each
 * fetcher) means a backend change shows up as one diff here, not
 * scattered across components.
 */

export type Severity = "high" | "medium" | "low" | string;
export type Prior =
  | "high"
  | "medium_high"
  | "medium"
  | "low"
  | "very_low"
  | "unclassified"
  | string;
export type Confidence = "high" | "medium" | "low" | "unclassified" | string;

export interface SystemStats {
  markets: number;
  markets_status_unknown: number;
  markets_high_prior: number;
  /** Distinct markets with ≥1 `anomalies` row (evidence, not just triage). */
  markets_with_flags: number;
  trades: number;
  snapshots: number;
  book_events: number;
  anomalies: number;
  anomalies_high_severity: number;
}

export interface BreakdownEntry {
  key: string;
  count: number;
}

export interface CategoryPriorEntry {
  category: string;
  prior: string;
  count: number;
}

export interface Breakdown {
  by_category: BreakdownEntry[];
  by_prior: BreakdownEntry[];
  by_confidence: BreakdownEntry[];
  by_layer: BreakdownEntry[];
  category_x_prior: CategoryPriorEntry[];
}

export interface MarketRow {
  market_id: string;
  /** Kalshi `event_ticker` when hydrated; groups date/outcome legs of one event. */
  event_id: string | null;
  title: string;
  subtitle: string | null;
  status: string;
  category: string | null;
  subcategory: string | null;
  manipulability_prior: Prior | null;
  classifier_confidence: Confidence | null;
  classifier_layer: string | null;
  classifier_rule: string | null;
  open_time: string | null;
  close_time: string | null;
  trade_count: number;
  anomaly_count: number;
  last_price: number | null;
  volume_24h: number | null;
  /** Classifier `manipulability_prior` bucket; “unclassified” if unknown. */
  market_priority: string;
  /** 0–100 from stored `anomalies` row mass (not prior). */
  evidence_score: number;
  /** 0–100 combined urgency aligned with the “Alerts first” sort. */
  urgency_score: number;
  /** Deduped snake_case slugs from materialized `reasons[]` on alert rows. */
  reasons: string[];
  /** Number of hydrated contracts sharing the same Kalshi event_ticker. */
  event_market_count: number | null;
}

export interface MarketsList {
  total: number;
  filtered: number;
  limit: number;
  offset: number;
  markets: MarketRow[];
}

/** All contracts under the same Kalshi `event_ticker` (`Market.event_id`). */
export interface EventGroup {
  event_id: string;
  title: string;
  market_count: number;
  markets: MarketRow[];
}

export interface MarketDetail extends MarketRow {
  classifier_tags: string[];
  stats: {
    trade_count: number;
    first_trade_ts: string | null;
    last_trade_ts: string | null;
    min_yes_price: number | null;
    max_yes_price: number | null;
    total_traded_size: number | null;
  };
  latest_snapshot: {
    ts: string | null;
    last_price_dollars: number | null;
    yes_bid_dollars: number | null;
    yes_ask_dollars: number | null;
    volume_24h_fp: number | null;
    open_interest_fp: number | null;
    /** Exchange-reported top-of-book depth / liquidity in dollars, when present. */
    liquidity_dollars: number | null;
  } | null;
}

export interface TradePoint {
  ts: string | null;
  yes_price: number | null;
  no_price: number | null;
  count: number | null;
  taker_side: string | null;
  /** Local trade outlier score 0..10 vs this market’s own recent tape (API field name unchanged). */
  suspicion?: number | null;
  suspicion_reasons?: string[];
  suspicion_features?: Record<string, number | null>;
  /** 0..10 local burst / same-side cluster intensity in a sliding window. */
  cluster_0_10?: number;
}

export interface SuspiciousTrade {
  market_id: string;
  event_id: string | null;
  title: string;
  subtitle: string | null;
  category: string | null;
  manipulability_prior: Prior | null;
  trade_id: string;
  ts: string | null;
  yes_price: number | null;
  no_price: number | null;
  count: number | null;
  taker_side: string | null;
  suspicion: number;
  reasons: string[];
  features: Record<string, number | null>;
}

export interface SuspiciousTradesList {
  count: number;
  sample: number;
  trades: SuspiciousTrade[];
}

export interface TapeCluster {
  burst_score_0_10: number;
  largest_window_count: number;
  window_sec: number;
  dominant_side: string | null;
}

export interface SnapshotPoint {
  ts: string | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  volume_24h: number | null;
  open_interest: number | null;
}

export interface MarketSeries {
  market_id: string;
  /** Rolling burst detector on the same tape as `trades` (30s default window). */
  tape_cluster: TapeCluster;
  trades: TradePoint[];
  snapshots: SnapshotPoint[];
}

export interface AnomalyRow {
  id: number;
  score: number;
  severity: Severity;
  reasons: string[];
  signals: Record<string, unknown>;
  created_at: string | null;
}

export interface MarketAnomalies {
  count: number;
  anomalies: AnomalyRow[];
}

export interface RecentAnomaly {
  id: number;
  market_id: string;
  title: string;
  subtitle: string | null;
  category: string | null;
  manipulability_prior: Prior | null;
  score: number;
  severity: Severity;
  reasons: string[];
  created_at: string | null;
}

export interface RecentAnomaliesList {
  count: number;
  anomalies: RecentAnomaly[];
}

export interface NewsArticle {
  title: string | null;
  url: string | null;
  source: string | null;
  language: string | null;
  published_at: string | null;
  tone: number | string | null;
}

/** Correlates GDELT window with the tape; see `app/services/news_gdelt.py`. */
export interface NewsAnchors {
  align: "default" | "activity" | string;
  last_trade_ts?: string | null;
  last_flag_ts?: string | null;
  focus_ts?: string | null;
  reason?: string;
}

export interface MarketNews {
  market_id: string;
  query: string;
  since: string;
  until: string;
  provider: "gdelt" | "unavailable" | string;
  articles: NewsArticle[];
  anchors?: NewsAnchors;
  error?: string;
}

export interface DashboardOverview {
  stats: SystemStats;
  breakdown: Breakdown;
  top_markets: TopMarketsList;
  recent_anomalies: RecentAnomaliesList;
  suspicious_trades: SuspiciousTradesList;
}

export interface TopMarketsList {
  count: number;
  markets: MarketRow[];
}
