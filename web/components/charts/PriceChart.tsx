"use client";

import { useEffect, useRef, useState } from "react";
import type { EquityPoint, Trigger } from "@/lib/types";

/**
 * NIFTY with the ladder's trigger levels drawn on top.
 *
 * Uses lightweight-charts (TradingView's own OSS library) so pan/zoom and the
 * crosshair behave the way anyone reading a price chart expects. Each fired
 * rung gets a horizontal line at its level plus a marker at the bar where it
 * actually triggered.
 */
export function PriceChart({
  points,
  triggers,
  height = 460,
}: {
  points: EquityPoint[];
  triggers: Trigger[];
  height?: number;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!container.current) return;
    let chart: any;
    let disposed = false;

    (async () => {
      try {
        const lib = await import("lightweight-charts");
        if (disposed || !container.current) return;

        const css = getComputedStyle(document.documentElement);
        const v = (name: string, fallback: string) =>
          css.getPropertyValue(name).trim() || fallback;

        chart = lib.createChart(container.current, {
          height,
          layout: {
            background: { color: "transparent" },
            textColor: v("--ink-muted", "#667485"),
            fontFamily: v("--font-sans", "sans-serif"),
            attributionLogo: false,
          },
          grid: {
            vertLines: { color: v("--grid", "#e7edf2") },
            horzLines: { color: v("--grid", "#e7edf2") },
          },
          rightPriceScale: { borderColor: v("--border", "#dfe7ec") },
          timeScale: { borderColor: v("--border", "#dfe7ec"), timeVisible: false },
          crosshair: { mode: lib.CrosshairMode.Normal },
          localization: {
            priceFormatter: (p: number) =>
              new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(p),
          },
        });

        const series = chart.addSeries(lib.LineSeries, {
          color: v("--c1", "#2979f6"),
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          title: "NIFTY",
        });

        const data = points
          .filter((p) => p.spot != null)
          .map((p) => ({ time: p.ts.slice(0, 10), value: p.spot as number }))
          .filter((d, i, arr) => i === 0 || d.time !== arr[i - 1].time);

        series.setData(data);

        // One price line per fired rung, coloured along a sequential ramp so
        // the ordering of the ladder reads at a glance.
        const ramp = ["--seq-5", "--seq-4", "--seq-3", "--seq-2"];
        triggers.forEach((t, i) => {
          series.createPriceLine({
            price: t.level,
            color: v(ramp[Math.min(i, ramp.length - 1)], "#2979f6"),
            lineWidth: 1,
            lineStyle: lib.LineStyle.Dashed,
            axisLabelVisible: true,
            title: `#${i + 1}`,
          });
        });

        chart.timeScale().fitContent();

        const observer = new ResizeObserver(() => {
          if (container.current) chart.applyOptions({ width: container.current.clientWidth });
        });
        observer.observe(container.current);
        chart.applyOptions({ width: container.current.clientWidth });

        return () => observer.disconnect();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();

    return () => {
      disposed = true;
      try { chart?.remove(); } catch { /* already torn down */ }
    };
  }, [points, triggers, height]);

  if (error) {
    return (
      <div style={{ padding: 20, color: "var(--ink-muted)", fontSize: 13 }}>
        Chart could not load: {error}
      </div>
    );
  }

  return <div ref={container} style={{ width: "100%", minHeight: height }} />;
}
