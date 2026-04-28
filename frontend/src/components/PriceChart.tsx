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
 * **Curved line + area:** `LineType.Curved` draws a smooth path through
 * each trade’s yes price. Between prints the curve is a spline, not a claim
 * that the contract traded at intermediate prices (there is no public tape
 * between events). A light gradient under the line improves legibility over
 * a hard stepped staircase.
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
  highlightTs,
  newsEvents,
}: {
  series: MarketSeries | undefined;
  anomalies: AnomalyRow[] | undefined;
  highlightTs?: string | null;
  newsEvents?: ChartNewsEvent[];
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lineTimesRef = useRef<Time[]>([]);
  const newsEventsRef = useRef<ChartNewsEvent[]>([]);
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
      lineType: LineType.WithSteps,
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
      renderNewsLineOverlay(
        chart,
        overlayRef.current,
        lineTimesRef.current,
        newsEventsRef.current,
      );
    };
    chart.timeScale().subscribeVisibleTimeRangeChange(redrawNewsLines);

    return () => {
      chart.timeScale().unsubscribeVisibleTimeRangeChange(redrawNewsLines);
      chart.remove();
      chartRef.current = null;
      priceRef.current = null;
      volumeRef.current = null;
    };
  }, []);

  // Push data updates whenever series changes.
  useEffect(() => {
    if (!priceRef.current || !volumeRef.current) return;
    if (!series) {
      priceRef.current.setData([]);
      volumeRef.current.setData([]);
      lineTimesRef.current = [];
      newsEventsRef.current = [];
      clearNewsLineOverlay(overlayRef.current);
      return;
    }

    const linePoints: LineData[] = [];
    const volumePoints: HistogramData[] = [];
    const seenTimes = new Set<number>();
    for (const t of series.trades) {
      if (!t.ts || t.yes_price == null) continue;
      // lightweight-charts requires unique, ascending timestamps. Trades
      // can share the same wall-clock second; nudge the x-axis by +1s until
      // unique (values are still each print’s `yes_price`).
      let unix = Math.floor(new Date(t.ts).getTime() / 1000);
      while (seenTimes.has(unix)) unix += 1;
      seenTimes.add(unix);
      const time = unix as UTCTimestamp;
      linePoints.push({ time, value: clampProbability(t.yes_price) });
      volumePoints.push({
        time,
        value: t.count ?? 0,
        color: takerColor(t),
      });
    }

    yRangeRef.current = dynamicProbabilityRange(linePoints.map((p) => p.value));
    lineTimesRef.current = linePoints.map((p) => p.time);
    newsEventsRef.current = newsEvents ?? [];
    priceRef.current.setData(linePoints);
    volumeRef.current.setData(volumePoints);

    // Anomaly markers: snap to the nearest trade timestamp on the
    // existing time series so the marker has a y-coordinate. If we have
    // no trades but do have anomalies, the time scale won't include
    // their x-position and lightweight-charts will silently drop them.
    if (priceRef.current && linePoints.length) {
      const sortedTimes = linePoints.map((p) => p.time);
      // One marker per bar time: same clock second can have many materialized rows.
      const byTime = new Map<number, { score: number; text: string; severity: string }>();
      for (const a of anomalies ?? []) {
        if (!a.created_at) continue;
        if (a.severity === "none" || a.score <= 0) continue;
        // Suppress pre-threshold / legacy weak rows; matches materializer `MIN_SCORE_TO_PERSIST`.
        if (a.score < 3) continue;
        const target = Math.floor(new Date(a.created_at).getTime() / 1000);
        const t = nearestTime(sortedTimes, target);
        if (t == null) continue;
        const hint =
          a.reasons && a.reasons.length > 0
            ? a.reasons[0].slice(0, 28)
            : "flag";
        const k = t as number;
        const text = `${hint} · ${a.score.toFixed(1)}`;
        const prev = byTime.get(k);
        if (!prev || a.score > prev.score) {
          byTime.set(k, { score: a.score, text, severity: a.severity });
        }
      }
      if (highlightTs) {
        const target = Math.floor(new Date(highlightTs).getTime() / 1000);
        if (Number.isFinite(target)) {
          const t = nearestTime(sortedTimes, target);
          if (t != null) {
            byTime.set(t as number, {
              score: 999,
              text: "selected unusual print",
              severity: "selected",
            });
          }
        }
      }
      const markers: SeriesMarker<Time>[] = Array.from(byTime.entries())
        .sort((a, b) => a[0] - b[0])
        .map(([ts, m]) => ({
          time: ts as UTCTimestamp,
          position: "aboveBar" as const,
          color: severityColor(m.severity),
          shape: "arrowDown" as const,
          text: m.text,
        }));
      priceRef.current.setMarkers(markers);
    } else {
      priceRef.current.setMarkers([]);
    }

    chartRef.current?.timeScale().fitContent();
    requestAnimationFrame(() => {
      if (chartRef.current) {
        renderNewsLineOverlay(
          chartRef.current,
          overlayRef.current,
          lineTimesRef.current,
          newsEventsRef.current,
        );
      }
    });
  }, [series, anomalies, highlightTs, newsEvents]);

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

function takerColor(t: TradePoint): string {
  if (t.taker_side === "yes") return "rgba(46, 204, 113, 0.55)";
  if (t.taker_side === "no") return "rgba(231, 76, 60, 0.55)";
  return "rgba(78, 161, 255, 0.45)";
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

function severityColor(s: string): string {
  if (s === "selected") return "rgba(78, 161, 255, 1)";
  if (s === "high") return "rgba(231, 76, 60, 0.95)";
  if (s === "medium") return "rgba(241, 196, 15, 0.95)";
  return "rgba(46, 204, 113, 0.95)";
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

function clearNewsLineOverlay(overlay: HTMLDivElement | null): void {
  if (overlay) overlay.replaceChildren();
}

function renderNewsLineOverlay(
  chart: IChartApi,
  overlay: HTMLDivElement | null,
  sortedTimes: Time[],
  events: ChartNewsEvent[],
): void {
  if (!overlay) return;
  overlay.replaceChildren();
  if (!sortedTimes.length || !events.length) return;

  const first = sortedTimes[0] as number;
  const last = sortedTimes[sortedTimes.length - 1] as number;
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

function parseChartUnix(value: string): number | null {
  const ms = new Date(value).getTime();
  if (!Number.isFinite(ms)) return null;
  return Math.floor(ms / 1000);
}
