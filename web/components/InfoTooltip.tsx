"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

export interface InfoExplanation {
  title: string;
  description: string;
  howToUse?: string;
  example?: string;
}

export const FIELD_EXPLANATIONS: Record<string, InfoExplanation> = {
  strategy: {
    title: "Trading Strategy",
    description:
      "Selects the execution logic. 'Condor Ladder' sells an Iron Condor at each grid step and profits when market stays range-bound or consolidates. 'Hybrid Iron Condor (HIC)' sells condors near the anchor, but automatically buys directional debit spreads when the index breaks out beyond the core band.",
    howToUse:
      "Choose Ladder for range-bound or oscillating markets. Choose HIC if you want upside/downside breakout protection.",
  },
  range: {
    title: "Historical Range",
    description:
      "The duration of historical NIFTY 50 and India VIX data replayed. Option premiums are resolved for every leg and bar in the test.",
    howToUse:
      "Start with 1 to 3 months for fast iteration, then run 1 to 3 years across varying market cycles before taking a setup live.",
  },
  resolution: {
    title: "Bar Size / Time Frame",
    description:
      "The bar interval on which grid levels and entry/exit triggers are evaluated. At 'Daily', levels fire only on the 15:30 closing price. At '5 min' or '15 min', levels fire intraday as each bar closes.",
    howToUse:
      "Daily bars filter out intraday noise and whipsaws. Intraday bars (5m/15m) enter rungs faster during trending intraday moves.",
  },
  cadence: {
    title: "Expiry Cadence",
    description:
      "Option expiration schedule. 'Weekly' trades the upcoming Thursday weekly NIFTY contract. 'Monthly' trades the last Thursday of the month.",
    howToUse:
      "Weekly options experience faster theta (time decay) but higher gamma risk near expiry. Monthly options offer wider wings and slower decay.",
  },
  lots: {
    title: "Position Sizing (Lots)",
    description:
      "Number of NIFTY lots traded for each rung. 1 lot trades 1 contract per leg (25 to 75 quantity depending on historical lot sizing regulations).",
    howToUse: "Use 1 lot during testing and calibration to inspect pure strategy performance.",
  },
  direction: {
    title: "Ladder Direction (v2)",
    description:
      "Controls whether condors are deployed on market declines, rallies, or both. 'Down-only' opens rungs only when NIFTY drops below anchor. 'Two-way (Both)' opens rungs in both directions as NIFTY moves away from anchor.",
    howToUse:
      "Use 'Two-way' for neutral or balanced markets. Use 'Down-only' if you specifically want to ladder into market pullbacks.",
    example: "At 25,000 anchor: Two-way opens rungs at 24,900, 24,800 AND 25,100, 25,200.",
  },
  anchor_mode: {
    title: "Anchor Strike Mode",
    description:
      "How the campaign's starting anchor level is calculated from the initial spot price. 'Floor' rounds down to the nearest grid step. 'Nearest' snaps to whichever step is closest. 'Round' uses standard mathematical rounding.",
    howToUse:
      "For Two-way strategies, 'Nearest' centers the ladder around current spot. For Down-only, 'Floor' anchors at the bottom of the current band.",
    example: "Spot at 24,670 with 100 pt step: Floor = 24,600; Nearest = 24,700.",
  },
  max_vix: {
    title: "Pause Above India VIX",
    description:
      "A volatility circuit breaker. When India VIX is above this threshold, no new condors or spreads will be opened. Once VIX drops back below, new entries resume. Existing open positions are never forcibly closed by this rule.",
    howToUse:
      "Set to 14 or 15 to avoid opening condors during high-volatility spikes or panic regimes. Leave blank/Off to trade in all regimes.",
  },
  take_profit: {
    title: "Take Profit Target",
    description:
      "Fixed profit target expressed as a percentage of the collected credit. When a condor achieves this mark-to-market profit, all 4 legs are closed immediately to lock in gains.",
    howToUse:
      "Common setting is 40% to 60%. Closing at 50% captures the majority of time decay without having to endure heightened tail risk close to expiration.",
    example: "Collected ₹100 credit. A 50% Take Profit will exit when trade profit reaches ₹50.",
  },
  stop_loss: {
    title: "Stop Loss Multiple",
    description:
      "Initial fixed risk limit expressed as a multiple of collected credit. If a position incurs a mark-to-market loss equal to this multiple, it is stopped out immediately to prevent catastrophic drawdowns.",
    howToUse:
      "Typically set to 1.5× to 2.5×. Prevents a single runaway trending gap from wiping out gains from multiple successful campaigns.",
    example: "Collected ₹100 credit. A 2.0× Stop Loss exits if loss reaches -₹200 (net position cost ₹300).",
  },
  trailing_sl: {
    title: "Trailing Stop-Loss (Trailing SL)",
    description:
      "Dynamic profit-protection stop. Tracks the peak mark-to-market profit of each position. If profit retreats from that peak by the specified multiple of credit, the trade exits with remaining profit locked in.",
    howToUse:
      "Set to 0.3× to 0.8× to safeguard accrued gains against late-campaign trend reversals. Safeguarded so it only activates once profits are secured.",
    example:
      "Collected ₹100 credit. Trade reaches ₹70 peak profit. With 0.5× (₹50) trailing SL, if profit drops back to ₹20 (₹70 - ₹50), it exits with ₹20 profit instead of running into a loss.",
  },
  trailing_trigger: {
    title: "Trailing Stop Activation Trigger",
    description:
      "The profit threshold required before trailing stop-loss activates. 'Auto (breakeven)' waits until profit covers the trailing distance, guaranteeing the stop floor is at or above breakeven (₹0). Alternatively, choose an explicit profit target like 20% or 30%.",
    howToUse:
      "Use 'Auto' to let the position breathe during initial entry noise while ensuring the trail never triggers at a loss.",
  },
  min_dte: {
    title: "Skip Late Entries (Min DTE)",
    description:
      "Days-to-Expiry filter. Rejects opening new condors if the current contract has fewer than this number of days remaining until expiration.",
    howToUse:
      "Set to 3 or 5 days to avoid the extreme gamma volatility and sudden gap risk that occurs in the final days of an expiry cycle.",
  },
  min_credit: {
    title: "Minimum Credit Ratio",
    description:
      "Quality filter requiring collected net credit to be at least this percentage of the wing width. Rejects opening condors when option pricing is abnormally depressed.",
    howToUse:
      "Set to 50% for standard risk parity: ensures max loss cannot exceed net credit collected.",
    example: "With a 100 pt wing, 50% requires at least ₹50 premium collected.",
  },
  max_down: {
    title: "Max Down Rungs",
    description:
      "The maximum number of ladder rungs allowed below the anchor strike. Once this cap is reached, further downward market drops will not trigger new positions.",
    howToUse:
      "Caps capital requirement and downside margin allocation during severe prolonged bear declines.",
  },
  max_up: {
    title: "Max Up Rungs",
    description:
      "The maximum number of ladder rungs allowed above the anchor strike. Caps exposure during strong upward rallies.",
    howToUse:
      "Balances upside capital exposure when running two-way or up-only configurations.",
  },
  step: {
    title: "Grid Step Interval",
    description:
      "The index point interval between consecutive rungs in the ladder. Each time NIFTY moves by this step from the previous level, a new condor is evaluated.",
    howToUse:
      "100 points is standard for NIFTY weekly strikes. Use 150 or 200 points for a wider, less frequent ladder deployment.",
  },
  wings: {
    title: "Condor Wing Offsets",
    description:
      "Distance of the short strikes and long protection strikes from each grid level. The difference (long offset minus short offset) is the wing width.",
    example: "Short ±100, Long ±200 creates a 100-point protected wing.",
  },
  hic_band: {
    title: "HIC Core Band",
    description:
      "The central zone around the anchor level where full 4-leg Iron Condors are deployed. Beyond this band, the strategy switches from selling condors to buying directional debit spreads.",
    howToUse:
      "Anchor ± 1 step creates a 3-level core condor zone. Beyond that, debit spreads catch trends.",
  },
  hic_half_mode: {
    title: "HIC Beyond The Band Mode",
    description:
      "What instrument to deploy when the index breaks outside the core band. 'Buy a spread' buys directional debit spreads to profit from the trending breakout. 'Sell one' sells credit spreads for comparison.",
    howToUse: "Use 'Buy a spread' to give the strategy asymmetric upside/downside trend participation.",
  },
  hic_debit_shift: {
    title: "HIC Spread Strikes Offset",
    description:
      "Strike placement for breakout debit spreads. 'Reverse condor (0 shift)' mirrors the condor's strikes. 'Bought at level (200 pt shift)' anchors the long leg directly at the breakout level for faster responsiveness.",
    howToUse:
      "'Bought at level' costs slightly more upfront premium but starts paying out sooner when the breakout accelerates.",
  },
  max_condors: {
    title: "Max Open Positions",
    description:
      "Global ceiling on total open positions active at the same time across the entire campaign.",
    howToUse: "Prevents over-leveraging and limits portfolio margin requirements.",
  },
  daily_loss_limit: {
    title: "Daily Loss Limit",
    description:
      "Circuit breaker in rupees (₹). If cumulative losses on a single trading day reach this threshold, no new positions will open for the rest of that day.",
    howToUse: "Protects against flash crashes and high-impact macro news events.",
  },
};

interface InfoTooltipProps {
  title?: string;
  content?: ReactNode;
  tip?: ReactNode;
  example?: string;
  field?: string;
  align?: "left" | "center" | "right";
  side?: "top" | "bottom";
  className?: string;
}

export function InfoTooltip({
  title,
  content,
  tip,
  example,
  field,
  align = "center",
  side = "top",
  className = "",
}: InfoTooltipProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLSpanElement>(null);

  // Look up predefined explanation if `field` is provided
  const exp = field ? FIELD_EXPLANATIONS[field] : undefined;
  const finalTitle = title ?? exp?.title;
  const finalContent = content ?? exp?.description;
  const finalTip = tip ?? exp?.howToUse;
  const finalExample = example ?? exp?.example;

  useEffect(() => {
    if (!open) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  if (!finalContent && !finalTitle) return null;

  return (
    <span
      ref={containerRef}
      className={`info-tooltip-wrapper ${className}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      style={{
        display: "inline-flex",
        alignItems: "center",
        position: "relative",
        verticalAlign: "middle",
        marginLeft: 5,
      }}
    >
      <button
        type="button"
        aria-label={finalTitle ? `Information about ${finalTitle}` : "More information"}
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="info-tooltip-btn"
        style={{
          border: "none",
          background: "transparent",
          padding: 0,
          margin: 0,
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          color: open ? "var(--brand, #2777f3)" : "var(--ink-muted, #667485)",
          borderRadius: "50%",
          transition: "all 0.15s ease",
          width: 16,
          height: 16,
          flexShrink: 0,
        }}
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="16" x2="12" y2="12" />
          <line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
      </button>

      {open && (
        <div
          role="tooltip"
          className="info-tooltip-popover"
          style={{
            position: "absolute",
            zIndex: 1200,
            width: "max-content",
            maxWidth: 300,
            padding: "10px 12px",
            background: "var(--surface-2, #ffffff)",
            color: "var(--ink, #221f20)",
            border: "1px solid var(--border-strong, #c6d3dd)",
            borderRadius: 8,
            boxShadow: "0 6px 20px rgba(0, 0, 0, 0.22), 0 2px 6px rgba(0, 0, 0, 0.1)",
            fontSize: 11.5,
            lineHeight: 1.48,
            pointerEvents: "auto",
            animation: "fadeIn 0.15s ease-out",
            ...(side === "top"
              ? { bottom: "calc(100% + 6px)" }
              : { top: "calc(100% + 6px)" }),
            ...(align === "center"
              ? { left: "50%", transform: "translateX(-50%)" }
              : align === "right"
              ? { right: -6 }
              : { left: -6 }),
          }}
        >
          {finalTitle && (
            <div
              style={{
                fontWeight: 700,
                fontSize: 12,
                color: "var(--ink, #221f20)",
                marginBottom: 5,
                display: "flex",
                alignItems: "center",
                gap: 6,
                borderBottom: "1px solid var(--border, #e4eaf0)",
                paddingBottom: 4,
              }}
            >
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: "var(--brand, #2777f3)",
                }}
              />
              {finalTitle}
            </div>
          )}

          <div style={{ color: "var(--ink-2, #3f454d)", margin: "4px 0" }}>
            {finalContent}
          </div>

          {finalTip && (
            <div
              style={{
                marginTop: 6,
                padding: "5px 7px",
                background: "var(--surface-3, #f3f6fa)",
                borderRadius: 5,
                borderLeft: "3px solid var(--brand, #2777f3)",
                fontSize: 11,
                color: "var(--ink-2, #3f454d)",
              }}
            >
              <strong style={{ color: "var(--brand, #2777f3)" }}>Tip: </strong>
              {finalTip}
            </div>
          )}

          {finalExample && (
            <div
              style={{
                marginTop: 6,
                fontSize: 11,
                color: "var(--ink-muted, #667485)",
                fontStyle: "italic",
              }}
            >
              <strong style={{ fontStyle: "normal", color: "var(--ink-2)" }}>Example: </strong>
              {finalExample}
            </div>
          )}
        </div>
      )}
    </span>
  );
}
