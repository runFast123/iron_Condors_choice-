"use client";

import type { LiveCampaign } from "@/lib/live";
import { inr, shortDate } from "@/lib/format";

const STATUS: Record<LiveCampaign["status"], { label: string; tone: string }> = {
  active: { label: "Live", tone: "var(--pos)" },
  settling: { label: "Settling", tone: "var(--warn)" },
  settled: { label: "Settled", tone: "var(--ink-muted)" },
  closed: { label: "Closed early", tone: "var(--ink-muted)" },
};

/**
 * The run's campaigns, one card each, newest first. A run's total mixes
 * every expiry it has traded; picking a card scopes the page to that one.
 */
export function CampaignStrip({
  campaigns,
  selected,
  onSelect,
}: {
  campaigns: LiveCampaign[];
  selected: string;
  onSelect: (expiry: string) => void;
}) {
  return (
    <div className="campaign-strip" role="tablist" aria-label="Campaigns">
      {campaigns.map((c) => {
        const status = STATUS[c.status];
        const on = c.expiry === selected;
        const pnl = c.pnl;
        return (
          <button
            key={c.expiry}
            type="button"
            role="tab"
            aria-selected={on}
            className={`campaign-card${on ? " is-on" : ""}`}
            onClick={() => onSelect(c.expiry)}
          >
            <span className="campaign-card-top">
              <span className="campaign-card-expiry">{shortDate(c.expiry)}</span>
              <span className="campaign-card-status" style={{ color: status.tone }}>
                {c.status === "active" && <span className="live-dot" aria-hidden="true" />}
                {status.label}
              </span>
            </span>
            <span
              className="campaign-card-pnl tnum"
              style={{ color: pnl == null ? "var(--ink-muted)" : pnl > 0 ? "var(--pos)" : pnl < 0 ? "var(--neg)" : "var(--ink)" }}
            >
              {pnl == null ? "--" : inr(pnl, { sign: true })}
            </span>
            <span className="campaign-card-meta">
              {c.positions} position{c.positions === 1 ? "" : "s"}
              {c.open > 0 ? ` · ${c.open} open` : ""}
            </span>
          </button>
        );
      })}
    </div>
  );
}
