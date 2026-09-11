"use client";

import { Fragment, useMemo, useState } from "react";
import type { Condor } from "@/lib/types";
import { inr, num, shortDate } from "@/lib/format";
import { Badge } from "@/components/ui";

/**
 * Trades grouped by condor, with the legs one click away.
 *
 * A flat leg blotter makes the reader add four numbers to answer the only
 * question they actually have -- did this condor make money -- and the answer
 * they get that way is wrong. Leg P&L is gross; the condor's P&L is net of
 * entry and exit costs, so a hand-summed column can never reconcile with the
 * net figure on the Overview. Both numbers are shown, with the difference
 * named, so the gap teaches rather than confuses.
 */

const OUTCOME: Record<Condor["status"], { label: string; tone: "pos" | "neg" | "brand" | "neutral" }> = {
  OPEN: { label: "Open", tone: "brand" },
  EXPIRED: { label: "Held to expiry", tone: "neutral" },
  CLOSED_TARGET: { label: "Take-profit", tone: "pos" },
  CLOSED_STOP: { label: "Stop-loss", tone: "neg" },
};

function pnlColour(value: number | null): string {
  if (value == null) return "var(--ink)";
  return value > 0 ? "var(--pos)" : value < 0 ? "var(--neg)" : "var(--ink)";
}

/** Gross P&L across the legs, or null while any leg is still open. */
function grossOf(condor: Condor): number | null {
  let total = 0;
  for (const leg of condor.legs) {
    if (leg.exit_price == null) return null;
    total += (leg.exit_price - leg.entry_price) * leg.signed_qty;
  }
  return total;
}

export function CondorBlotter({ condors }: { condors: Condor[] }) {
  const [open, setOpen] = useState<Set<number>>(new Set());

  const totals = useMemo(() => {
    const closed = condors.filter((c) => c.status !== "OPEN");
    return {
      net: condors.reduce((sum, c) => sum + (c.pnl ?? 0), 0),
      costs: condors.reduce((sum, c) => sum + c.entry_costs + c.exit_costs, 0),
      wins: closed.filter((c) => c.pnl > 0).length,
      losses: closed.filter((c) => c.pnl < 0).length,
      openNow: condors.length - closed.length,
    };
  }, [condors]);

  function toggle(index: number) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  const allOpen = open.size === condors.length && condors.length > 0;

  if (condors.length === 0) {
    return (
      <p style={{ padding: "22px 16px", textAlign: "center", fontSize: 12.5, color: "var(--ink-muted)" }}>
        No condors in this run.
      </p>
    );
  }

  return (
    <>
      <div
        style={{
          display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center",
          padding: "10px 14px", borderBottom: "1px solid var(--border)", fontSize: 12,
        }}
      >
        <span style={{ color: "var(--ink-2)" }}>
          <strong>{condors.length}</strong> condors
        </span>
        <span style={{ color: "var(--pos)" }}>{totals.wins} won</span>
        <span style={{ color: "var(--neg)" }}>{totals.losses} lost</span>
        {totals.openNow > 0 && <span style={{ color: "var(--ink-muted)" }}>{totals.openNow} open</span>}
        <span className="tnum" style={{ marginLeft: "auto", fontWeight: 700, color: pnlColour(totals.net) }}>
          {inr(totals.net, { sign: true })}
          <span style={{ fontWeight: 500, color: "var(--ink-muted)", marginLeft: 8, fontSize: 11.5 }}>
            net, after {inr(totals.costs)} of costs
          </span>
        </span>
        <button
          type="button"
          onClick={() => setOpen(allOpen ? new Set() : new Set(condors.map((c) => c.index)))}
          className="btn-quiet"
          style={{ fontSize: 11.5, padding: "4px 10px" }}
        >
          {allOpen ? "Collapse all" : "Expand all"}
        </button>
      </div>

      <div className="scroll-x" style={{ maxHeight: "70vh", overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 28 }}><span className="sr-only">Expand</span></th>
              <th>Condor</th>
              <th>Side</th>
              <th>Opened</th>
              <th>Expiry</th>
              <th style={{ textAlign: "right" }}>Credit</th>
              <th style={{ textAlign: "right" }}>Max loss</th>
              <th style={{ textAlign: "right" }}>Net P&amp;L</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {condors.map((condor) => {
              const expanded = open.has(condor.index);
              const gross = grossOf(condor);
              const costs = condor.entry_costs + condor.exit_costs;
              const outcome = OUTCOME[condor.status] ?? OUTCOME.OPEN;

              return (
                // Keyed on the Fragment, not the rows: a shorthand <> cannot
                // carry a key, so React would reconcile these by position and
                // mix up expanded state when the list changes.
                <Fragment key={condor.index}>
                  <tr
                    onClick={() => toggle(condor.index)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        toggle(condor.index);
                      }
                    }}
                    tabIndex={0}
                    role="button"
                    aria-expanded={expanded}
                    aria-label={`Condor at ${num(condor.level)}, ${expanded ? "hide" : "show"} its four legs`}
                    style={{ cursor: "pointer", background: expanded ? "var(--surface-3)" : undefined }}
                  >
                    <td style={{ color: "var(--ink-muted)", textAlign: "center" }} aria-hidden="true">
                      {expanded ? "▾" : "▸"}
                    </td>
                    <td className="tnum" style={{ fontWeight: 700 }}>{num(condor.level)}</td>
                    <td>
                      <Badge tone={condor.side === "up" ? "warn" : condor.side === "anchor" ? "brand" : "neutral"}>
                        {(condor.side ?? "down").toUpperCase()}
                      </Badge>
                    </td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(condor.entry_time)}</td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(condor.expiry)}</td>
                    <td className="tnum" style={{ textAlign: "right" }}>{inr(condor.credit)}</td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {inr(-condor.max_loss)}
                    </td>
                    <td
                      className="tnum"
                      style={{ textAlign: "right", fontWeight: 700, color: pnlColour(condor.status === "OPEN" ? null : condor.pnl) }}
                    >
                      {condor.status === "OPEN" ? "--" : inr(condor.pnl, { sign: true })}
                    </td>
                    <td>
                      <Badge tone={outcome.tone}>{outcome.label}</Badge>
                    </td>
                  </tr>

                  {expanded && (
                    <tr>
                      <td colSpan={8} style={{ padding: 0, background: "var(--surface-3)" }}>
                        <div style={{ padding: "10px 14px 14px 40px" }}>
                          <table style={{ width: "100%" }}>
                            <thead>
                              <tr>
                                <th>Side</th>
                                <th style={{ textAlign: "right" }}>Strike</th>
                                <th style={{ textAlign: "right" }}>Qty</th>
                                <th style={{ textAlign: "right" }}>Entry</th>
                                <th style={{ textAlign: "right" }}>Exit</th>
                                <th style={{ textAlign: "right" }}>Leg P&amp;L</th>
                                <th>Source</th>
                              </tr>
                            </thead>
                            <tbody>
                              {condor.legs.map((leg, i) => {
                                const legPnl =
                                  leg.exit_price == null
                                    ? null
                                    : (leg.exit_price - leg.entry_price) * leg.signed_qty;
                                return (
                                  <tr key={i}>
                                    <td>
                                      <Badge tone={leg.side === "SELL" ? "warn" : "brand"}>
                                        {leg.side} {leg.right}
                                      </Badge>
                                    </td>
                                    <td className="tnum" style={{ textAlign: "right", fontWeight: 600 }}>
                                      {num(leg.strike)}
                                    </td>
                                    <td className="tnum" style={{ textAlign: "right" }}>{num(leg.qty)}</td>
                                    <td className="tnum" style={{ textAlign: "right" }}>
                                      {num(leg.entry_price, 2)}
                                    </td>
                                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                                      {leg.exit_price == null ? "--" : num(leg.exit_price, 2)}
                                    </td>
                                    <td
                                      className="tnum"
                                      style={{ textAlign: "right", fontWeight: 600, color: pnlColour(legPnl) }}
                                    >
                                      {legPnl == null ? "--" : inr(legPnl, { sign: true })}
                                    </td>
                                    <td>
                                      <Badge tone={leg.source === "choice" ? "pos" : "warn"}>
                                        {leg.source === "choice" ? "CHOICE" : "MODELED"}
                                      </Badge>
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>

                          {/* Why the four leg figures do not add up to the P&L above. */}
                          <p
                            className="tnum"
                            style={{
                              margin: "10px 0 0", fontSize: 12, color: "var(--ink-2)",
                              display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline",
                            }}
                          >
                            {gross == null ? (
                              <span style={{ color: "var(--ink-muted)" }}>
                                Still open — leg P&amp;L settles when the condor closes.
                              </span>
                            ) : (
                              <>
                                <span>Legs {inr(gross, { sign: true })}</span>
                                <span style={{ color: "var(--ink-muted)" }}>&minus;</span>
                                <span>costs {inr(costs)}</span>
                                <span style={{ color: "var(--ink-muted)" }}>=</span>
                                <strong style={{ color: pnlColour(condor.pnl) }}>
                                  {inr(condor.pnl, { sign: true })}
                                </strong>
                                <span style={{ color: "var(--ink-muted)", fontSize: 11.5 }}>
                                  &mdash; leg figures are gross, so they always read better than the truth
                                </span>
                              </>
                            )}
                          </p>

                          {condor.exit_reason && (
                            <p style={{ margin: "6px 0 0", fontSize: 11.5, color: "var(--ink-muted)" }}>
                              Closed: {condor.exit_reason}
                            </p>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
