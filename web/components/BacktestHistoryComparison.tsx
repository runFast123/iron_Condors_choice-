"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import type { BacktestHistoryRun } from "@/lib/types";
import { inr, num, pct, ratio } from "@/lib/format";
import { InfoTooltip } from "./InfoTooltip";

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
  const router = useRouter();
  const [runList, setRunList] = useState<BacktestHistoryRun[]>(runs);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [compareModalOpen, setCompareModalOpen] = useState(false);
  const [filterStrategy, setFilterStrategy] = useState<string>("all");
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [confirmDeleteRun, setConfirmDeleteRun] = useState<BacktestHistoryRun | null>(null);
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);

  useEffect(() => {
    setRunList(runs);
  }, [runs]);

  if (!runList || runList.length === 0) {
    return null;
  }

  const filteredRuns = runList.filter((r) => {
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

  const selectedRuns = runList.filter((r) => selectedIds.includes(r.run_id));

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

  const handleDeleteRuns = async (runIds: string[]) => {
    if (!runIds || runIds.length === 0) return;
    setIsDeleting(true);
    setDeleteError(null);
    try {
      const res = await fetch("/api/backtest/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_ids: runIds }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        throw new Error(data.error || "Failed to delete backtest run(s).");
      }

      setRunList((prev) => prev.filter((r) => !runIds.includes(r.run_id)));
      setSelectedIds((prev) => prev.filter((id) => !runIds.includes(id)));
      setConfirmDeleteRun(null);
      setConfirmBulkDelete(false);

      if (currentRunId && runIds.includes(currentRunId)) {
        router.push("/");
      } else {
        router.refresh();
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to delete backtest.";
      setDeleteError(msg);
    } finally {
      setIsDeleting(false);
    }
  };

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
              {runList.length} {runList.length === 1 ? "run" : "runs"} saved
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
              <option value="all">All ({runList.length})</option>
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
              type="button"
              onClick={() => setConfirmBulkDelete(true)}
              disabled={isDeleting}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                padding: "5px 10px",
                fontSize: 12,
                fontWeight: 600,
                color: "var(--neg, #ef4444)",
                background: "rgba(239, 68, 68, 0.08)",
                border: "1px solid rgba(239, 68, 68, 0.25)",
                borderRadius: 5,
                cursor: isDeleting ? "not-allowed" : "pointer",
                transition: "all 0.15s ease",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "rgba(239, 68, 68, 0.16)";
                e.currentTarget.style.borderColor = "rgba(239, 68, 68, 0.4)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "rgba(239, 68, 68, 0.08)";
                e.currentTarget.style.borderColor = "rgba(239, 68, 68, 0.25)";
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 6h18"/>
                <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
              </svg>
              Delete Selected ({selectedIds.length})
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

      {deleteError && (
        <div
          style={{
            margin: "12px 16px 0",
            padding: "8px 12px",
            borderRadius: 6,
            background: "rgba(239, 68, 68, 0.1)",
            border: "1px solid rgba(239, 68, 68, 0.25)",
            color: "var(--neg, #ef4444)",
            fontSize: 12,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <span>{deleteError}</span>
          <button
            onClick={() => setDeleteError(null)}
            style={{
              background: "none",
              border: "none",
              color: "inherit",
              fontSize: 14,
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>
      )}

      {/* History Runs Table */}
      <div className="scroll-x" style={{ maxHeight: 380, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            <tr style={{ background: "var(--surface-2)", color: "var(--ink-muted)", textAlign: "left", fontSize: 11 }}>
              <th style={{ padding: "8px 12px", width: 36, textAlign: "center" }}>Comp</th>
              <th style={{ padding: "8px 12px" }}>Run Date</th>
              <th style={{ padding: "8px 12px" }}>Strategy &amp; Cadence</th>
              <th style={{ padding: "8px 12px" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Conditions
                  <InfoTooltip
                    title="Strategy Conditions"
                    content="The exact set of parameters and guardrails configured for this test run."
                  />
                </span>
              </th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "flex-end" }}>
                  Net P&amp;L
                  <InfoTooltip
                    title="Net Realized P&L"
                    content="Cumulative net rupee (₹) profit or loss generated across all closed condors in the test."
                  />
                </span>
              </th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "flex-end" }}>
                  Win Rate
                  <InfoTooltip
                    title="Win Rate"
                    content="Percentage of closed condors that produced a positive net profit."
                  />
                </span>
              </th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "flex-end" }}>
                  Profit Factor
                  <InfoTooltip
                    title="Profit Factor"
                    content="Gross profits divided by gross losses. A ratio above 1.5 indicates a solid statistical edge."
                  />
                </span>
              </th>
              <th style={{ padding: "8px 12px", textAlign: "right" }}>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "flex-end" }}>
                  Max Drawdown
                  <InfoTooltip
                    title="Maximum Drawdown"
                    content="Worst peak-to-trough decline in portfolio equity experienced during the test."
                  />
                </span>
              </th>
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
                    <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 6 }}>
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
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setConfirmDeleteRun(r);
                        }}
                        disabled={isDeleting}
                        title="Delete backtest"
                        aria-label="Delete backtest"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          width: 26,
                          height: 24,
                          padding: 0,
                          borderRadius: 4,
                          background: "var(--surface-2)",
                          border: "1px solid var(--border)",
                          color: "var(--ink-muted)",
                          cursor: isDeleting ? "not-allowed" : "pointer",
                          transition: "all 0.15s ease",
                        }}
                        onMouseEnter={(e) => {
                          e.currentTarget.style.color = "var(--neg, #ef4444)";
                          e.currentTarget.style.borderColor = "rgba(239, 68, 68, 0.4)";
                          e.currentTarget.style.background = "rgba(239, 68, 68, 0.1)";
                        }}
                        onMouseLeave={(e) => {
                          e.currentTarget.style.color = "var(--ink-muted)";
                          e.currentTarget.style.borderColor = "var(--border)";
                          e.currentTarget.style.background = "var(--surface-2)";
                        }}
                      >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M3 6h18"/>
                          <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                          <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                          <line x1="10" y1="11" x2="10" y2="17"/>
                          <line x1="14" y1="11" x2="14" y2="17"/>
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Delete Single Run Confirmation Modal */}
      {confirmDeleteRun && (
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
          onClick={() => !isDeleting && setConfirmDeleteRun(null)}
        >
          <div
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 10,
              width: "100%",
              maxWidth: 460,
              boxShadow: "0 20px 40px rgba(0, 0, 0, 0.6)",
              overflow: "hidden",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ padding: "18px 20px 14px", borderBottom: "1px solid var(--border)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    background: "rgba(239, 68, 68, 0.15)",
                    color: "var(--neg, #ef4444)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 6h18"/>
                    <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                    <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                    <line x1="10" y1="11" x2="10" y2="17"/>
                    <line x1="14" y1="11" x2="14" y2="17"/>
                  </svg>
                </div>
                <div>
                  <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Delete Backtest Run</h3>
                  <p style={{ margin: "2px 0 0", fontSize: 11.5, color: "var(--ink-muted)" }}>
                    This will permanently delete this test and its stored metrics.
                  </p>
                </div>
              </div>
            </div>

            <div style={{ padding: "16px 20px" }}>
              <div
                style={{
                  background: "var(--surface-2)",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: "10px 14px",
                  fontSize: 12,
                  marginBottom: 14,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>
                  <span style={{ color: "var(--ink-muted)" }}>Date &amp; Time:</span>
                  <span style={{ fontWeight: 600 }}>
                    {new Date(confirmDeleteRun.created_at).toLocaleDateString("en-IN", {
                      day: "2-digit",
                      month: "short",
                      year: "numeric",
                    })}{" "}
                    {new Date(confirmDeleteRun.created_at).toLocaleTimeString("en-IN", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>
                  <span style={{ color: "var(--ink-muted)" }}>Strategy:</span>
                  <span style={{ fontWeight: 600, textTransform: "capitalize" }}>
                    {confirmDeleteRun.strategy_id || "Ladder"}
                  </span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--ink-muted)" }}>Net P&amp;L:</span>
                  <span
                    className="mono"
                    style={{
                      fontWeight: 700,
                      color:
                        (confirmDeleteRun.summary?.net_pnl ?? 0) >= 0
                          ? "var(--pos, #22c55e)"
                          : "var(--neg, #ef4444)",
                    }}
                  >
                    {inr(confirmDeleteRun.summary?.net_pnl ?? 0, { sign: true })}
                  </span>
                </div>
              </div>

              {(currentRunId === confirmDeleteRun.run_id || (!currentRunId && runList[0]?.run_id === confirmDeleteRun.run_id)) && (
                <div
                  style={{
                    padding: "8px 12px",
                    borderRadius: 6,
                    background: "rgba(234, 179, 8, 0.12)",
                    border: "1px solid rgba(234, 179, 8, 0.3)",
                    color: "var(--warn, #ca8a04)",
                    fontSize: 11.5,
                    lineHeight: 1.4,
                    marginBottom: 14,
                  }}
                >
                  ⚠️ <strong>Active Run:</strong> This backtest is currently displayed on your dashboard. Deleting it will reload the dashboard with your next available backtest.
                </div>
              )}

              <p style={{ margin: 0, fontSize: 12, color: "var(--ink-muted)" }}>
                Are you sure you want to delete this test?
              </p>
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: 8,
                padding: "12px 20px",
                borderTop: "1px solid var(--border)",
                background: "var(--surface-2)",
              }}
            >
              <button
                type="button"
                onClick={() => setConfirmDeleteRun(null)}
                disabled={isDeleting}
                style={{
                  padding: "6px 14px",
                  fontSize: 12,
                  fontWeight: 600,
                  borderRadius: 6,
                  background: "transparent",
                  border: "1px solid var(--border)",
                  color: "var(--ink)",
                  cursor: isDeleting ? "not-allowed" : "pointer",
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => handleDeleteRuns([confirmDeleteRun.run_id])}
                disabled={isDeleting}
                style={{
                  padding: "6px 14px",
                  fontSize: 12,
                  fontWeight: 600,
                  borderRadius: 6,
                  background: "var(--neg, #ef4444)",
                  border: "none",
                  color: "#fff",
                  cursor: isDeleting ? "not-allowed" : "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                {isDeleting ? "Deleting..." : "Delete Test"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Bulk Delete Confirmation Modal */}
      {confirmBulkDelete && (
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
          onClick={() => !isDeleting && setConfirmBulkDelete(false)}
        >
          <div
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              borderRadius: 10,
              width: "100%",
              maxWidth: 440,
              boxShadow: "0 20px 40px rgba(0, 0, 0, 0.6)",
              overflow: "hidden",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ padding: "18px 20px 14px", borderBottom: "1px solid var(--border)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    background: "rgba(239, 68, 68, 0.15)",
                    color: "var(--neg, #ef4444)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 6h18"/>
                    <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                    <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                    <line x1="10" y1="11" x2="10" y2="17"/>
                    <line x1="14" y1="11" x2="14" y2="17"/>
                  </svg>
                </div>
                <div>
                  <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>
                    Delete {selectedIds.length} Backtest {selectedIds.length === 1 ? "Run" : "Runs"}
                  </h3>
                  <p style={{ margin: "2px 0 0", fontSize: 11.5, color: "var(--ink-muted)" }}>
                    This will permanently remove the selected tests from your history.
                  </p>
                </div>
              </div>
            </div>

            <div style={{ padding: "16px 20px" }}>
              {currentRunId && selectedIds.includes(currentRunId) && (
                <div
                  style={{
                    padding: "8px 12px",
                    borderRadius: 6,
                    background: "rgba(234, 179, 8, 0.12)",
                    border: "1px solid rgba(234, 179, 8, 0.3)",
                    color: "var(--warn, #ca8a04)",
                    fontSize: 11.5,
                    lineHeight: 1.4,
                    marginBottom: 14,
                  }}
                >
                  ⚠️ <strong>Active Run Selected:</strong> One of the selected backtests is currently loaded on your dashboard. Deleting it will reset the dashboard to the latest remaining backtest.
                </div>
              )}

              <p style={{ margin: 0, fontSize: 12, color: "var(--ink)" }}>
                Are you sure you want to permanently delete these <strong>{selectedIds.length}</strong> backtest {selectedIds.length === 1 ? "run" : "runs"}? This action cannot be undone.
              </p>
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: 8,
                padding: "12px 20px",
                borderTop: "1px solid var(--border)",
                background: "var(--surface-2)",
              }}
            >
              <button
                type="button"
                onClick={() => setConfirmBulkDelete(false)}
                disabled={isDeleting}
                style={{
                  padding: "6px 14px",
                  fontSize: 12,
                  fontWeight: 600,
                  borderRadius: 6,
                  background: "transparent",
                  border: "1px solid var(--border)",
                  color: "var(--ink)",
                  cursor: isDeleting ? "not-allowed" : "pointer",
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => handleDeleteRuns(selectedIds)}
                disabled={isDeleting}
                style={{
                  padding: "6px 14px",
                  fontSize: 12,
                  fontWeight: 600,
                  borderRadius: 6,
                  background: "var(--neg, #ef4444)",
                  border: "none",
                  color: "#fff",
                  cursor: isDeleting ? "not-allowed" : "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                {isDeleting ? "Deleting..." : `Delete ${selectedIds.length} ${selectedIds.length === 1 ? "Run" : "Runs"}`}
              </button>
            </div>
          </div>
        </div>
      )}

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
