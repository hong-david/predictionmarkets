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
export type MarketScope = "active" | "historical" | "all";

export interface SystemStats {
  market_scope: MarketScope | string;
  markets: number;
  markets_in_scope?: number;
  markets_all: number;
  markets_active: number;
  markets_historical: number;
  markets_status_unknown: number;
  /** Distinct markets with at least one market-level alert row. */
  markets_with_flags: number;
  trades: number;
  snapshots: number;
  book_events: number;
  anomalies: number;
  news_articles: number;
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
  market_lifecycle: "active" | "historical" | "other" | "unknown" | "out_of_scope" | string;
  is_active: boolean;
  trade_count: number;
  trade_dollar_volume: number | null;
  anomaly_count: number;
  last_price: number | null;
  volume_24h: number | null;
  /** Exchange-reported cumulative contract volume since market inception, when present. */
  volume_total: number | null;
  /** Classifier `manipulability_prior` bucket; “unclassified” if unknown. */
  market_priority: string;
  /** 0–100 from stored `anomalies` row mass (not prior). */
  evidence_score: number;
  /** 0-100 combined urgency aligned with the activity-alerts-first sort. */
  urgency_score: number;
  /** Highest durable per-trade flag score seen for this market, if any. */
  top_trade_flag_score?: number | null;
  /** Highest retained news/trade timing score linked to this market, if any. */
  top_news_trade_score?: number | null;
  /** Deduped snake_case slugs from materialized `reasons[]` on alert history rows. */
  reasons: string[];
  /** Number of hydrated contracts sharing the same Kalshi event_ticker. */
  event_market_count: number | null;
  /** Raw retention tier from MarketMetric when available. */
  storage_tier?: string | null;
  /** Auditable raw-retention score from MarketMetric when available. */
  retention_score?: number | null;
}

export interface MarketsList {
  total: number;
  filtered: number;
  counts_exact?: boolean;
  has_more?: boolean;
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
  news_search_query?: string | null;
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
  /** Estimated dollars paid for this print: count times the side price. */
  trade_dollar_amount?: number | null;
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
  /** Estimated dollars paid for this print: count times the side price. */
  trade_dollar_amount?: number | null;
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
  partial?: boolean;
  limit?: number;
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
  event_id?: number;
  article_id?: number;
  title: string | null;
  url: string | null;
  source: string | null;
  language: string | null;
  published_at: string | null;
  first_seen_at?: string | null;
  tone: number | string | null;
  relevance_score?: number;
  pre_news_trade_score?: number;
  status?: string;
  leakage_window_seconds?: number | null;
  direction_label?: string | null;
  direction_confidence?: number | null;
  reasons?: string[];
  best_trade?: Record<string, unknown> | null;
  market_direction?: Record<string, unknown>;
  news_trade_correlation?: Record<string, unknown>;
  relevance_components?: Record<string, unknown>;
  candidate_generation?: Record<string, unknown>;
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
  provider: "stored" | "gdelt" | "unavailable" | string;
  articles: NewsArticle[];
  anchors?: NewsAnchors;
  stored_event_count?: number;
  error?: string;
}

export interface NewsSignal {
  event_id: number;
  market_id: string;
  event_market_id: string | null;
  title: string;
  subtitle: string | null;
  category: string | null;
  manipulability_prior: Prior | null;
  article: NewsArticle;
  article_title: string | null;
  article_url: string | null;
  article_source: string | null;
  first_seen_at: string | null;
  relevance_score: number;
  pre_news_trade_score: number;
  status: string;
  leakage_window_seconds: number | null;
  direction_label: string | null;
  direction_confidence: number | null;
  reasons: string[];
  best_trade: Record<string, unknown> | null;
}

export interface NewsSignalsList {
  count: number;
  min_score: number;
  signals: NewsSignal[];
}

export type PipelineComponentStatus = "healthy" | "stale" | "empty" | "error";
export type PipelineSummaryStatus = "healthy" | "degraded" | "empty" | "error";

export interface PipelineHealthComponent {
  key: string;
  label: string;
  status: PipelineComponentStatus;
  latest_at: string | null;
  age_seconds: number | null;
  count: number | null;
  description: string;
  detail: string;
  heartbeat_at?: string | null;
  last_success_at?: string | null;
  last_error_at?: string | null;
  last_error?: string | null;
  component_type?: string | null;
  source?: string | null;
  run_id?: string | null;
}

export interface PipelineHealth {
  generated_at: string;
  summary: {
    status: PipelineSummaryStatus;
    healthy: number;
    stale: number;
    empty: number;
    error: number;
    total: number;
  };
  components: PipelineHealthComponent[];
}

export interface NewsDiagnostics {
  generated_at: string;
  latest_at: string | null;
  age_seconds: number | null;
  provider_status: string | null;
  fetch_error: string | null;
  summary: {
    articles_stored: number;
    articles_seen_last_run: number;
    articles_upserted_last_run: number;
    article_clusters_seen_last_run: number;
    news_events_linked: number;
    news_events_linked_last_run: number;
    positive_correlations: number;
    profiles_refreshed_last_run: number;
  };
  source_counts: Record<string, unknown>;
  rss_feed_details: Array<{
    source?: string | null;
    key?: string | null;
    label?: string | null;
    count?: number | null;
    error?: string | null;
    source_tier?: string | null;
    authority_tier?: string | null;
    topic_tags?: string[];
  }>;
  source_registry: {
    total?: number;
    enabled?: number;
    available?: number;
    rss_available?: number;
    optional_unavailable?: Array<Record<string, unknown>>;
    sources?: Array<Record<string, unknown>>;
  };
  heartbeats: Record<string, PipelineHealthComponent>;
}

export interface HistoricalSignalQaMarket {
  market_id: string;
  title: string;
  status: string | null;
  category: string | null;
  prior: Prior | null;
  close_time: string | null;
  market_lifecycle: string;
  trade_count: number;
  metric_urgency_score: number;
  qa_label: string;
  flags: {
    count_sample: number;
    before_close_sample: number;
    best_score: number;
    best_severity: string | null;
    best_ts: string | null;
    best_reasons: string[];
  };
  pre_news: {
    count_sample: number;
    before_close_sample: number;
    best_score: number;
    best_article: string | null;
    best_source: string | null;
    first_seen_at: string | null;
    leakage_window_seconds: number | null;
  };
  quote_book_anomalies: {
    count: number;
    high_count: number;
    last_ts: string | null;
  };
}

export interface HistoricalSignalQa {
  generated_at: string;
  limit: number;
  min_flag_score: number;
  category: string | null;
  market_id: string | null;
  markets: HistoricalSignalQaMarket[];
}

export interface SearchMarketResult {
  kind: "market";
  market_pk: number | null;
  market_id: string;
  event_id: string | null;
  title: string | null;
  subtitle: string | null;
  status: string | null;
  category: string | null;
  subcategory: string | null;
  manipulability_prior: Prior | null;
  classifier_confidence: Confidence | null;
  trade_count: number;
  anomaly_count: number;
  market_priority: string | null;
  evidence_score: number;
  urgency_score: number;
  score: number | null;
  url: string;
  match_reason?: string | null;
}

export interface SearchNewsResult {
  kind: "news";
  article_id: number | null;
  title: string | null;
  summary: string | null;
  url: string | null;
  source: string | null;
  source_tier: string | null;
  language: string | null;
  published_at: string | null;
  first_seen_at: string | null;
  score: number | null;
  linked_market_count: number;
  linked_markets: SearchMarketResult[];
  pre_news_trade_score?: number;
}

export interface SearchSuggestion {
  kind: "market" | "news";
  label: string | null;
  value: string | null;
  url: string | null;
}

export interface SearchResponse {
  query: string;
  scope: "all" | "markets" | "news" | "empty" | string;
  provider: "opensearch" | "postgres" | "empty" | string;
  fallback_reason?: string | null;
  took_ms: number;
  markets: SearchMarketResult[];
  news: SearchNewsResult[];
  suggestions: SearchSuggestion[];
}

export interface DashboardOverview {
  stats: SystemStats;
  breakdown: Breakdown;
  top_markets: TopMarketsList;
  recent_anomalies: RecentAnomaliesList;
  suspicious_trades: SuspiciousTradesList;
  news_signals?: NewsSignalsList;
}

export interface TopMarketsList {
  count: number;
  markets: MarketRow[];
}
