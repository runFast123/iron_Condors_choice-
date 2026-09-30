"use client";

import { useEffect, useState } from "react";
import type { LiveCampaign, LivePosition } from "@/lib/live";
import { dateTime, inr, num, shortDate } from "@/lib/format";
import { useCountUp } from "@/lib/useCountUp";
import { Badge } from "@/components/ui";
import { LiveChart, type LivePoint } from "@/components/charts/LiveChart";

/**
 * One finished (or settling) campaign on its own: what it opened, how NIFTY
 * moved through it, where it settled, and what it made -- none of it mixed
 * with the campaign trading now.
 */
export function PastCampaign({
  run,
  campaign,
  positions,
}: {
  run: string;
  campaign: LiveCampaign;
  positions: LivePosition[];
}) {
  const [points, setPoints] = useState<LivePoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setPoints(null);
    setError(null);
    fetch(`/api/forward/campaign?run=${encodeURIComponent(run)}&expiry=${encodeURIComponent(campaign.expiry)}`, {
      cache: "no-store",
    })
      .then(async (res) => {
        const body = await res.json();
        if (!live) return;
        if (!res.ok) {
          setError(body.error ?? `Could not load this campaign's chart (${res.status}).`);
          return;
        }
        setPoints(
          (body.bars as { ts: string; spot: number }[]).map((b) => ({
            t: Math.floor(new Date(b.ts).getTime() / 1000),
            price: b.spot,
          })),
        );
      })
      .catch((err) => live && setError((err as Error).message));
    return () => {
      live = false;
    };
  }, [run, campaign.expiry]);

  const pnl = useCountUp(campaign.pnl);
  const mine = positions
    .filter((p) => p.expiry === campaign.expiry)
    .sort((a, b) => a.entry_time.localeCompare(b.entry_time));
  const outcome = campaign.status === "settled" ? "Settled at expiry"
    : campaign.status === "closed" ? "Closed before expiry"
      : "Awaiting its official close";

  return (
    <div className="fade-up">
      <div className="campaign-head">
        <div>
          <div style={{ fontSize: 15, fontWeight: 700 }}>
            {shortDate(campaign.expiry)} campaign
          </div>
          <div style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 3, lineHeight: 1.55 }}>
            Opened {shortDate(campaign.started_at)}, anchored at {num(campaign.anchor)}.{" "}
            {outcome}
            {campaign.settlement_spot != null
              ? ` against NIFTY's official close of ${num(Math.round(campaign.settlement_spot))}`
              : ""}
            {campaign.ended_at ? ` (${dateTime(campaign.ended_at)})` : ""}.
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 10.5, color: "var(--ink-muted)", fontWeight: 600 }}>Campaign P&amp;L</div>
          <div
            className="tnum"
            style={{
              fontSize: 26, fontWeight: 800,
              color: pnl == null ? "var(--ink-muted)" : pnl > 0 ? "var(--pos)" : pnl < 0 ? "var(--neg)" : "var(--ink)",
            }}
          >
            {pnl == null ? "--" : inr(Math.round(pnl), { sign: true })}
          </div>
        </div>
      </div>

      <div className="campaign-stats">
        <Tile label="Positions" value={num(campaign.positions)}
              hint={campaign.up > 0 ? `${campaign.down} down · ${campaign.up} up` : `${campaign.down} below the anchor`} />
        <Tile label="Credit collected" value={inr(campaign.credit)} />
        <Tile label="Realised" value={inr(campaign.realised, { sign: true })}
              tone={campaign.realised > 0 ? "pos" : campaign.realised < 0 ? "neg" : undefined} />
        {campaign.open > 0 && (
          <Tile label="Still open" value={inr(campaign.unrealised, { sign: true })}
                hint={`${campaign.open} marked at the last price`}
                tone={campaign.unrealised > 0 ? "pos" : campaign.unrealised < 0 ? "neg" : undefined} />
        )}
        <Tile label="Best position" value={inr(campaign.best, { sign: true })}
              tone={campaign.best > 0 ? "pos" : campaign.best < 0 ? "neg" : undefined} />
        <Tile label="Worst position" value={inr(campaign.worst, { sign: true })}
              tone={campaign.worst > 0 ? "pos" : campaign.worst < 0 ? "neg" : undefined} />
      </div>

      <div className="campaign-chart">
        <div className="campaign-chart-head">
          <span>NIFTY through the campaign · 15-minute bars</span>
          <span style={{ color: "var(--c3)" }}>&#9473; levels opened</span>
        </div>
        {error ? (
          <div className="campaign-chart-empty">{error}</div>
        ) : points == null ? (
          <div className="skeleton" style={{ height: 280, borderRadius: 0 }} aria-label="Loading the chart" />
        ) : points.length === 0 ? (
          <div className="campaign-chart-empty">Choice served no NIFTY bars for these dates.</div>
        ) : (
          <div className="fade-in">
            <LiveChart points={points} firedLevels={campaign.levels} nextTrigger={null} height={280} levelTitle="opened" />
          </div>
        )}
      </div>

      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink-2)", margin: "16px 0 7px" }}>
        Positions in this campaign
      </div>
      <div className="scroll-x">
        <table>
          <thead>
            <tr>
              <th>Level</th><th>Side</th><th>Opened</th>
              <th style={{ textAlign: "right" }}>Credit</th>
              <th style={{ textAlign: "right" }}>P&amp;L</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {mine.map((p, i) => (
              <tr key={p.index} className="row-in" style={{ animationDelay: `${Math.min(i, 12) * 30}ms` }}>
                <td className="tnum" style={{ fontWeight: 700 }}>{num(p.level)}</td>
                <td>
                  <Badge tone={p.side === "up" ? "warn" : p.side === "anchor" ? "brand" : "neutral"}>
                    {(p.side ?? "down").toUpperCase()}
                  </Badge>
                </td>
                <td style={{ color: "var(--ink-2)" }}>{dateTime(p.entry_time)}</td>
                <td className="tnum" style={{ textAlign: "right" }}>{inr(p.credit)}</td>
                <td
                  className="tnum"
                  style={{
                    textAlign: "right", fontWeight: 700,
                    color: p.pnl == null ? "var(--ink-muted)" : p.pnl > 0 ? "var(--pos)" : p.pnl < 0 ? "var(--neg)" : "var(--ink)",
                  }}
                >
                  {p.pnl == null ? "--" : inr(p.pnl, { sign: true })}
                </td>
                <td>
                  <Badge tone="neutral" title={p.exit_reason ?? undefined}>
                    {p.is_open ? "Open" : p.status === "EXPIRED" ? "Settled" : "Closed"}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Tile({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "pos" | "neg" }) {
  return (
    <div className="campaign-tile">
      <div style={{ fontSize: 10.5, color: "var(--ink-muted)", fontWeight: 600 }}>{label}</div>
      <div className="tnum" style={{ fontSize: 16, fontWeight: 700, marginTop: 2,
        color: tone === "pos" ? "var(--pos)" : tone === "neg" ? "var(--neg)" : "var(--ink)" }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 10, color: "var(--ink-muted)", marginTop: 1 }}>{hint}</div>}
    </div>
  );
}
