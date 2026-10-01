"use client";

import { useMemo, useState } from "react";
import { inr, inrCompact, istDay } from "@/lib/format";

const W = 900;
const PAD = { top: 14, right: 14, bottom: 28, left: 64 };

/**
 * The spread of simulated outcomes: how many paths ended in each P&L band.
 * Losses and profits take the two poles of one diverging pair; a second plan
 * is drawn as an outline over the first so the two shapes can be compared.
 */
export function OutcomeHistogram({
  primary,
  secondary,
  height = 230,
}: {
  primary: { lo: number; hi: number; count: number }[];
  secondary?: { lo: number; hi: number; count: number }[] | null;
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const model = useMemo(() => {
    const all = [...primary, ...(secondary ?? [])];
    if (!all.length) return null;
    const lo = Math.min(...all.map((b) => b.lo));
    const hi = Math.max(...all.map((b) => b.hi));
    const total1 = primary.reduce((s, b) => s + b.count, 0) || 1;
    const total2 = (secondary ?? []).reduce((s, b) => s + b.count, 0) || 1;
    const share = (b: { count: number }, total: number) => b.count / total;
    const peak = Math.max(...primary.map((b) => share(b, total1)), ...(secondary ?? []).map((b) => share(b, total2)));
    const innerW = W - PAD.left - PAD.right;
    const innerH = height - PAD.top - PAD.bottom;
    const x = (v: number) => PAD.left + ((v - lo) / (hi - lo || 1)) * innerW;
    const y = (s: number) => PAD.top + innerH - (s / (peak || 1)) * innerH;
    return { lo, hi, x, y, total1, total2, share, innerH };
  }, [primary, secondary, height]);
  if (!model) return null;
  const { x, y, total1, total2, share, lo, hi } = model;
  const zero = lo < 0 && hi > 0 ? x(0) : null;

  return (
    <svg viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label="Distribution of simulated campaign P&L"
         onMouseLeave={() => setHover(null)}>
      <line x1={PAD.left} x2={W - PAD.right} y1={height - PAD.bottom} y2={height - PAD.bottom} stroke="var(--border)" />
      {primary.map((b, i) => {
        const x0 = x(b.lo), x1 = x(b.hi);
        const mid = (b.lo + b.hi) / 2;
        return (
          <rect key={i} x={x0 + 1} width={Math.max(1, x1 - x0 - 2)} y={y(share(b, total1))}
                height={height - PAD.bottom - y(share(b, total1))} rx={3}
                fill={mid < 0 ? "var(--neg)" : "var(--pos)"} opacity={hover === i ? 0.95 : 0.7}
                className="pg-bar" onMouseEnter={() => setHover(i)} />
        );
      })}
      {secondary && (
        <path
          d={secondary.map((b, i) => `${i === 0 ? "M" : "L"}${x(b.lo)},${y(share(b, total2))} L${x(b.hi)},${y(share(b, total2))}`).join(" ")}
          fill="none" stroke="var(--ink)" strokeWidth={2} strokeDasharray="5 4" />
      )}
      {zero != null && <line x1={zero} x2={zero} y1={PAD.top} y2={height - PAD.bottom} stroke="var(--ink-muted)" strokeDasharray="3 3" />}
      {[lo, (lo + hi) / 2, hi].map((v, i) => (
        <text key={i} x={x(v)} y={height - 8} fontSize={11} fill="var(--ink-muted)"
              textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}>{inrCompact(v)}</text>
      ))}
      {hover != null && primary[hover] && (
        <g>
          <rect x={Math.min(W - 230, x(primary[hover].lo))} y={PAD.top} width={220} height={40} rx={6}
                fill="var(--surface)" stroke="var(--border-strong)" />
          <text x={Math.min(W - 230, x(primary[hover].lo)) + 10} y={PAD.top + 17} fontSize={11.5} fill="var(--ink)">
            {inr(primary[hover].lo)} to {inr(primary[hover].hi)}
          </text>
          <text x={Math.min(W - 230, x(primary[hover].lo)) + 10} y={PAD.top + 32} fontSize={11} fill="var(--ink-muted)">
            {(100 * share(primary[hover], total1)).toFixed(1)}% of paths
          </text>
        </g>
      )}
    </svg>
  );
}

/** Two equity curves over the same dates: the campaign as traded and as edited. */
export function CompareLines({
  a,
  b,
  height = 240,
}: {
  a: { ts: string; equity: number }[];
  b: { ts: string; equity: number }[];
  height?: number;
}) {
  const model = useMemo(() => {
    const pts = [...a, ...b];
    if (pts.length < 2) return null;
    const ts = pts.map((p) => Date.parse(p.ts));
    const t0 = Math.min(...ts), t1 = Math.max(...ts);
    const ys = pts.map((p) => p.equity);
    const pad = (Math.max(...ys) - Math.min(...ys)) * 0.1 || 1;
    const lo = Math.min(...ys, 0) - pad, hi = Math.max(...ys, 0) + pad;
    const innerW = W - PAD.left - PAD.right, innerH = height - PAD.top - PAD.bottom;
    const x = (t: number) => PAD.left + ((t - t0) / (t1 - t0 || 1)) * innerW;
    const y = (v: number) => PAD.top + innerH - ((v - lo) / (hi - lo)) * innerH;
    const path = (s: { ts: string; equity: number }[]) =>
      s.map((p, i) => `${i ? "L" : "M"}${x(Date.parse(p.ts)).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
    return { x, y, lo, hi, t0, t1, path };
  }, [a, b, height]);
  if (!model) return null;
  const { y, lo, hi, t0, t1, path } = model;
  return (
    <svg viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label="Campaign equity, as traded and as edited">
      {[lo, 0, hi].map((v, i) => (
        <g key={i}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} stroke="var(--border)"
                strokeDasharray={v === 0 ? "0" : "3 4"} />
          <text x={PAD.left - 8} y={y(v) + 4} fontSize={11} fill="var(--ink-muted)" textAnchor="end">{inrCompact(v)}</text>
        </g>
      ))}
      <path d={path(a)} fill="none" stroke="var(--ink-muted)" strokeWidth={2} className="pg-draw" />
      <path d={path(b)} fill="none" stroke="var(--brand)" strokeWidth={2.4} className="pg-draw" />
      <text x={PAD.left} y={height - 8} fontSize={11} fill="var(--ink-muted)">{istDay(t0 / 1000)}</text>
      <text x={W - PAD.right} y={height - 8} fontSize={11} fill="var(--ink-muted)" textAnchor="end">{istDay(t1 / 1000)}</text>
    </svg>
  );
}
