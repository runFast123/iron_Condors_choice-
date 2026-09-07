"use client";

import { useMemo, useRef, useState } from "react";
import type { EquityPoint } from "@/lib/types";
import { inrCompact, inr, num, shortDate } from "@/lib/format";

const PAD = { top: 12, right: 14, bottom: 26, left: 58 };

/**
 * Cumulative P&L with the drawdown envelope beneath it.
 *
 * Two panes rather than a dual axis: equity (rupees) and drawdown (rupees)
 * share a unit but not a range, and overlaying them on two y-scales would be
 * the classic misleading chart. Stacking small multiples keeps both honest.
 */
export function EquityChart({ points, height = 240 }: { points: EquityPoint[]; height?: number }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ i: number; x: number } | null>(null);
  const W = 900;
  const ddH = 74;

  const model = useMemo(() => {
    if (!points.length) return null;
    const eq = points.map((p) => p.equity);
    const dd = points.map((p) => p.drawdown);
    const eqMin = Math.min(0, ...eq);
    const eqMax = Math.max(0, ...eq);
    const pad = (eqMax - eqMin) * 0.08 || 1;
    const ddMin = Math.min(-1, ...dd);

    const innerW = W - PAD.left - PAD.right;
    const innerH = height - PAD.top - PAD.bottom;

    const x = (i: number) => PAD.left + (i / Math.max(1, points.length - 1)) * innerW;
    const y = (v: number) => PAD.top + innerH - ((v - (eqMin - pad)) / (eqMax + pad - (eqMin - pad))) * innerH;
    const yDd = (v: number) => (v / ddMin) * (ddH - 14);

    return { x, y, yDd, eqMin: eqMin - pad, eqMax: eqMax + pad, ddMin, innerW, innerH };
  }, [points, height]);

  if (!model) return null;

  const { x, y, yDd, eqMin, eqMax, ddMin, innerW } = model;

  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const area =
    `M${x(0).toFixed(1)},${y(0).toFixed(1)} ` +
    points.map((p, i) => `L${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ") +
    ` L${x(points.length - 1).toFixed(1)},${y(0).toFixed(1)} Z`;
  const ddArea =
    `M${x(0).toFixed(1)},0 ` +
    points.map((p, i) => `L${x(i).toFixed(1)},${yDd(p.drawdown).toFixed(1)}`).join(" ") +
    ` L${x(points.length - 1).toFixed(1)},0 Z`;

  const yTicks = ticks(eqMin, eqMax, 5);
  const xTicks = [0, Math.floor(points.length / 3), Math.floor((2 * points.length) / 3), points.length - 1];

  function onMove(event: React.MouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    const i = Math.round(((px - PAD.left) / innerW) * (points.length - 1));
    if (i >= 0 && i < points.length) setHover({ i, x: px });
  }

  const active = hover ? points[hover.i] : null;

  return (
    <div ref={wrapRef} style={{ position: "relative" }}>
      <svg
        viewBox={`0 0 ${W} ${height + ddH}`}
        style={{ width: "100%", height: "auto", display: "block" }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Cumulative profit and loss with drawdown"
      >
        {yTicks.map((t) => (
          <g key={t}>
            <line className="grid-line" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
            <text className="axis-text" x={PAD.left - 8} y={y(t) + 3} textAnchor="end">
              {inrCompact(t)}
            </text>
          </g>
        ))}
        <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} />

        <path d={area} fill="var(--c1)" opacity="0.12" />
        <path d={line} fill="none" stroke="var(--c1)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />

        {xTicks.map((i) => (
          <text key={i} className="axis-text" x={x(i)} y={height - 8} textAnchor="middle">
            {shortDate(points[i].ts)}
          </text>
        ))}

        <g transform={`translate(0, ${height})`}>
          <text className="axis-text" x={PAD.left - 8} y={10} textAnchor="end">
            0
          </text>
          <text className="axis-text" x={PAD.left - 8} y={ddH - 12} textAnchor="end">
            {inrCompact(ddMin)}
          </text>
          <path d={ddArea} fill="var(--neg)" opacity="0.22" />
          <path
            d={points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${yDd(p.drawdown).toFixed(1)}`).join(" ")}
            fill="none"
            stroke="var(--neg)"
            strokeWidth="1.4"
          />
          <line className="axis-line" x1={PAD.left} x2={W - PAD.right} y1={0} y2={0} />
          <text className="axis-text" x={W - PAD.right} y={ddH - 2} textAnchor="end" style={{ fontWeight: 600 }}>
            Drawdown
          </text>
        </g>

        {hover && (
          <>
            <line
              x1={x(hover.i)}
              x2={x(hover.i)}
              y1={PAD.top}
              y2={height + ddH - 16}
              stroke="var(--ink-muted)"
              strokeWidth="1"
              strokeDasharray="3 3"
            />
            <circle cx={x(hover.i)} cy={y(points[hover.i].equity)} r="4" fill="var(--c1)"
                    stroke="var(--surface-2)" strokeWidth="2" />
          </>
        )}
      </svg>

      {active && hover && (
        <div
          style={{
            position: "absolute",
            left: `${Math.min(78, (hover.x / W) * 100)}%`,
            top: 4,
            transform: "translateX(6px)",
            pointerEvents: "none",
            background: "var(--surface)",
            border: "1px solid var(--border-strong)",
            borderRadius: 7,
            padding: "7px 10px",
            fontSize: 11.5,
            boxShadow: "0 4px 14px rgba(0,0,0,.16)",
            whiteSpace: "nowrap",
            zIndex: 3,
          }}
        >
          <div style={{ color: "var(--ink-muted)", marginBottom: 3 }}>{shortDate(active.ts)}</div>
          <Row label="P&L" value={inr(active.equity, { sign: true })} tone={active.equity} />
          <Row label="Drawdown" value={inr(active.drawdown)} tone={active.drawdown} />
          {active.spot != null && <Row label="NIFTY" value={num(active.spot)} />}
          <Row label="Open rungs" value={String(active.open_condors)} />
        </div>
      )}
    </div>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: number }) {
  const color = tone === undefined ? "var(--ink)" : tone > 0 ? "var(--pos)" : tone < 0 ? "var(--neg)" : "var(--ink)";
  return (
    <div style={{ display: "flex", gap: 14, justifyContent: "space-between" }}>
      <span style={{ color: "var(--ink-muted)" }}>{label}</span>
      <span className="tnum" style={{ fontWeight: 600, color }}>
        {value}
      </span>
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
