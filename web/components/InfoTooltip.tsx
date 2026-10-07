"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

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
    title: "Monthly Campaign Stop Loss",
    description:
      "Caps cumulative loss across the monthly campaign (all condors opened for that monthly expiry). When cumulative monthly loss hits this threshold, all open positions are immediately squared off and no further condors open for that month.",
    howToUse:
      "Essential for protecting against adverse trending months like September 2026. Setting ₹20,000 or ₹25,000 caps monthly drawdown cleanly.",
    example:
      "In Sep 2026, setting ₹25,000 Campaign SL squares off open condors when loss reaches -₹25k, saving +₹41,000 compared to holding without stop loss (-₹66k).",
  },
  trailing_sl: {
    title: "Monthly Campaign Trailing SL (% of Peak Capital)",
    description:
      "Protects accumulated campaign capital across months. If the highest capital reached at the end of any monthly campaign is ₹25,000, setting a 15% TSL (₹3,750) will halt all new positions if capital drops to ₹21,250 in upcoming months, locking in your profits.",
    howToUse:
      "Select 15% (Recommended) or 10%–25% to lock in gains after profitable monthly campaigns and prevent giveback during subsequent adverse months.",
    example:
      "If peak capital at campaign end reaches ₹25,000, a 15% TSL equals ₹3,750. In the next upcoming month, if total capital drops to ₹21,250, all positions exit immediately and the system halts all further entries.",
  },
  trailing_trigger: {
    title: "TSL Activation Hurdle (Min Profit to Arm TSL)",
    description:
      "The minimum cumulative profit required before Trailing Stop Loss arms. Prevents premature exits from small daily wiggles during early trading. Once peak capital reaches this hurdle (e.g. ₹75,000 or ₹1,00,000), TSL arms to protect your accumulated profits.",
    howToUse:
      "Select 'After ₹75,000 profit (Recommended)' or 'After ₹1,00,000 profit' so your ladder can grow through the year and lock in peak returns (e.g. 1.46L) without being stopped out early.",
    example:
      "With a ₹75,000 hurdle and 15% TSL: early wiggles in months 1–3 don't trigger the stop. In August, when profit reaches ₹1,46,000, TSL arms. When September plunges, the 15% TSL halts at ₹1,24,100, saving over ₹51,000.",
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
  className?: string;
}

export function InfoTooltip({
  title,
  content,
  tip,
  example,
  field,
  className = "",
}: InfoTooltipProps) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [coords, setCoords] = useState<{
    top: number;
    left: number;
    placement: "top" | "bottom";
    arrowLeft: number;
  }>({ top: 0, left: 0, placement: "top", arrowLeft: 20 });

  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const closeTimerRef = useRef<NodeJS.Timeout | null>(null);

  // Look up predefined explanation if `field` is provided
  const exp = field ? FIELD_EXPLANATIONS[field] : undefined;
  const finalTitle = title ?? exp?.title;
  const finalContent = content ?? exp?.description;
  const finalTip = tip ?? exp?.howToUse;
  const finalExample = example ?? exp?.example;

  useEffect(() => {
    setMounted(true);
  }, []);

  const updatePosition = useCallback(() => {
    if (!triggerRef.current) return;
    const triggerRect = triggerRef.current.getBoundingClientRect();
    const popover = popoverRef.current;

    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const PADDING = 12;
    const GAP = 8;

    // Use measured dimensions if available, otherwise sensible default
    const popWidth = popover ? popover.offsetWidth : 300;
    const popHeight = popover ? popover.offsetHeight : 180;

    // Center horizontally on the trigger icon
    const triggerCenterX = triggerRect.left + triggerRect.width / 2;
    let left = triggerCenterX - popWidth / 2;

    // Strict boundary clamping so the tooltip NEVER cuts off on left or right
    if (left < PADDING) {
      left = PADDING;
    } else if (left + popWidth > viewportWidth - PADDING) {
      left = viewportWidth - PADDING - popWidth;
    }

    // Vertical placement: prefer top unless space above is cramped
    const spaceAbove = triggerRect.top;
    const spaceBelow = viewportHeight - triggerRect.bottom;
    const placement: "top" | "bottom" =
      spaceAbove >= popHeight + GAP + PADDING || spaceAbove >= spaceBelow
        ? "top"
        : "bottom";

    let top =
      placement === "top"
        ? triggerRect.top - popHeight - GAP
        : triggerRect.bottom + GAP;

    // Clamp top to viewport
    if (top < PADDING) top = PADDING;
    if (top + popHeight > viewportHeight - PADDING) {
      top = viewportHeight - PADDING - popHeight;
    }

    // Pointer arrow position aligned with trigger center
    const arrowLeft = Math.max(14, Math.min(triggerCenterX - left, popWidth - 14));

    setCoords({ top, left, placement, arrowLeft });
  }, []);

  const handleOpen = () => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    setOpen(true);
  };

  const handleClose = () => {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    closeTimerRef.current = setTimeout(() => {
      setOpen(false);
    }, 120);
  };

  useLayoutEffect(() => {
    if (open) {
      updatePosition();
    }
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;

    // Recalculate on window resize or any parent container scroll
    const onScrollOrResize = () => {
      updatePosition();
    };

    window.addEventListener("resize", onScrollOrResize);
    window.addEventListener("scroll", onScrollOrResize, { capture: true, passive: true });

    function handleClickOutside(e: MouseEvent) {
      if (
        triggerRef.current &&
        !triggerRef.current.contains(e.target as Node) &&
        popoverRef.current &&
        !popoverRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("resize", onScrollOrResize);
      window.removeEventListener("scroll", onScrollOrResize, { capture: true });
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, updatePosition]);

  if (!finalContent && !finalTitle) return null;

  return (
    <span
      className={`info-tooltip-wrapper ${className}`}
      onMouseEnter={handleOpen}
      onMouseLeave={handleClose}
      style={{
        display: "inline-flex",
        alignItems: "center",
        verticalAlign: "middle",
        marginLeft: 5,
        position: "relative",
      }}
    >
      <button
        ref={triggerRef}
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

      {/* Render via Portal to document.body so no parent overflow:hidden or transform can ever clip it */}
      {mounted && open &&
        createPortal(
          <div
            ref={popoverRef}
            role="tooltip"
            className="info-tooltip-popover"
            onMouseEnter={handleOpen}
            onMouseLeave={handleClose}
            style={{
              position: "fixed",
              top: coords.top,
              left: coords.left,
              zIndex: 99999,
              width: "max-content",
              maxWidth: 310,
              padding: "11px 13px",
              background: "var(--surface-2, #ffffff)",
              color: "var(--ink, #221f20)",
              border: "1px solid var(--border-strong, #c6d3dd)",
              borderRadius: 9,
              boxShadow:
                "0 8px 24px rgba(0, 0, 0, 0.28), 0 2px 6px rgba(0, 0, 0, 0.12)",
              fontSize: 11.5,
              lineHeight: 1.5,
              pointerEvents: "auto",
            }}
          >
            {/* Pointer arrow pointing to trigger icon */}
            <div
              style={{
                position: "absolute",
                width: 8,
                height: 8,
                background: "var(--surface-2, #ffffff)",
                left: coords.arrowLeft,
                transform: "translateX(-50%) rotate(45deg)",
                zIndex: -1,
                ...(coords.placement === "top"
                  ? {
                      bottom: -4,
                      borderRight: "1px solid var(--border-strong, #c6d3dd)",
                      borderBottom: "1px solid var(--border-strong, #c6d3dd)",
                    }
                  : {
                      top: -4,
                      borderLeft: "1px solid var(--border-strong, #c6d3dd)",
                      borderTop: "1px solid var(--border-strong, #c6d3dd)",
                    }),
              }}
            />

            {finalTitle && (
              <div
                style={{
                  fontWeight: 700,
                  fontSize: 12,
                  color: "var(--ink, #221f20)",
                  marginBottom: 6,
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
                    flexShrink: 0,
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
                  marginTop: 7,
                  padding: "5px 8px",
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
                  marginTop: 7,
                  fontSize: 11,
                  color: "var(--ink-muted, #667485)",
                  fontStyle: "italic",
                }}
              >
                <strong style={{ fontStyle: "normal", color: "var(--ink-2)" }}>Example: </strong>
                {finalExample}
              </div>
            )}
          </div>,
          document.body,
        )}
    </span>
  );
}
