"use client";

import { useState } from "react";
import type { BacktestHistoryRun } from "@/lib/types";
import { inr, num, pct, ratio } from "@/lib/format";

interface BacktestHistoryComparisonProps {
  runs: BacktestHistoryRun[];
  currentRunId?: string | null;
  onSelectRun?: (runId: string) => void;
  onPopulateParams?: (params: Record<string, unknown>) => void;
}

export function BacktestHistoryComparison({
  runs,
  currentRunId,
  onSelectRun,
  onPopulateParams,
}: BacktestHistoryComparisonProps) {
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [compareModalOpen, setCompareModalOpen] = useState(false);
  const [filterStrategy, setFilterStrategy] = useState<string>("all");

  if (!runs || runs.length === 0) {
    return null;
  }

  const filteredRuns = runs.filter((r) => {
    if (filterStrategy === "all") return true;
    return (r.strategy_id || "ladder").toLowerCase() === filterStrategy.toLowerCase();
  });

  const toggleSelect = (runId: string) => {
    setSelectedIds((prev) =>
      prev.includes(runId)
        ? prev.filter((id) => id !== runId)
        : prev.length < 4
        ? [...prev, runId]
        : prev
    );
  };

  const selectedRuns = runs.filter((r) => selectedIds.includes(r.run_id));

  // Determine winners for comparison
  const highestPnlRunId = selectedRuns.reduce((best, curr) => {
    const pnl = curr.summary?.net_pnl ?? -Infinity;
    const bestPnl = best?.summary?.net_pnl ?? -Infinity;
    return pnl > bestPnl ? curr : best;
  }, selectedRuns[0])?.run_id;

  const lowestDdRunId = selectedRuns.reduce((best, curr) => {
    const dd = Math.abs(curr.summary?.max_drawdown ?? Infinity);
    const bestDd = Math.abs(best?.summary?.max_drawdown ?? Infinity);
    return dd < bestDd ? curr : best;
  }, selectedRuns[0])?.run_id;

  const highestWinRateRunId = selectedRuns.reduce((best, curr) => {
    const wr = curr.summary?.win_rate ?? -1;
    const bestWr = best?.summary?.win_rate ?? -1;
    return wr > bestWr ? curr : best;
  }, selectedRuns[0])?.run_id;

  return (
    <section
      className="card"
      style={{
        border: "1px solid var(--border)",
        background: "var(--surface)",
        borderRadius: 8,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "12px 16px",
          borderBottom: "1px solid var(--border)",
          flexWrap: "wrap",
          gap: 12,
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h2 style={{ margin: 0, fontSize: 13.5, fontWeight: 600 }}>
              Backtest History &amp; Run Comparison
            </h2>
            <span
              style={{
                fontSize: 11,
                padding: "2px 7px",
                borderRadius: 999,
                background: "var(--surface-2)",
                color: "var(--ink-muted)",
                fontWeight: 600,
              }}
            >
              {runs.length} {runs.length === 1 ? "run" : "runs"} saved
            </span>
          </div>
          <p style={{ margin: "3px 0 0", fontSize: 11.5, color: "var(--ink-muted)" }}>
            Compare past backtests, evaluate condition variations, or switch the dashboard to view any historical run.
          </p>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          {/* Strategy filter */}
          <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12 }}>
            <span style={{ color: "var(--ink-muted)" }}>Strategy:</span>
            <select
              value={filterStrategy}
              onChange={(e) => setFilterStrategy(e.target.value)}
              style={{
                background: "var(--surface-2)",
                border: "1px solid var(--border)",
                color: "var(--ink)",
                borderRadius: 4,
                padding: "3px 7px",
                fontSize: 12,
              }}
            >
              <option value="all">All ({runs.length})</option>
              <option value="ladder">Ladder</option>
              <option value="hic">HIC</option>
            </select>
          </div>

          {selectedIds.length >= 2 && (
            <button
              onClick={() => setCompareModalOpen(true)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "5px 12px",
                fontSize: 12,
                fontWeight: 600,
                color: "#fff",
                background: "var(--brand, #2563eb)",
                border: "none",
                borderRadius: 5,
                cursor: "pointer",
                boxShadow: "0 1px 4px rgba(37, 99, 235, 0.4)",
              }}
            >
              Compare Selected ({selectedIds.length})
            </button>
          )}

          {selectedIds.length > 0 && (
            <button
              onClick={() => setSelectedIds([])}
              style={{
                background: "none",
                border: "none",
                color: "var(--ink-muted)",
                fontSize: 11.5,
                cursor: "pointer",
                textDecoration: "underline",
              }}
            >
              Clear selection
            </button>
          )}
        </div>
      </div>

      {/* History Runs Table */}
      <div className="scroll-x" style={{ maxHeight: 380, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ background: "var(--surface-2)", color: "var(--ink-muted)", textAlign: "left", fontSize: 11 }}>
              <th style={{ padding: "8px 12px", width: 36, textAlign: "center" }}>Comp</th>
              <th style={{ padding: "8px 12px" }}>Run Date</th>
              <th style={{ padding: "8px 12px" }}>Strategy &amp; Cadence</th>
              <th style={{ padding: "8px 12px" }}>Conditions</th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>Net P&amp;L</th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>Win Rate</th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>Profit Factor</th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>Max Drawdown</th>
              <th style={{ padding: "8px 12px", textAlign: "center" }}>Condors</th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredRuns.map((r, i) => {
              const s = r.summary;
              const p = r.params || {};
              const isActive = currentRunId ? r.run_id === currentRunId : i === 0;
              const isChecked = selectedIds.includes(r.run_id);
              const pnl = s?.net_pnl ?? 0;
              const isPos = pnl > 0;
              const isNeg = pnl < 0;

              const runDate = new Date(r.created_at);
              const dateStr = runDate.toLocaleDateString("en-IN", {
                day: "2-digit",
                month: "short",
                year: "2-digit",
              });
              const timeStr = runDate.toLocaleTimeString("en-IN", {
                hour: "2-digit",
                minute: "2-digit",
              });

              return (
                <tr
                  key={r.run_id}
                  style={{
                    borderBottom: "1px solid var(--border)",
                    background: isActive ? "rgba(59, 130, 246, 0.08)" : "transparent",
                    transition: "background 0.15s ease",
                  }}
                >
                  {/* Select Checkbox */}
                  <td style={{ padding: "8px 12px", textAlign: "center" }}>
                    <input
                      type="checkbox"
                      checked={isChecked}
                      disabled={!isChecked && selectedIds.length >= 4}
                      onChange={() => toggleSelect(r.run_id)}
                      title="Select to compare side-by-side (up to 4 runs)"
                      style={{ cursor: "pointer" }}
                    />
                  </td>

                  {/* Run Date */}
                  <td style={{ padding: "8px 12px", whiteSpace: "nowrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      {isActive && (
                        <span
                          style={{
                            display: "inline-block",
                            width: 6,
                            height: 6,
                            borderRadius: "50%",
                            background: "var(--brand, #3b82f6)",
                            boxShadow: "0 0 6px var(--brand, #3b82f6)",
                          }}
                          title="Currently loaded on dashboard"
                        />
                      )}
                      <div>
                        <div style={{ fontWeight: 600, color: "var(--ink)" }}>{dateStr}</div>
                        <div style={{ fontSize: 10.5, color: "var(--ink-muted)" }}>{timeStr}</div>
                      </div>
                    </div>
                  </td>

                  {/* Strategy & Cadence */}
                  <td style={{ padding: "8px 12px", whiteSpace: "nowrap" }}>
                    <div style={{ fontWeight: 600, textTransform: "capitalize" }}>
                      {r.strategy_id || "Ladder"}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--ink-muted)" }}>
                      {String(p.expiry_cadence || "monthly")} · {p.resolution === "D" ? "daily" : `${p.resolution || "D"}m`}
                    </div>
                  </td>

                  {/* Conditions Chips */}
                  <td style={{ padding: "8px 12px" }}>
                    <div style={{ display: "flex", gap: 4, flexWrap: "wrap", maxWidth: 220 }}>
                      <span
                        style={{
                          fontSize: 10.5,
                          padding: "1px 5px",
                          borderRadius: 3,
                          background: "var(--surface-2)",
                          color: "var(--ink)",
                        }}
                      >
                        {p.step ? `${p.step}pt` : "100pt"}
                      </span>
                      <span
                        style={{
                          fontSize: 10.5,
                          padding: "1px 5px",
                          borderRadius: 3,
                          background: "var(--surface-2)",
                          color: p.direction === "both" ? "#38bdf8" : "var(--ink)",
                        }}
                      >
                        {String(p.direction || "down")}
                      </span>
                      {p.max_entry_vix ? (
                        <span
                          style={{
                            fontSize: 10.5,
                            padding: "1px 5px",
                            borderRadius: 3,
                            background: "rgba(52, 211, 153, 0.15)",
                            color: "#34d399",
                            fontWeight: 600,
                          }}
                        >
                          VIX &le; {String(p.max_entry_vix)}
                        </span>
                      ) : null}
                      {p.take_profit ? (
                        <span
                          style={{
                            fontSize: 10.5,
                            padding: "1px 5px",
                            borderRadius: 3,
                            background: "var(--surface-2)",
                            color: "var(--ink-muted)",
                          }}
                        >
                          {Math.round(Number(p.take_profit) * 100)}% TP
                        </span>
                      ) : null}
                    </div>
                  </td>

                  {/* Net P&L */}
                  <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
                    {s ? (
                      <span
                        className="mono"
                        style={{
                          fontWeight: 700,
                          color: isPos ? "var(--pos, #22c55e)" : isNeg ? "var(--neg, #ef4444)" : "var(--ink)",
                        }}
                      >
                        {inr(pnl, { sign: true })}
                      </span>
                    ) : (
                      <span style={{ color: "var(--ink-muted)" }}>-</span>
                    )}
                  </td>

                  {/* Win Rate */}
                  <td style={{ padding: "8px 12px", textAlign: "right" }}>
                    {s ? <span className="mono">{pct(s.win_rate)}</span> : <span style={{ color: "var(--ink-muted)" }}>-</span>}
                  </td>

                  {/* Profit Factor */}
                  <td style={{ padding: "8px 12px", textAlign: "right" }}>
                    {s ? (
                      <span
                        className="mono"
                        style={{
                          color: s.profit_factor && s.profit_factor >= 1 ? "var(--pos, #22c55e)" : "var(--ink)",
                        }}
                      >
                        {s.profit_factor == null && s.wins && s.wins > 0 && s.losses === 0
                          ? "∞"
                          : ratio(s.profit_factor)}
                      </span>
                    ) : (
                      <span style={{ color: "var(--ink-muted)" }}>-</span>
                    )}
                  </td>

                  {/* Max Drawdown */}
                  <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
                    {s ? (
                      <span className="mono" style={{ color: s.max_drawdown < 0 ? "var(--neg, #ef4444)" : "var(--ink)" }}>
                        {inr(s.max_drawdown)}
                      </span>
                    ) : (
                      <span style={{ color: "var(--ink-muted)" }}>-</span>
                    )}
                  </td>

                  {/* Condors */}
                  <td style={{ padding: "8px 12px", textAlign: "center" }}>
                    {s ? (
                      <span className="mono">
                        {s.condors} {s.open_positions ? `(+${s.open_positions} open)` : ""}
                      </span>
                    ) : (
                      <span style={{ color: "var(--ink-muted)" }}>-</span>
                    )}
                  </td>

                  {/* Actions */}
                  <td style={{ padding: "8px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
                    <div style={{ display: "flex", justifyContent: "flex-end", gap: 6 }}>
                      {!isActive && (
                        <a
                          href={`/?run=${encodeURIComponent(r.run_id)}`}
                          style={{
                            padding: "3px 8px",
                            fontSize: 11,
                            fontWeight: 600,
                            borderRadius: 4,
                            background: "var(--surface-2)",
                            color: "var(--ink)",
                            textDecoration: "none",
                            border: "1px solid var(--border)",
                          }}
                        >
                          View
                        </a>
                      )}
                      {isActive && (
                        <span
                          style={{
                            padding: "3px 8px",
                            fontSize: 11,
                            fontWeight: 600,
                            borderRadius: 4,
                            background: "rgba(59, 130, 246, 0.2)",
                            color: "var(--brand, #3b82f6)",
                            border: "1px solid rgba(59, 130, 246, 0.4)",
                          }}
                        >
                          Active
                        </span>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Comparison Modal Drawer */}
      {compareModalOpen && selectedRuns.length >= 2 && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 9999,
            background: "rgba(0, 0, 0, 0.75)",
            backdropFilter: "blur(4px)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
          }}
          onClick={() => setCompareModalOpen(false)}
        >
          <div
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 10,
              width: "100%",
              maxWidth: 960,
              maxHeight: "90vh",
              overflowY: "auto",
              boxShadow: "0 20px 40px rgba(0, 0, 0, 0.6)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "16px 20px",
                borderBottom: "1px solid var(--border)",
              }}
            >
              <div>
                <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>
                  Backtest Run Comparison ({selectedRuns.length} Runs)
                </h3>
                <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--ink-muted)" }}>
                  Side-by-side analysis of rule conditions, financial returns, and risk metrics
                </p>
              </div>
              <button
                onClick={() => setCompareModalOpen(false)}
                style={{
                  background: "var(--surface-2)",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  color: "var(--ink)",
                  padding: "6px 12px",
                  fontSize: 12,
                  cursor: "pointer",
                }}
              >
                ✕ Close
              </button>
            </div>

            {/* Comparison Table */}
            <div style={{ padding: 20 }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                <thead>
                  <tr style={{ borderBottom: "2px solid var(--border)" }}>
                    <th style={{ padding: "10px 12px", textAlign: "left", width: 220, color: "var(--ink-muted)" }}>
                      Metric / Condition
                    </th>
                    {selectedRuns.map((r, idx) => (
                      <th
                        key={r.run_id}
                        style={{
                          padding: "10px 12px",
                          textAlign: "right",
                          background: idx % 2 === 0 ? "rgba(255, 255, 255, 0.02)" : "transparent",
                        }}
                      >
                        <div style={{ fontWeight: 700, fontSize: 13, color: "var(--ink)" }}>
                          Run #{idx + 1}
                        </div>
                        <div style={{ fontSize: 11, color: "var(--ink-muted)", fontWeight: 400 }}>
                          {new Date(r.created_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" })} ·{" "}
                          {r.strategy_id || "Ladder"}
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {/* Category: Financial Results */}
                  <tr style={{ background: "var(--surface-2)" }}>
                    <td colSpan={selectedRuns.length + 1} style={{ padding: "6px 12px", fontWeight: 700, fontSize: 11, color: "var(--brand, #3b82f6)", textTransform: "uppercase" }}>
                      Performance &amp; Profitability
                    </td>
                  </tr>

                  {/* Net P&L */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", fontWeight: 600 }}>Net P&amp;L (Closed)</td>
                    {selectedRuns.map((r) => {
                      const pnl = r.summary?.net_pnl ?? 0;
                      const isWinner = r.run_id === highestPnlRunId && selectedRuns.length > 1;
                      return (
                        <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                          <div className="mono" style={{ fontSize: 14, fontWeight: 700, color: pnl >= 0 ? "var(--pos, #22c55e)" : "var(--neg, #ef4444)" }}>
                            {inr(pnl, { sign: true })}
                          </div>
                          {isWinner && (
                            <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "rgba(34, 197, 94, 0.2)", color: "#22c55e", fontWeight: 700 }}>
                              BEST P&amp;L
                            </span>
                          )}
                        </td>
                      );
                    })}
                  </tr>

                  {/* Win Rate */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Win Rate</td>
                    {selectedRuns.map((r) => {
                      const wr = r.summary?.win_rate ?? 0;
                      const isWinner = r.run_id === highestWinRateRunId && selectedRuns.length > 1;
                      return (
                        <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                          <span className="mono" style={{ fontWeight: 600 }}>{pct(wr)}</span>{" "}
                          <span style={{ fontSize: 11, color: "var(--ink-muted)" }}>({r.summary?.wins}W / {r.summary?.losses}L)</span>
                          {isWinner && (
                            <div style={{ fontSize: 10, color: "#34d399", fontWeight: 700 }}>Highest Win Rate</div>
                          )}
                        </td>
                      );
                    })}
                  </tr>

                  {/* Profit Factor */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Profit Factor</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        <span className="mono" style={{ fontWeight: 600, color: r.summary?.profit_factor && r.summary.profit_factor >= 1 ? "var(--pos, #22c55e)" : "var(--ink)" }}>
                          {r.summary?.profit_factor == null && (r.summary?.wins ?? 0) > 0 && (r.summary?.losses ?? 0) === 0 ? "∞" : ratio(r.summary?.profit_factor)}
                        </span>
                      </td>
                    ))}
                  </tr>

                  {/* Max Drawdown */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Max Drawdown</td>
                    {selectedRuns.map((r) => {
                      const dd = r.summary?.max_drawdown ?? 0;
                      const isWinner = r.run_id === lowestDdRunId && selectedRuns.length > 1;
                      return (
                        <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                          <span className="mono" style={{ fontWeight: 600, color: dd < 0 ? "var(--neg, #ef4444)" : "var(--ink)" }}>
                            {inr(dd)}
                          </span>
                          {isWinner && (
                            <div style={{ fontSize: 10, color: "#38bdf8", fontWeight: 700 }}>Lowest Drawdown</div>
                          )}
                        </td>
                      );
                    })}
                  </tr>

                  {/* Total Credit Collected */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Total Credit Collected</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        <span className="mono">{inr(r.summary?.total_credit ?? 0)}</span>
                      </td>
                    ))}
                  </tr>

                  {/* Transaction Costs */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Transaction Costs</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        <span className="mono">{inr(r.summary?.total_costs ?? 0)}</span>
                      </td>
                    ))}
                  </tr>

                  {/* Category: Strategy Conditions */}
                  <tr style={{ background: "var(--surface-2)" }}>
                    <td colSpan={selectedRuns.length + 1} style={{ padding: "6px 12px", fontWeight: 700, fontSize: 11, color: "var(--brand, #3b82f6)", textTransform: "uppercase" }}>
                      Strategy Rules &amp; Input Conditions
                    </td>
                  </tr>

                  {/* Step & Direction */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Step &amp; Direction</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        <span style={{ fontWeight: 600 }}>{num(Number(r.params?.step || 100))} pts</span> · {String(r.params?.direction || "down")}
                      </td>
                    ))}
                  </tr>

                  {/* Anchor Mode */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Anchor Mode</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        {String(r.params?.anchor_mode || "nearest")}
                      </td>
                    ))}
                  </tr>

                  {/* Max VIX Filter */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>India VIX Filter</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        {r.params?.max_entry_vix != null ? (
                          <span style={{ color: "#34d399", fontWeight: 600 }}>&le; {String(r.params.max_entry_vix)}</span>
                        ) : (
                          <span style={{ color: "var(--ink-muted)" }}>Disabled</span>
                        )}
                      </td>
                    ))}
                  </tr>

                  {/* Expiry Cadence */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Cadence &amp; Resolution</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        {String(r.params?.expiry_cadence || "monthly")} · {r.summary?.resolution === "D" ? "Daily" : `${r.summary?.resolution || "D"}m`}
                      </td>
                    ))}
                  </tr>

                  {/* Condors Opened */}
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "10px 12px", color: "var(--ink-muted)" }}>Condors Opened</td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "10px 12px", textAlign: "right" }}>
                        <span className="mono" style={{ fontWeight: 600 }}>{r.summary?.condors ?? 0}</span>
                      </td>
                    ))}
                  </tr>

                  {/* Actions Row */}
                  <tr>
                    <td style={{ padding: "14px 12px" }}></td>
                    {selectedRuns.map((r) => (
                      <td key={r.run_id} style={{ padding: "14px 12px", textAlign: "right" }}>
                        <a
                          href={`/?run=${encodeURIComponent(r.run_id)}`}
                          style={{
                            display: "inline-block",
                            padding: "6px 14px",
                            fontSize: 12,
                            fontWeight: 600,
                            borderRadius: 6,
                            background: "var(--brand, #2563eb)",
                            color: "#fff",
                            textDecoration: "none",
                          }}
                        >
                          View Full Dashboard
                        </a>
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
