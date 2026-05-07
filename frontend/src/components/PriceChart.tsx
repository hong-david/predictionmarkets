import {
  ColorType,
  HistogramData,
  IChartApi,
  ISeriesApi,
  LineData,
  LineStyle,
  LineType,
  SeriesMarker,
  Time,
  UTCTimestamp,
  createChart,
  isBusinessDay,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

import type {
  AnomalyRow,
  MarketSeries,
  TradePoint,
} from "@/api/types";
import { fmtTimeEastern, fmtTimeUtc } from "@/lib/utils";

export interface ChartNewsEvent {
  ts: string | null;
  title: string | null;
  source?: string | null;
  direction_label?: string | null;
  score?: number | null;
}

const MARKET_ALERT_MARKER_MIN_SCORE = 5.0;
const MAX_ALERT_MARKERS = 3;
const MAX_TRADE_OUTLIER_MARKERS = 3;
const DISPLAY_SERIES_TARGET_MAX_POINTS = 2200;
const DISPLAY_BUCKET_INTERVALS_SEC = [60, 300, 900, 3600, 14400, 86400];

type MarkerLabel = {
  time: UTCTimestamp;
  text: string;
  source: "alert" | "trade" | "selected";
};

type DisplayObservation = {
  unix: number;
  value: number;
  sourceRank: number;
};

type DisplaySeries = {
  points: LineData[];
  bucketSeconds: number;
  start: number;
  end: number;
};

/**
 * Price + volume chart, with anomaly markers overlaid on the price
 * series.  Uses TradingView's `lightweight-charts` — same library that
 * powers Polymarket / Kalshi's own price views — so it handles
 * crosshairs, time-axis zoom, and pan natively.
 *
 * Layout:
 *   - Top pane (~80% height): yes_price area/curve + anomaly markers
 *   - Bottom pane (~20%):     trade-volume histogram
 *
 * The two series share an x-axis; lightweight-charts handles
 * synchronisation when you pan or zoom.
 *
 * **Temporary display price:** the main line prefers retained trades and
 * explicit last-price snapshots, then quote midpoint when price data is sparse.
 * Points are projected onto a uniform time grid and interpolated so calendar
 * time stays visually linear until canonical chart history exists.
 *
 * **Volume bars:** per-trade *contract size* (that print’s `count`), colored by
 * taker side. Bar height is **not** the same quantity as the snapshot
 * *cumulative* `volume_fp` the anomaly rules use (see market detail copy).
 *
 * **Markers:** not trades — a stored *materialized* score row, snapped to the
 * nearest trade time for a y position. “none” / 0 are skipped. Multiple
 * materializations can target the same second; we show one marker per time
 * (highest total score) so the chart is readable.
 */
export function PriceChart({
  series,
  anomalies,
  tradeOutliers,
  highlightTs,
  newsEvents,
}: {
  series: MarketSeries | undefined;
  anomalies: AnomalyRow[] | undefined;
  tradeOutliers?: TradePoint[];
  highlightTs?: string | null;
  newsEvents?: ChartNewsEvent[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceRef = useRef<ISeriesApi<"Line"> | null>(null);
  const bidRef = useRef<ISeriesApi<"Line"> | null>(null);
  const askRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lineTimesRef = useRef<Time[]>([]);
  const newsEventsRef = useRef<ChartNewsEvent[]>([]);
  const markerLabelsRef = useRef<MarkerLabel[]>([]);
  const yRangeRef = useRef<{ minValue: number; maxValue: number }>({
    minValue: 0,
    maxValue: 1,
  });

  // Mount the chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "rgba(160, 168, 180, 1)",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.04)" },
        horzLines: { color: "rgba(255, 255, 255, 0.04)" },
      },
      rightPriceScale: {
        borderColor: "rgba(255, 255, 255, 0.08)",
        scaleMargins: { top: 0.1, bottom: 0.25 },
      },
      timeScale: {
        borderColor: "rgba(255, 255, 255, 0.08)",
        timeVisible: true,
        // Many prints share the same wall-clock minute; show seconds or labels repeat.
        secondsVisible: true,
      },
      crosshair: {
        mode: 1,
        vertLine: { style: LineStyle.Solid, width: 1, color: "rgba(78, 161, 255, 0.5)" },
        horzLine: { style: LineStyle.Solid, width: 1, color: "rgba(78, 161, 255, 0.5)" },
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
      localization: {
        locale: typeof navigator !== "undefined" ? navigator.language : "en-US",
        timeFormatter: (t: Time) => formatLocalChartTime(t),
        dateFormat: "dd MMM 'yy",
      },
    });
    chartRef.current = chart;

    const price = chart.addLineSeries({
      lineType: LineType.Curved,
      color: "rgba(120, 185, 255, 0.95)",
      lineWidth: 2,
      lineStyle: LineStyle.Solid,
      priceLineVisible: false,
      lastValueVisible: true,
      priceFormat: { type: "price", precision: 3, minMove: 0.001 },
      crosshairMarkerVisible: true,
      crosshairMarkerBorderColor: "rgba(150, 200, 255, 0.9)",
      crosshairMarkerBackgroundColor: "hsl(220 12% 9%)",
      autoscaleInfoProvider: () => ({
        priceRange: yRangeRef.current,
      }),
    });
    priceRef.current = price;

    const bid = chart.addLineSeries({
      lineType: LineType.WithSteps,
      color: "rgba(46, 204, 113, 0.5)",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "price", precision: 3, minMove: 0.001 },
      crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => ({
        priceRange: yRangeRef.current,
      }),
    });
    bidRef.current = bid;

    const ask = chart.addLineSeries({
      lineType: LineType.WithSteps,
      color: "rgba(231, 76, 60, 0.5)",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "price", precision: 3, minMove: 0.001 },
      crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => ({
        priceRange: yRangeRef.current,
      }),
    });
    askRef.current = ask;

    const volume = chart.addHistogramSeries({
      color: "rgba(78, 161, 255, 0.35)",
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });
    volumeRef.current = volume;

    const redrawNewsLines = () => {
      renderOverlayAnnotations(
        chart,
        overlayRef.current,
        lineTimesRef.current,
        newsEventsRef.current,
        markerLabelsRef.current,
      );
    };
    chart.timeScale().subscribeVisibleTimeRangeChange(redrawNewsLines);

    return () => {
      chart.timeScale().unsubscribeVisibleTimeRangeChange(redrawNewsLines);
      chart.remove();
      chartRef.current = null;
      priceRef.current = null;
      bidRef.current = null;
      askRef.current = null;
      volumeRef.current = null;
    };
  }, []);

  // Push data updates whenever series changes.
  useEffect(() => {
    if (!priceRef.current || !volumeRef.current) return;
    if (!series) {
      priceRef.current.setData([]);
      bidRef.current?.setData([]);
      askRef.current?.setData([]);
      volumeRef.current.setData([]);
      lineTimesRef.current = [];
      newsEventsRef.current = [];
      markerLabelsRef.current = [];
      clearOverlay(overlayRef.current);
      return;
    }

    const display = buildDisplayPriceSeries(series);
    const linePoints = display.points;
    const bidPoints = buildUniformQuoteSeries(
      quoteObservations(series, "bid"),
      display,
    );
    const askPoints = buildUniformQuoteSeries(
      quoteObservations(series, "ask"),
      display,
    );
    const volumePoints = buildBucketedVolumePoints(series, display);

    const allLinePoints = [...linePoints, ...bidPoints, ...askPoints];
    yRangeRef.current = dynamicProbabilityRange(allLinePoints.map((p) => p.value));
    lineTimesRef.current = uniqueSortedTimes(allLinePoints);
    newsEventsRef.current = newsEvents ?? [];
    priceRef.current.setData(linePoints);
    bidRef.current?.setData(bidPoints);
    askRef.current?.setData(askPoints);
    volumeRef.current.setData(volumePoints);
    priceRef.current.setMarkers([]);
    bidRef.current?.setMarkers([]);
    askRef.current?.setMarkers([]);

    // Anomaly markers: snap to the nearest trade timestamp on the
    // existing time series so the marker has a y-coordinate. If we have
    // no trades but do have anomalies, the time scale won't include
    // their x-position and lightweight-charts will silently drop them.
    const markerSeries =
      linePoints.length > 0
        ? priceRef.current
        : bidPoints.length > 0
          ? bidRef.current
          : askRef.current;
    if (markerSeries && lineTimesRef.current.length) {
      const sortedTimes = lineTimesRef.current;
      const chartMarkers: Array<{
        time: UTCTimestamp;
        color: string;
        text: string;
        rank: number;
        source: "alert" | "trade" | "selected";
      }> = [];

      const topAlerts = [...(anomalies ?? [])]
        .filter(
          (a) =>
            !!a.created_at &&
            a.score >= MARKET_ALERT_MARKER_MIN_SCORE &&
            a.severity !== "none" &&
            a.severity !== "low",
        )
        .sort(
          (a, b) =>
            b.score - a.score ||
            String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")),
        )
        .slice(0, MAX_ALERT_MARKERS);

      for (const a of topAlerts) {
        if (!a.created_at) continue;
        const target = Math.floor(new Date(a.created_at).getTime() / 1000);
        const t = nearestTime(sortedTimes, target);
        if (t == null) continue;
        const hint =
          a.reasons && a.reasons.length > 0
            ? a.reasons[0].slice(0, 28)
            : "alert";
        chartMarkers.push({
          time: t,
          color: "rgba(245, 158, 11, 0.95)",
          text: `${hint} · ${a.score.toFixed(1)}`,
          rank: a.score,
          source: "alert",
        });
      }

      const topTrades = [...(tradeOutliers ?? [])]
        .filter((t) => !!t.ts && t.suspicion != null && t.suspicion > 0)
        .sort(
          (a, b) =>
            (b.suspicion ?? 0) - (a.suspicion ?? 0) ||
            String(b.ts ?? "").localeCompare(String(a.ts ?? "")),
        )
        .slice(0, MAX_TRADE_OUTLIER_MARKERS);
      for (const t of topTrades) {
        if (!t.ts) continue;
        const target = Math.floor(new Date(t.ts).getTime() / 1000);
        const snapped = nearestTime(sortedTimes, target);
        if (snapped == null) continue;
        const dollars = t.trade_dollar_amount ?? estimateTradeDollars(t);
        const dollarsText = dollars == null ? "$?" : formatAbbrevDollars(dollars);
        chartMarkers.push({
          time: snapped,
          color: "rgba(231, 76, 60, 0.98)",
          text: `outlier ${dollarsText} · ${(t.suspicion ?? 0).toFixed(1)}`,
          rank: t.suspicion ?? 0,
          source: "trade",
        });
      }
      if (highlightTs) {
        const target = Math.floor(new Date(highlightTs).getTime() / 1000);
        if (Number.isFinite(target)) {
          const t = nearestTime(sortedTimes, target);
          if (t != null) {
            chartMarkers.push({
              time: t,
              color: "rgba(78, 161, 255, 1)",
              text: "selected unusual print",
              rank: 999,
              source: "selected",
            });
          }
        }
      }

      const dedupedByTime = new Map<number, (typeof chartMarkers)[number]>();
      for (const marker of chartMarkers) {
        const key = marker.time as number;
        const existing = dedupedByTime.get(key);
        if (!existing || marker.rank > existing.rank) {
          dedupedByTime.set(key, marker);
        }
      }

      const merged = Array.from(dedupedByTime.values()).sort(
        (a, b) => (a.time as number) - (b.time as number),
      );
      markerLabelsRef.current = merged.map((m) => ({
        time: m.time,
        text: m.text,
        source: m.source,
      }));
      const markers: SeriesMarker<Time>[] = merged.map((m) => ({
          time: m.time,
          position: "aboveBar" as const,
          color: m.color,
          shape: "arrowDown" as const,
          text: "",
        }));
      markerSeries.setMarkers(markers);
    } else {
      priceRef.current.setMarkers([]);
      bidRef.current?.setMarkers([]);
      askRef.current?.setMarkers([]);
      markerLabelsRef.current = [];
    }

    chartRef.current?.timeScale().fitContent();
    requestAnimationFrame(() => {
      if (chartRef.current) {
        renderOverlayAnnotations(
          chartRef.current,
          overlayRef.current,
          lineTimesRef.current,
          newsEventsRef.current,
          markerLabelsRef.current,
        );
      }
    });
  }, [series, anomalies, tradeOutliers, highlightTs, newsEvents]);

  return (
    <div className="relative h-[420px] w-full" aria-label="Price and volume chart">
      <div ref={containerRef} className="h-full w-full" />
      <div
        ref={overlayRef}
        className="pointer-events-none absolute inset-0 overflow-hidden"
        aria-hidden="true"
      />
    </div>
  );
}

function formatLocalChartTime(t: Time): string {
  if (typeof t === "number") {
    const sec = t as number;
    const d = new Date(sec * 1000);
    const iso = d.toISOString();
    const local = d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
    return `${local} · ET ${fmtTimeEastern(iso)} · ${fmtTimeUtc(iso)}`;
  }
  if (isBusinessDay(t)) {
    return new Date(t.year, t.month - 1, t.day).toLocaleDateString();
  }
  if (typeof t === "string") {
    return new Date(t).toLocaleString();
  }
  return "";
}

function buildDisplayPriceSeries(series: MarketSeries): DisplaySeries {
  const observations = normalizeObservations([
    ...series.snapshots.flatMap((snapshot) => {
      const out: DisplayObservation[] = [];
      addObservation(out, snapshot.ts, snapshot.last_price, 2);
      if (snapshot.yes_bid != null && snapshot.yes_ask != null) {
        addObservation(out, snapshot.ts, (snapshot.yes_bid + snapshot.yes_ask) / 2, 1);
      }
      return out;
    }),
    ...series.trades.flatMap((trade) => {
      const out: DisplayObservation[] = [];
      addObservation(out, trade.ts, trade.yes_price, 3);
      return out;
    }),
  ]);

  return buildUniformSeries(observations);
}

function quoteObservations(
  series: MarketSeries,
  side: "bid" | "ask",
): DisplayObservation[] {
  const out: DisplayObservation[] = [];
  for (const snapshot of series.snapshots) {
    addObservation(
      out,
      snapshot.ts,
      side === "bid" ? snapshot.yes_bid : snapshot.yes_ask,
      1,
    );
  }
  return normalizeObservations(out);
}

function buildUniformQuoteSeries(
  observations: DisplayObservation[],
  display: DisplaySeries,
): LineData[] {
  if (!observations.length) return [];
  if (!display.points.length) return buildUniformSeries(observations).points;
  return buildUniformPoints(
    observations,
    display.bucketSeconds,
    display.start,
    display.end,
  );
}

function buildUniformSeries(observations: DisplayObservation[]): DisplaySeries {
  const clean = normalizeObservations(observations);
  if (!clean.length) {
    return { points: [], bucketSeconds: 60, start: 0, end: 0 };
  }

  const first = clean[0].unix;
  const last = clean[clean.length - 1].unix;
  const bucketSeconds = displayBucketSeconds(Math.max(0, last - first));
  const start = Math.floor(first / bucketSeconds) * bucketSeconds;
  const end = Math.ceil(last / bucketSeconds) * bucketSeconds;

  return {
    points: buildUniformPoints(clean, bucketSeconds, start, end),
    bucketSeconds,
    start,
    end,
  };
}

function buildUniformPoints(
  observations: DisplayObservation[],
  bucketSeconds: number,
  start: number,
  end: number,
): LineData[] {
  const clean = normalizeObservations(observations);
  if (!clean.length || end < start) return [];

  const bucketValues = bucketBestObservations(clean, bucketSeconds, start, end);
  const points: LineData[] = [];
  let nextIndex = 0;
  let previous: DisplayObservation | null = null;

  for (let unix = start; unix <= end; unix += bucketSeconds) {
    while (nextIndex < clean.length && clean[nextIndex].unix < unix) {
      previous = clean[nextIndex];
      nextIndex += 1;
    }

    const direct = bucketValues.get(unix);
    const value =
      direct?.value ?? interpolatedValue(previous, clean[nextIndex] ?? null, unix);
    if (value == null) continue;
    points.push({ time: unix as UTCTimestamp, value: clampProbability(value) });
  }

  return points;
}

function buildBucketedVolumePoints(
  series: MarketSeries,
  display: DisplaySeries,
): HistogramData[] {
  if (!display.points.length) return [];

  const buckets = new Map<
    number,
    { total: number; yes: number; no: number; other: number }
  >();
  for (const trade of series.trades) {
    const unix = trade.ts ? parseChartUnix(trade.ts) : null;
    const count = finiteNumber(trade.count);
    if (unix == null || count == null || count <= 0) continue;

    const bucket =
      Math.floor(unix / display.bucketSeconds) * display.bucketSeconds;
    if (bucket < display.start || bucket > display.end) continue;

    const entry = buckets.get(bucket) ?? { total: 0, yes: 0, no: 0, other: 0 };
    entry.total += count;
    if (trade.taker_side === "yes") entry.yes += count;
    else if (trade.taker_side === "no") entry.no += count;
    else entry.other += count;
    buckets.set(bucket, entry);
  }

  return Array.from(buckets.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([unix, entry]) => ({
      time: unix as UTCTimestamp,
      value: entry.total,
      color: volumeBucketColor(entry),
    }));
}

function addObservation(
  out: DisplayObservation[],
  ts: string | null | undefined,
  value: number | null | undefined,
  sourceRank: number,
): void {
  if (!ts) return;
  const unix = parseChartUnix(ts);
  const cleanValue = finiteNumber(value);
  if (unix == null || cleanValue == null) return;
  out.push({ unix, value: clampProbability(cleanValue), sourceRank });
}

function normalizeObservations(
  observations: DisplayObservation[],
): DisplayObservation[] {
  const bySecond = new Map<number, DisplayObservation>();
  for (const obs of observations) {
    if (!Number.isFinite(obs.unix) || !Number.isFinite(obs.value)) continue;
    const previous = bySecond.get(obs.unix);
    if (
      !previous ||
      obs.sourceRank > previous.sourceRank ||
      (obs.sourceRank === previous.sourceRank && obs.unix >= previous.unix)
    ) {
      bySecond.set(obs.unix, obs);
    }
  }
  return Array.from(bySecond.values()).sort((a, b) => a.unix - b.unix);
}

function bucketBestObservations(
  observations: DisplayObservation[],
  bucketSeconds: number,
  start: number,
  end: number,
): Map<number, DisplayObservation> {
  const buckets = new Map<number, DisplayObservation>();
  for (const obs of observations) {
    const bucket = Math.floor(obs.unix / bucketSeconds) * bucketSeconds;
    if (bucket < start || bucket > end) continue;
    const previous = buckets.get(bucket);
    if (
      !previous ||
      obs.sourceRank > previous.sourceRank ||
      (obs.sourceRank === previous.sourceRank && obs.unix >= previous.unix)
    ) {
      buckets.set(bucket, obs);
    }
  }
  return buckets;
}

function displayBucketSeconds(spanSeconds: number): number {
  let bucket =
    spanSeconds <= 24 * 3600
      ? 60
      : spanSeconds <= 7 * 24 * 3600
        ? 300
        : spanSeconds <= 30 * 24 * 3600
          ? 900
          : 3600;

  while (
    Math.ceil(spanSeconds / bucket) + 1 > DISPLAY_SERIES_TARGET_MAX_POINTS
  ) {
    const next = DISPLAY_BUCKET_INTERVALS_SEC.find((interval) => interval > bucket);
    if (next == null) break;
    bucket = next;
  }
  return bucket;
}

function interpolatedValue(
  previous: DisplayObservation | null,
  next: DisplayObservation | null,
  unix: number,
): number | null {
  if (previous && next) {
    if (next.unix === previous.unix) return next.value;
    const ratio = Math.min(
      1,
      Math.max(0, (unix - previous.unix) / (next.unix - previous.unix)),
    );
    return previous.value + (next.value - previous.value) * ratio;
  }
  return previous?.value ?? next?.value ?? null;
}

function finiteNumber(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function volumeBucketColor(entry: { yes: number; no: number }): string {
  if (entry.yes > entry.no) return "rgba(46, 204, 113, 0.55)";
  if (entry.no > entry.yes) return "rgba(231, 76, 60, 0.55)";
  return "rgba(78, 161, 255, 0.45)";
}

function uniqueSortedTimes(points: LineData[]): Time[] {
  return Array.from(new Set(points.map((p) => p.time as number)))
    .sort((a, b) => a - b)
    .map((t) => t as UTCTimestamp);
}

function clampProbability(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

function dynamicProbabilityRange(values: number[]): { minValue: number; maxValue: number } {
  const clean = values.filter((v) => Number.isFinite(v));
  if (!clean.length) return { minValue: 0, maxValue: 1 };

  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = Math.max(0, max - min);

  if (span >= 0.35) {
    return { minValue: 0, maxValue: 1 };
  }

  const pad = Math.max(0.035, span * 0.75);
  let lo = Math.max(0, min - pad);
  let hi = Math.min(1, max + pad);

  if (hi - lo < 0.12) {
    const mid = (lo + hi) / 2;
    lo = Math.max(0, mid - 0.06);
    hi = Math.min(1, mid + 0.06);
  }

  if (lo === 0 && hi < 0.12) hi = 0.12;
  if (hi === 1 && lo > 0.88) lo = 0.88;

  return { minValue: lo, maxValue: hi };
}

function nearestTime(
  sortedTimes: Time[],
  target: number,
): UTCTimestamp | null {
  if (!sortedTimes.length) return null;
  const n = sortedTimes.length;
  const t0 = sortedTimes[0] as number;
  const tLast = sortedTimes[n - 1] as number;
  if (target <= t0) return sortedTimes[0] as UTCTimestamp;
  if (target >= tLast) return sortedTimes[n - 1] as UTCTimestamp;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((sortedTimes[mid] as number) < target) lo = mid + 1;
    else hi = mid;
  }
  const i = lo;
  const before = sortedTimes[i - 1] as number;
  const at = sortedTimes[i] as number;
  return target - before <= at - target
    ? (sortedTimes[i - 1] as UTCTimestamp)
    : (sortedTimes[i] as UTCTimestamp);
}

function clearOverlay(overlay: HTMLDivElement | null): void {
  if (overlay) overlay.replaceChildren();
}

function renderOverlayAnnotations(
  chart: IChartApi,
  overlay: HTMLDivElement | null,
  sortedTimes: Time[],
  events: ChartNewsEvent[],
  markerLabels: MarkerLabel[],
): void {
  if (!overlay) return;
  overlay.replaceChildren();
  if (!sortedTimes.length) return;

  const first = sortedTimes[0] as number;
  const last = sortedTimes[sortedTimes.length - 1] as number;
  if (events.length) {
    const seen = new Set<number>();
    const visibleEvents = events
      .map((event) => ({ event, unix: event.ts ? parseChartUnix(event.ts) : null }))
      .filter((row): row is { event: ChartNewsEvent; unix: number } => row.unix != null)
      .filter((row) => row.unix >= first && row.unix <= last)
      .sort((a, b) => a.unix - b.unix)
      .slice(0, 16);

    for (const { event, unix } of visibleEvents) {
      let x = chart.timeScale().timeToCoordinate(unix as UTCTimestamp);
      if (x == null) {
        const snapped = nearestTime(sortedTimes, unix);
        x = snapped == null ? null : chart.timeScale().timeToCoordinate(snapped);
      }
      if (x == null || x < 0 || x > overlay.clientWidth) continue;
      const key = Math.round(x);
      if (seen.has(key)) continue;
      seen.add(key);

      const line = document.createElement("div");
      line.className =
        "absolute top-0 bottom-7 w-px bg-[hsl(var(--severity-medium))]/75";
      line.style.left = `${x}px`;
      overlay.append(line);

      const label = document.createElement("div");
      label.className =
        "absolute top-2 rounded-sm border border-[hsl(var(--severity-medium))]/50 bg-card/90 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-foreground shadow-sm";
      label.textContent = event.direction_label
        ? `news ${event.direction_label.replace("supports_", "")}`
        : "news";
      label.style.left = `${Math.min(Math.max(x + 4, 4), Math.max(4, overlay.clientWidth - 86))}px`;
      overlay.append(label);
    }
  }

  if (!markerLabels.length) return;
  const laneEnds = [-Infinity, -Infinity, -Infinity, -Infinity];
  const sorted = [...markerLabels]
    .map((label) => ({
      ...label,
      x: chart.timeScale().timeToCoordinate(label.time),
    }))
    .filter((row) => row.x != null)
    .map((row) => ({
      ...row,
      x: Number(row.x),
    }))
    .filter((row) => row.x >= 0 && row.x <= overlay.clientWidth)
    .sort((a, b) => a.x - b.x);
  for (const item of sorted) {
    const width = Math.min(220, 16 + item.text.length * 6.2);
    const left = Math.max(4, Math.min(item.x + 6, Math.max(4, overlay.clientWidth - width - 4)));
    const right = left + width;
    let lane = 0;
    while (lane < laneEnds.length - 1 && laneEnds[lane] > left - 8) lane += 1;
    laneEnds[lane] = right;

    const label = document.createElement("div");
    const palette =
      item.source === "trade"
        ? "border-[rgba(231,76,60,0.75)] bg-[rgba(120,20,20,0.85)] text-[rgba(255,220,220,1)]"
        : item.source === "alert"
          ? "border-[rgba(245,158,11,0.75)] bg-[rgba(92,53,10,0.86)] text-[rgba(255,229,188,1)]"
          : "border-[rgba(78,161,255,0.8)] bg-[rgba(17,48,84,0.88)] text-[rgba(220,238,255,1)]";
    label.className =
      `absolute rounded-sm border px-1.5 py-0.5 text-[10px] tracking-wide shadow-sm ${palette}`;
    label.style.left = `${left}px`;
    label.style.top = `${4 + lane * 18}px`;
    label.textContent = item.text;
    overlay.append(label);
  }
}

function parseChartUnix(value: string): number | null {
  const ms = new Date(value).getTime();
  if (!Number.isFinite(ms)) return null;
  return Math.floor(ms / 1000);
}

function estimateTradeDollars(t: TradePoint): number | null {
  if (t.count == null) return null;
  const side = (t.taker_side ?? "").toLowerCase();
  const price =
    side === "no" && t.no_price != null ? t.no_price : t.yes_price ?? t.no_price;
  return price == null ? null : t.count * price;
}

function formatAbbrevDollars(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}
