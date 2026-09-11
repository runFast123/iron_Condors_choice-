"use client";

import { Fragment, useMemo, useState } from "react";
import type { LiveFill, LivePosition, LiveState } from "@/lib/live";
import { dateTime, inr, num } from "@/lib/format";
import { Badge } from "@/components/ui";

/**
 * The forward run's fills, grouped by condor, with the legs one click away.
 *
 * Same shape as the Trades page blotter, for the same reason: a flat list of
 * leg fills makes the reader find four rows scattered among eight and add them
 * up to answer the only question they have -- what did this condor do. Here
 * there are twice as many rows to sift, because a closed condor writes four
 * fills going in and four coming out.
 *
 * What differs from the backtest blotter is that these condors are live. An
 * open one carries a mark, not a result, and the two must never be read as the
 * same number.
 */

const OUTCOME: Record<string, { label: string; tone: "pos" | "neg" | "brand" | "neutral" }> = {
  OPEN: { label: "Open", tone: "brand" },
  EXPIRED: { label: "Held to expiry", tone: "neutral" },
  CLOSED_TARGET: { label: "Take-profit", tone: "pos" },
  CLOSED_STOP: { label: "Stop-loss", tone: "neg" },
};

function pnlColour(value: number | null): string {
  if (value == null) return "var(--ink)";
  return value > 0 ? "var(--pos)" : value < 0 ? "var(--neg)" : "var(--ink)";
}

/**
 * When the market did the thing that caused this trade, not when the engine
 * got round to it.
 *
 * Two different instants, and showing only the engine's is what made the log
 * disagree with the chart. The index is served from candles rather than the
 * live book, so a rung fires off a spot that printed earlier — and one tick
 * can open two condors a third of a second apart that the market crossed a
 * session apart. The engine's clock is kept for the audit trail, in the
 * tooltip, since that is what the run log and the broker's own records show.
 */
function tradedAt(fill: LiveFill): { shown: string; recorded: string | null } {
  if (!fill.market_ts || fill.market_ts === fill.ts) {
    return { shown: fill.ts, recorded: null };
  }
  return { shown: fill.market_ts, recorded: fill.ts };
}

/** How long after the market printed the engine acted, in seconds. */
function lagSeconds(fill: LiveFill): number | null {
  if (!fill.market_ts) return null;
  const gap = (Date.parse(fill.ts) - Date.parse(fill.market_ts)) / 1000;
  return Number.isFinite(gap) && gap > 0 ? gap : null;
}

/** "42s" / "7m" / "2h" — a lag worth reading at a glance, not to the second. */
function lagLabel(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export function LiveCondorBlotter({
  positions,
  fills,
  marks,
  pnl,
}: {
  positions: LivePosition[];
  fills: LiveFill[];
  marks: Record<string, number>;
  pnl: LiveState["pnl"];
}) {
  const [open, setOpen] = useState<Set<number>>(new Set());

  /**
   * Fills belonging to each condor, oldest first.
   *
   * Grouped off the positions list rather than off the fills, so a condor is
   * never missing from the history just because its fills were. The engine
   * sends fills newest-first for the raw log; within one condor the entry
   * should come before the exit or the story reads backwards.
   */
  const groups = useMemo(() => {
    const byCondor = new Map<number, LiveFill[]>();
    for (const f of fills) {
      const bucket = byCondor.get(f.condor_index);
      if (bucket) bucket.push(f);
      else byCondor.set(f.condor_index, [f]);
    }
    for (const bucket of byCondor.values()) {
      bucket.sort((a, b) => a.ts.localeCompare(b.ts));
    }
    return positions
      .map((p) => {
        const legs = byCondor.get(p.index) ?? [];
        // The condor opened when its legs traded, not when the engine wrote
        // the record. Taken from the first entry leg so the header time and
        // the leg times below it cannot disagree.
        const firstOpen = legs.find((f) => f.action === "OPEN");
        return {
          condor: p,
          legs,
          openedAt: (firstOpen && firstOpen.market_ts) || p.entry_time,
        };
      })
      .sort((a, b) => b.openedAt.localeCompare(a.openedAt));
  }, [positions, fills]);

  const counts = useMemo(() => {
    // Off `status`, which every other surface reads, rather than `is_open`.
    // The engine derives one from the other so they cannot disagree, and this
    // way a payload missing the newer field still counts correctly.
    const closed = positions.filter((p) => p.status !== "OPEN");
    return {
      openNow: positions.length - closed.length,
      wins: closed.filter((p) => (p.pnl ?? 0) > 0).length,
      losses: closed.filter((p) => (p.pnl ?? 0) < 0).length,
    };
  }, [positions]);

  function toggle(index: number) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  const allOpen = open.size === groups.length && groups.length > 0;

  /** Where this contract trades now, or null if it has not been marked. */
  const markOf = (f: LiveFill): number | null =>
    f.token != null && marks[String(f.token)] != null ? marks[String(f.token)] : null;

  /**
   * Green when the move since the fill helped, red when it hurt.
   *
   * Which direction is "good" depends on the side, not on whether the number
   * went up: a short leg gains when its premium falls. Colouring by raw
   * direction would tell half the rows the opposite of the truth.
   */
  const cmpColour = (f: LiveFill): string => {
    const mark = markOf(f);
    if (mark == null) return "var(--ink-muted)";
    const move = mark - f.price;
    const favourable = f.side === "SELL" ? -move : move;
    if (Math.abs(move) < 0.005) return "var(--ink)";
    return favourable > 0 ? "var(--pos)" : "var(--neg)";
  };

  const cmpTitle = (f: LiveFill): string => {
    const mark = markOf(f);
    if (mark == null) return "Not marked yet — the next tick will price it.";
    const move = mark - f.price;
    const per = move >= 0 ? `+${move.toFixed(2)}` : move.toFixed(2);
    const total = (f.side === "SELL" ? -move : move) * f.qty;
    return `${per} per share since the fill — ${total >= 0 ? "+" : ""}${total.toFixed(0)} on this leg`;
  };

  if (groups.length === 0) {
    return (
      <p style={{ padding: "22px 16px", textAlign: "center", fontSize: 12.5, color: "var(--ink-muted)" }}>
        No fills yet. Trade history appears here once a forward run opens its first condor.
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
          <strong>{groups.length}</strong> condors
        </span>
        {counts.wins > 0 && <span style={{ color: "var(--pos)" }}>{counts.wins} won</span>}
        {counts.losses > 0 && <span style={{ color: "var(--neg)" }}>{counts.losses} lost</span>}
        {counts.openNow > 0 && <span style={{ color: "var(--ink-muted)" }}>{counts.openNow} open</span>}

        <span className="tnum" style={{ marginLeft: "auto", fontWeight: 700, color: pnlColour(pnl.total) }}>
          {inr(pnl.total, { sign: true })}
          <span style={{ fontWeight: 500, color: "var(--ink-muted)", marginLeft: 8, fontSize: 11.5 }}>
            {inr(pnl.realised, { sign: true })} realised, {inr(pnl.unrealised, { sign: true })} on open marks
          </span>
        </span>

        <button
          type="button"
          onClick={() => setOpen(allOpen ? new Set() : new Set(groups.map((g) => g.condor.index)))}
          className="btn-quiet"
          style={{ fontSize: 11.5, padding: "4px 10px" }}
        >
          {allOpen ? "Collapse all" : "Expand all"}
        </button>
      </div>

      {/* Stated once here rather than on every row that shows a dash. */}
      {(pnl.unmarked_condors ?? 0) > 0 && (
        <p style={{ margin: 0, padding: "8px 14px", fontSize: 11.5, color: "var(--warn)", borderBottom: "1px solid var(--border)" }}>
          {pnl.unmarked_condors} open condor{(pnl.unmarked_condors ?? 0) === 1 ? "" : "s"} ha
          {(pnl.unmarked_condors ?? 0) === 1 ? "s" : "ve"} no mark yet, so
          {(pnl.unmarked_condors ?? 0) === 1 ? " it is" : " they are"} excluded from the total above.
        </p>
      )}

      <div className="scroll-x" style={{ maxHeight: "70vh", overflowY: "auto" }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 28 }}><span className="sr-only">Expand</span></th>
              <th>Condor</th>
              <th>Opened</th>
              <th style={{ textAlign: "right" }}>Legs</th>
              <th style={{ textAlign: "right" }}>Credit</th>
              <th style={{ textAlign: "right" }}>P&amp;L</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {groups.map(({ condor, legs, openedAt }) => {
              const expanded = open.has(condor.index);
              const isOpen = condor.status === "OPEN";
              const outcome = OUTCOME[condor.status] ?? OUTCOME.OPEN;
              const closes = legs.filter((f) => f.action === "CLOSE").length;

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
                    aria-label={`Condor at ${num(condor.level)}, ${expanded ? "hide" : "show"} its fills`}
                    style={{ cursor: "pointer", background: expanded ? "var(--surface-3)" : undefined }}
                  >
                    <td style={{ color: "var(--ink-muted)", textAlign: "center" }} aria-hidden="true">
                      {expanded ? "▾" : "▸"}
                    </td>
                    <td className="tnum" style={{ fontWeight: 700 }}>{num(condor.level)}</td>
                    <td
                      style={{ color: "var(--ink-2)" }}
                      title={
                        openedAt === condor.entry_time
                          ? "When this condor opened."
                          : `Market reached this rung at ${dateTime(openedAt)}; the engine opened the condor at ${dateTime(condor.entry_time)}`
                      }
                    >
                      {dateTime(openedAt)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {legs.length === 0 ? "--" : closes > 0 ? `${legs.length} (in + out)` : num(legs.length)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right" }}>{inr(condor.credit)}</td>
                    <td
                      className="tnum"
                      style={{ textAlign: "right", fontWeight: 700, color: pnlColour(condor.pnl) }}
                      title={
                        condor.pnl == null
                          ? "No mark yet — the next tick will price this condor."
                          : isOpen
                            ? "Marked to market as of the last tick. Nothing is banked until it closes."
                            : "Realised, net of entry and exit costs."
                      }
                    >
                      {condor.pnl == null ? "--" : inr(condor.pnl, { sign: true })}
                      {isOpen && condor.pnl != null && (
                        <span style={{ fontWeight: 500, fontSize: 10.5, color: "var(--ink-muted)", marginLeft: 5 }}>
                          mark
                        </span>
                      )}
                    </td>
                    <td>
                      <Badge tone={outcome.tone}>{outcome.label}</Badge>
                    </td>
                  </tr>

                  {expanded && (
                    <tr>
                      <td colSpan={7} style={{ padding: 0, background: "var(--surface-3)" }}>
                        <div style={{ padding: "10px 14px 14px 40px" }}>
                          {legs.length === 0 ? (
                            <p style={{ margin: 0, fontSize: 12, color: "var(--ink-muted)" }}>
                              No fill records for this condor.
                            </p>
                          ) : (
                            <table style={{ width: "100%" }}>
                              <thead>
                                <tr>
                                  <th>Time</th>
                                  <th>Action</th>
                                  <th>Side</th>
                                  <th style={{ textAlign: "right" }}>Strike</th>
                                  <th style={{ textAlign: "right" }}>Qty</th>
                                  <th style={{ textAlign: "right" }}>Price</th>
                                  <th style={{ textAlign: "right" }}>Value</th>
                                  <th style={{ textAlign: "right" }}>CMP</th>
                                  <th>Token</th>
                                  <th>Mode</th>
                                </tr>
                              </thead>
                              <tbody>
                                {legs.map((f, i) => {
                                  const when = tradedAt(f);
                                  const lag = lagSeconds(f);
                                  return (
                                  <tr key={i}>
                                    <td
                                      style={{ color: "var(--ink-2)" }}
                                      title={
                                        when.recorded
                                          ? `Market printed this at ${dateTime(when.shown)}; the engine filled it at ${dateTime(when.recorded)}`
                                          : "The market data behind this fill was current, so this is both when it traded and when it was recorded."
                                      }
                                    >
                                      {dateTime(when.shown)}
                                      {lag != null && lag >= 1 && (
                                        <span style={{ color: "var(--ink-muted)", fontSize: 10.5, marginLeft: 5 }}>
                                          +{lagLabel(lag)}
                                        </span>
                                      )}
                                    </td>
                                    <td>
                                      <Badge tone={f.action === "OPEN" ? "brand" : "neutral"}>{f.action}</Badge>
                                    </td>
                                    <td>
                                      <Badge tone={f.side === "SELL" ? "warn" : "brand"}>
                                        {f.side} {f.right}
                                      </Badge>
                                    </td>
                                    <td className="tnum" style={{ textAlign: "right", fontWeight: 600 }}>
                                      {num(f.strike)}
                                    </td>
                                    <td className="tnum" style={{ textAlign: "right" }}>{num(f.qty)}</td>
                                    <td className="tnum" style={{ textAlign: "right" }}>{f.price.toFixed(2)}</td>
                                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                                      {inr(f.price * f.qty)}
                                    </td>
                                    <td
                                      className="tnum"
                                      style={{ textAlign: "right", fontWeight: 600, color: cmpColour(f) }}
                                      title={cmpTitle(f)}
                                    >
                                      {markOf(f) == null ? "--" : num(markOf(f) as number, 2)}
                                    </td>
                                    <td className="tnum mono" style={{ fontSize: 11, color: "var(--ink-muted)" }}>
                                      {f.token ?? "--"}
                                    </td>
                                    <td>
                                      <Badge tone={f.mode === "live" ? "neg" : "neutral"}>{f.mode}</Badge>
                                    </td>
                                  </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          )}

                          {condor.exit_reason && (
                            <p style={{ margin: "8px 0 0", fontSize: 11.5, color: "var(--ink-muted)" }}>
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
