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

/**
 * Price + volume chart, with anomaly markers overlaid on the price
 * series.  Uses TradingView's `lightweight-charts` — same library that
 * powers Polymarket / Kalshi's own price views — so it handles
 * crosshairs, time-axis zoom, and pan natively.
 *
 * Layout:
 *   - Top pane (~80% height): yes_price line series + anomaly markers
 *   - Bottom pane (~20%):     trade-volume histogram
 *
 * The two series share an x-axis; lightweight-charts handles
 * synchronisation when you pan or zoom.
 *
 * **Step line:** price is flat between trades (no diagonal “phantom” moves).
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
}: {
  series: MarketSeries | undefined;
  anomalies: AnomalyRow[] | undefined;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const priceRef = useRef<ISeriesApi<"Line"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);

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
      color: "rgba(78, 161, 255, 1)",
      lineWidth: 2,
      lineType: LineType.WithSteps,
      priceLineVisible: false,
      lastValueVisible: true,
      priceFormat: { type: "price", precision: 3, minMove: 0.001 },
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

    return () => {
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
      return;
    }

    const linePoints: LineData[] = [];
    const volumePoints: HistogramData[] = [];
    const seenTimes = new Set<number>();
    for (const t of series.trades) {
      if (!t.ts || t.yes_price == null) continue;
      // lightweight-charts requires unique, ascending timestamps. Trades
      // can collide on the second; offset duplicates by 1ms-ish so the
      // line includes every print.
      let unix = Math.floor(new Date(t.ts).getTime() / 1000);
      while (seenTimes.has(unix)) unix += 1;
      seenTimes.add(unix);
      const time = unix as UTCTimestamp;
      linePoints.push({ time, value: t.yes_price });
      volumePoints.push({
        time,
        value: t.count ?? 0,
        color: takerColor(t),
      });
    }

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
  }, [series, anomalies]);

  return (
    <div
      ref={containerRef}
      className="h-[420px] w-full"
      aria-label="Price and volume chart"
    />
  );
}

function formatLocalChartTime(t: Time): string {
  if (typeof t === "number") {
    const sec = t as number;
    return new Date(sec * 1000).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
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

function severityColor(s: string): string {
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
