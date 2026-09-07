"use client";

import { useMemo, useState } from "react";
import { inr, inrCompact, num } from "@/lib/format";

const PAD = { top: 16, right: 16, bottom: 30, left: 62 };

/**
 * Expiry payoff across the whole ladder.
 *
 * Profit and loss are a polarity, so the fill is diverging: one hue above the
 * zero line, one below, with the zero line itself as the neutral midpoint.
 * That is the one place green/red is legitimate here, and the axis label plus
 * the sign on every tooltip figure carry the meaning without relying on hue.
 */
export function PayoffChart({
  data,
  spot,
  height = 300,
}: {
  data: { spot: number; pnl: number }[];
  spot?: number | null;
  height?: number;
}) {
  const [hover, setHover] = useState<{ i: number; x: number } | null>(null);
  const W = 900;

  const model = useMemo(() => {
    if (data.length < 2) return null;
    const xs = data.map((d) => d.spot);
    const ys = data.map((d) => d.pnl);
    const xMin = Math.min(...xs);
    const xMax = Math.max(...xs);
    const yMin = Math.min(...ys);
    const yMax = Math.max(...ys);
    const pad = (yMax - yMin) * 0.12 || 1;
    const lo = yMin - pad;
    const hi = yMax + pad;

    const innerW = W - PAD.left - PAD.right;
    const innerH = height - PAD.top - PAD.bottom;
    const x = (v: number) => PAD.left + ((v - xMin) / (xMax - xMin)) * innerW;
    const y = (v: number) => PAD.top + innerH - ((v - lo) / (hi - lo)) * innerH;

    // Zero crossings = breakevens of the combined structure.
    const crossings: number[] = [];
    for (let i = 1; i < data.length; i++) {
      const a = data[i - 1];
      const b = data[i];
      if ((a.pnl <= 0 && b.pnl > 0) || (a.pnl >= 0 && b.pnl < 0)) {
        const t = Math.abs(a.pnl) / (Math.abs(a.pnl) + Math.abs(b.pnl) || 1);
        crossings.push(a.spot + t * (b.spot - a.spot));
      }
    }
    return { x, y, xMin, xMax, lo, hi, innerW, innerH, crossings };
  }, [data, height]);

  if (!model) return null;
  const { x, y, lo, hi, innerW, crossings } = model;

  const path = data.map((d, i) => `${i ? "L" : "M"}${x(d.spot).toFixed(1)},${y(d.pnl).toFixed(1)}`).join(" ");
  const zeroY = y(0);
  const yTicks = ticks(lo, hi, 5);
  const xTicks = ticks(model.xMin, model.xMax, 6);

  function onMove(event: React.MouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    const i = Math.round(((px - PAD.left) / innerW) * (data.length - 1));
    if (i >= 0 && i < data.length) setHover({ i, x: px });
  }

  const active = hover ? data[hover.i] : null;

  return (
    <div style={{ position: "relative" }}>
      <svg
        viewBox={`0 0 ${W} ${height}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Combined expiry payoff across the condor ladder"
      >
        <defs>
          <clipPath id="above-zero">
            <rect x={PAD.left} y={PAD.top} width={innerW} height={Math.max(0, zeroY - PAD.top)} />
          </clipPath>
          <clipPath id="below-zero">
            <rect x={PAD.left} y={zeroY} width={innerW} height={Math.max(0, height - PAD.bottom - zeroY)} />
          </clipPath>
        </defs>

        {yTicks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
            <text className="axis-text" x={PAD.left - 8} y={y(t) + 3} textAnchor="end">
              {inrCompact(t)}
            </text>
          </g>
        ))}

        {/* Diverging fill about the zero line. */}
        <path d={`${path} L${x(model.xMax)},${zeroY} L${x(model.xMin)},${zeroY} Z`}
              fill="var(--pos)" opacity="0.16" clipPath="url(#above-zero)" />
        <path d={`${path} L${x(model.xMax)},${zeroY} L${x(model.xMin)},${zeroY} Z`}
              fill="var(--neg)" opacity="0.16" clipPath="url(#below-zero)" />

        <line x1={PAD.left} x2={W - PAD.right} y1={zeroY} y2={zeroY} stroke="var(--axis)" strokeWidth="1.5" />
        <path d={path} fill="none" stroke="var(--c1)" strokeWidth="2.2" strokeLinejoin="round" />

        {crossings.map((c) => (
          <g key={c}>
            <line x1={x(c)} x2={x(c)} y1={PAD.top} y2={height - PAD.bottom}
                  stroke="var(--ink-muted)" strokeWidth="1" strokeDasharray="2 3" opacity="0.7" />
            <text className="axis-text" x={x(c)} y={PAD.top - 3} textAnchor="middle" style={{ fontWeight: 600 }}>
              BE {num(c)}
            </text>
          </g>
        ))}

        {spot != null && (
          <g>
            <line x1={x(spot)} x2={x(spot)} y1={PAD.top} y2={height - PAD.bottom}
                  stroke="var(--accent)" strokeWidth="1.8" />
            <text className="axis-text" x={x(spot)} y={height - PAD.bottom + 22} textAnchor="middle"
                  style={{ fill: "var(--ink)", fontWeight: 700 }}>
              spot {num(spot)}
            </text>
          </g>
        )}

        {xTicks.map((t) => (
          <text key={t} className="axis-text" x={x(t)} y={height - PAD.bottom + 13} textAnchor="middle">
            {num(t)}
          </text>
        ))}

        {hover && active && (
          <>
            <line x1={x(active.spot)} x2={x(active.spot)} y1={PAD.top} y2={height - PAD.bottom}
                  stroke="var(--ink-muted)" strokeWidth="1" strokeDasharray="3 3" />
            <circle cx={x(active.spot)} cy={y(active.pnl)} r="4.5"
                    fill={active.pnl >= 0 ? "var(--pos)" : "var(--neg)"}
                    stroke="var(--surface-2)" strokeWidth="2" />
          </>
        )}
      </svg>

      {active && hover && (
        <div
          style={{
            position: "absolute",
            left: `${Math.min(76, (hover.x / W) * 100)}%`,
            top: 8,
            transform: "translateX(8px)",
            pointerEvents: "none",
            background: "var(--surface)",
            border: "1px solid var(--border-strong)",
            borderRadius: 7,
            padding: "7px 10px",
            fontSize: 11.5,
            boxShadow: "0 4px 14px rgba(0,0,0,.16)",
            whiteSpace: "nowrap",
          }}
        >
          <div style={{ color: "var(--ink-muted)" }}>NIFTY at expiry</div>
          <div className="tnum" style={{ fontWeight: 700, fontSize: 13 }}>{num(active.spot)}</div>
          <div
            className="tnum"
            style={{
              fontWeight: 700,
              color: active.pnl >= 0 ? "var(--pos)" : "var(--neg)",
              marginTop: 2,
            }}
          >
            {inr(active.pnl, { sign: true })}
          </div>
        </div>
      )}
    </div>
  );
}

function ticks(min: number, max: number, count: number): number[] {
  const span = max - min;
  if (span <= 0) return [min];
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const out: number[] = [];
  for (let t = Math.ceil(min / step) * step; t <= max; t += step) out.push(t);
  return out;
}
