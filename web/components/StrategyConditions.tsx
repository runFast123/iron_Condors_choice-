"use client";

import { useState } from "react";
import type { Params, Provenance } from "@/lib/types";
import { num } from "@/lib/format";
import { InfoTooltip } from "./InfoTooltip";

interface StrategyConditionsProps {
  params: Params;
  provenance: Provenance;
}

export function StrategyConditions({ params, provenance }: StrategyConditionsProps) {
  const [expanded, setExpanded] = useState(false);

  const directionText =
    params.direction === "both"
      ? "Two-way (both rallies and declines open rungs)"
      : params.direction === "up"
      ? "Up-only (only market rallies open rungs)"
      : "Down-only (only market declines open rungs)";

  const wingWidth = params.long_offset - params.short_offset;
  const isVixActive = params.max_entry_vix != null && params.max_entry_vix > 0;
  const isDteActive = params.min_entry_dte != null && params.min_entry_dte > 0;
  const isCreditActive = params.min_credit_ratio != null && params.min_credit_ratio > 0;
  const isTakeProfitActive = params.take_profit_pct != null && params.take_profit_pct > 0;
  const isStopLossActive = params.stop_loss_mult != null && params.stop_loss_mult > 0;
  const isTrailingSlActive = params.trailing_sl_mult != null && params.trailing_sl_mult > 0;

  return (
    <section
      className="card"
      style={{
        border: "1px solid var(--border)",
        background: "linear-gradient(180deg, var(--surface) 0%, rgba(18, 22, 31, 0.7) 100%)",
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
          borderBottom: expanded ? "1px solid var(--border)" : "none",
          cursor: "pointer",
          userSelect: "none",
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "var(--brand, #3b82f6)",
                boxShadow: "0 0 8px var(--brand, #3b82f6)",
              }}
            />
            <h2 style={{ margin: 0, fontSize: 13.5, fontWeight: 600, color: "var(--ink)" }}>
              Strategy Conditions &amp; Active Rules
            </h2>
          </div>
          <span style={{ fontSize: 12, color: "var(--ink-muted)" }}>
            Exact parameters and filters applied to this backtest run
          </span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 11.5, color: "var(--ink-muted)" }}>
            {expanded ? "Hide details ▲" : "View full rules ▼"}
          </span>
        </div>
      </div>

      {/* Quick Summary Pill Bar */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 8,
          padding: "10px 16px",
          background: "var(--surface-2)",
          borderTop: expanded ? "none" : "1px solid var(--border)",
          fontSize: 12,
        }}
      >
        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>Grid:</span>
          <span className="mono" style={{ fontWeight: 600, color: "var(--ink)" }}>
            {num(params.step)} pts
          </span>
          <InfoTooltip field="step" />
        </div>

        <span style={{ color: "var(--border)" }}>•</span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>Direction:</span>
          <span
            style={{
              fontWeight: 600,
              color: params.direction === "both" ? "#38bdf8" : "var(--ink)",
            }}
          >
            {params.direction === "both" ? "Two-way" : params.direction === "up" ? "Up-only" : "Down-only"}
          </span>
          <InfoTooltip field="direction" />
        </div>

        <span style={{ color: "var(--border)" }}>•</span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>Wings:</span>
          <span className="mono" style={{ color: "var(--ink)" }}>
            &plusmn;{num(params.short_offset)} / &plusmn;{num(params.long_offset)} ({num(wingWidth)} pt wing)
          </span>
          <InfoTooltip field="wings" />
        </div>

        <span style={{ color: "var(--border)" }}>•</span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>Anchor:</span>
          <span style={{ color: "var(--ink)" }}>{params.anchor_mode}</span>
          <InfoTooltip field="anchor_mode" />
        </div>

        <span style={{ color: "var(--border)" }}>•</span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>VIX Gate:</span>
          {isVixActive ? (
            <span style={{ color: "#34d399", fontWeight: 600 }}>&le; {params.max_entry_vix}</span>
          ) : (
            <span style={{ color: "var(--ink-muted)" }}>Off</span>
          )}
          <InfoTooltip field="max_vix" />
        </div>

        <span style={{ color: "var(--border)" }}>•</span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>Exit Rule:</span>
          <span style={{ color: "var(--ink)" }}>
            {[
              isTakeProfitActive ? `${Math.round(params.take_profit_pct! * 100)}% TP` : null,
              isStopLossActive ? `${params.stop_loss_mult}x SL` : null,
              isTrailingSlActive ? `${params.trailing_sl_mult}x Trail SL` : null,
            ].filter(Boolean).join(" / ") || "Hold to expiry"}
          </span>
          <InfoTooltip
            title="Exit Rules & Guardrails"
            content="Automated exit triggers active on every bar. Take Profit locks in designated gains; Stop Loss limits max drawdown; Trailing SL locks in peak profit upon pullbacks."
          />
        </div>

        <span style={{ color: "var(--border)" }}>•</span>

        <div style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
          <span style={{ color: "var(--ink-muted)" }}>Cadence:</span>
          <span style={{ color: "var(--ink)" }}>
            {provenance.resolution === "D" ? "Daily bars" : `${provenance.resolution}-min bars`} · Roll on expiry
          </span>
          <InfoTooltip field="cadence" />
        </div>
      </div>

      {/* Expanded Rule Cards */}
      {expanded && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
            gap: 12,
            padding: 16,
            background: "rgba(0, 0, 0, 0.15)",
          }}
        >
          {/* Card 1: Ladder Grid & Structure */}
          <div
            style={{
              padding: "12px 14px",
              background: "var(--surface)",
              borderRadius: 6,
              border: "1px solid var(--border)",
            }}
          >
            <div style={{ fontSize: 11, fontWeight: 700, color: "var(--ink-muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              1. Grid &amp; Condor Geometry
            </div>
            <div style={{ display: "grid", gap: 6, fontSize: 12.5 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Step interval <InfoTooltip field="step" />
                </span>
                <span className="mono" style={{ fontWeight: 600 }}>{num(params.step)} points</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Short leg strike <InfoTooltip title="Short Leg Strike" content="Strike of the sold call and put legs at Level ± Short Offset." />
                </span>
                <span className="mono">Level &plusmn; {num(params.short_offset)}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Long protection leg <InfoTooltip title="Long Protection Leg" content="Strike of the bought protection call and put legs at Level ± Long Offset." />
                </span>
                <span className="mono">Level &plusmn; {num(params.long_offset)}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Wing width <InfoTooltip field="wings" />
                </span>
                <span className="mono" style={{ color: "#38bdf8", fontWeight: 600 }}>{num(wingWidth)} points</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Anchor mode <InfoTooltip field="anchor_mode" />
                </span>
                <span>{params.anchor_mode}</span>
              </div>
              <div style={{ marginTop: 4, fontSize: 11.5, color: "var(--ink-muted)", lineHeight: 1.4 }}>
                {directionText}
              </div>
            </div>
          </div>

          {/* Card 2: Risk Gates & Entry Filters */}
          <div
            style={{
              padding: "12px 14px",
              background: "var(--surface)",
              borderRadius: 6,
              border: "1px solid var(--border)",
            }}
          >
            <div style={{ fontSize: 11, fontWeight: 700, color: "var(--ink-muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              2. Entry Filters &amp; Risk Guards
            </div>
            <div style={{ display: "grid", gap: 6, fontSize: 12.5 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Max India VIX Gate <InfoTooltip field="max_vix" />
                </span>
                <span style={{ fontWeight: 600, color: isVixActive ? "#34d399" : "var(--ink-muted)" }}>
                  {isVixActive ? `≤ ${params.max_entry_vix} (Active)` : "Unrestricted (Off)"}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Min Days to Expiry (DTE) <InfoTooltip field="min_dte" />
                </span>
                <span style={{ fontWeight: 600, color: isDteActive ? "#34d399" : "var(--ink-muted)" }}>
                  {isDteActive ? `≥ ${params.min_entry_dte} days` : "Any DTE (Off)"}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Min Credit Ratio <InfoTooltip field="min_credit" />
                </span>
                <span style={{ fontWeight: 600, color: isCreditActive ? "#34d399" : "var(--ink-muted)" }}>
                  {isCreditActive ? `≥ ${Math.round(params.min_credit_ratio! * 100)}% of wing` : "Any credit (Off)"}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Max open condors <InfoTooltip field="max_condors" />
                </span>
                <span className="mono">Max {params.max_condors} total</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Per-side caps <InfoTooltip title="Per-Side Caps" content="Maximum rungs allowed down (below anchor) and up (above anchor) to cap directional capital deployment." />
                </span>
                <span className="mono">Down {params.max_down ?? "none"} / Up {params.max_up ?? "none"}</span>
              </div>
            </div>
          </div>

          {/* Card 3: Execution, Expiry & Sizing */}
          <div
            style={{
              padding: "12px 14px",
              background: "var(--surface)",
              borderRadius: 6,
              border: "1px solid var(--border)",
            }}
          >
            <div style={{ fontSize: 11, fontWeight: 700, color: "var(--ink-muted)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>
              3. Execution, Expiry &amp; Sizing
            </div>
            <div style={{ display: "grid", gap: 6, fontSize: 12.5 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Exit policy <InfoTooltip title="Exit Policy" content="Automatic profit-taking and loss-cutting rules evaluated on every bar." />
                </span>
                <span style={{ fontWeight: 600 }}>
                  {[
                    isTakeProfitActive ? `${Math.round(params.take_profit_pct! * 100)}% Take Profit` : null,
                    isStopLossActive ? `${params.stop_loss_mult}x Stop Loss` : null,
                    isTrailingSlActive ? `${params.trailing_sl_mult}x Trailing SL` : null,
                  ].filter(Boolean).join(" · ") || "Held to settlement / expiry"}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Trailing Stop-Loss <InfoTooltip field="trailing_sl" />
                </span>
                <span style={{ fontWeight: 600, color: isTrailingSlActive ? "#38bdf8" : "var(--ink-muted)" }}>
                  {isTrailingSlActive
                    ? `${params.trailing_sl_mult}x pullback${
                        params.trailing_sl_trigger_pct != null
                          ? ` (trig ≥ ${Math.round(params.trailing_sl_trigger_pct * 100)}%)`
                          : " (auto trig)"
                      }`
                    : "Off"}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Expiry roll <InfoTooltip title="Expiry Roll" content="Whether the strategy automatically closes and transitions to the subsequent expiry cycle." />
                </span>
                <span>{params.roll_to_next_expiry !== false ? "Rolls to next expiry" : "Single campaign"}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Bar resolution <InfoTooltip field="resolution" />
                </span>
                <span className="mono">{provenance.resolution === "D" ? "Daily closes (15:30)" : `${provenance.resolution} minute bars`}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Lots &amp; quantity <InfoTooltip field="lots" />
                </span>
                <span className="mono">
                  {(provenance.lot_sizes?.length ?? 0) > 1
                    ? `${params.lots} lot (${provenance.lot_sizes!.join(" → ")} qty)`
                    : `${params.lots} lot · ${params.qty > 0 ? num(params.qty) : num(params.lot_size)} qty`}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ color: "var(--ink-muted)", display: "inline-flex", alignItems: "center" }}>
                  Data coverage <InfoTooltip title="Data Coverage & Provenance" content="Displays whether pricing was fetched from real executed exchange ticks / database records, or calculated via Black-76 IV model." />
                </span>
                <span style={{ color: "#34d399", fontWeight: 600 }}>
                  {provenance.verified ? "100% Real Choice/DB trades" : "Exchange-anchored"}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
