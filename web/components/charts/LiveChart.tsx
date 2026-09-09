"use client";

import { useEffect, useRef } from "react";
import { istClock, istDay } from "@/lib/format";

export interface LivePoint {
  t: number;      // epoch seconds
  price: number;
}

/**
 * A live-updating NIFTY chart with the ladder's trigger levels drawn on it.
 *
 * Uses lightweight-charts (TradingView's own library) and appends each new
 * tick rather than re-seeding, so the line advances smoothly instead of
 * flickering on every poll. Levels already opened are drawn solid; the next
 * one that will fire is dashed and gold, so the thing a forward test is
 * waiting for is the most visible object on the chart.
 */
export function LiveChart({
  points,
  firedLevels,
  nextTrigger,
  height = 300,
}: {
  points: LivePoint[];
  firedLevels: number[];
  nextTrigger: number | null;
  height?: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chartRef = useRef<any>(null);
  const seriesRef = useRef<any>(null);
  const linesRef = useRef<any[]>([]);
  const lastTimeRef = useRef<number>(0);

  // Create once. Re-creating on every tick would reset pan/zoom and flicker.
  useEffect(() => {
    if (!container.current) return;
    let disposed = false;

    (async () => {
      const lib = await import("lightweight-charts");
      if (disposed || !container.current) return;

      const css = getComputedStyle(document.documentElement);
      const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;

      const chart = lib.createChart(container.current, {
        height,
        layout: {
          background: { color: "transparent" },
          textColor: v("--ink-muted", "#667485"),
          fontFamily: v("--font-sans", "sans-serif"),
          attributionLogo: false,
        },
        grid: {
          vertLines: { color: v("--grid", "#eef2f6") },
          horzLines: { color: v("--grid", "#eef2f6") },
        },
        rightPriceScale: { borderColor: v("--border", "#e4eaf0") },
        timeScale: {
          borderColor: v("--border", "#e4eaf0"),
          timeVisible: true,
          secondsVisible: false,
          rightOffset: 6,
          // The library defaults to UTC, which put 09:33 IST on the axis as
          // 04:03 -- beside a "Last tick" metric reading 09:33 am.
          tickMarkFormatter: (time: number) => istClock(time),
        },
        crosshair: { mode: lib.CrosshairMode.Normal },
        localization: {
          priceFormatter: (p: number) =>
            new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(p),
          timeFormatter: (time: number) => `${istDay(time)} ${istClock(time)} IST`,
        },
      });

      const series = chart.addSeries(lib.AreaSeries, {
        lineColor: v("--c1", "#2979f6"),
        topColor: "rgba(41,121,246,0.22)",
        bottomColor: "rgba(41,121,246,0.02)",
        lineWidth: 2,
        priceLineVisible: true,
        lastValueVisible: true,
      });

      chartRef.current = chart;
      seriesRef.current = series;
      (chartRef.current as any).__lib = lib;

      const observer = new ResizeObserver(() => {
        if (container.current) chart.applyOptions({ width: container.current.clientWidth });
      });
      observer.observe(container.current);
      chart.applyOptions({ width: container.current.clientWidth });
      (chartRef.current as any).__observer = observer;
    })();

    return () => {
      disposed = true;
      try {
        (chartRef.current as any)?.__observer?.disconnect();
        chartRef.current?.remove();
      } catch {
        /* already torn down */
      }
      chartRef.current = null;
      seriesRef.current = null;
      lastTimeRef.current = 0;
    };
  }, [height]);

  // Feed data. `update` for a single new tick keeps the line moving; a full
  // `setData` only when the series is seeded or history was replaced.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || points.length === 0) return;

    const sorted = [...points].sort((a, b) => a.t - b.t);
    const latest = sorted[sorted.length - 1];

    if (lastTimeRef.current === 0) {
      series.setData(sorted.map((p) => ({ time: p.t as any, value: p.price })));
      chartRef.current?.timeScale().fitContent();
    } else if (latest.t > lastTimeRef.current) {
      series.update({ time: latest.t as any, value: latest.price });
    }
    lastTimeRef.current = latest.t;
  }, [points]);

  // Redraw the ladder levels whenever they change.
  useEffect(() => {
    const series = seriesRef.current;
    const lib = (chartRef.current as any)?.__lib;
    if (!series || !lib) return;

    for (const line of linesRef.current) {
      try { series.removePriceLine(line); } catch { /* gone with the chart */ }
    }
    linesRef.current = [];

    const css = getComputedStyle(document.documentElement);
    const v = (n: string, f: string) => css.getPropertyValue(n).trim() || f;

    for (const level of firedLevels) {
      linesRef.current.push(
        series.createPriceLine({
          price: level,
          color: v("--c3", "#009591"),
          lineWidth: 1,
          lineStyle: lib.LineStyle.Solid,
          axisLabelVisible: true,
          title: "open",
        }),
      );
    }

    if (nextTrigger != null) {
      linesRef.current.push(
        series.createPriceLine({
          price: nextTrigger,
          color: v("--accent", "#ffce02"),
          lineWidth: 2,
          lineStyle: lib.LineStyle.Dashed,
          axisLabelVisible: true,
          title: "next entry",
        }),
      );
    }
  }, [firedLevels, nextTrigger]);

  return <div ref={container} style={{ width: "100%", minHeight: height }} />;
}
