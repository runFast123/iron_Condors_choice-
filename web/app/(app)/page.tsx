export const dynamic = "force-dynamic";

import { getBacktestHistory, getDataset } from "@/lib/data";
import { inr, num, pct, ratio, shortDate } from "@/lib/format";
import { Badge, Card, PageHeader, ProvenanceBanner, Stat, StatGrid } from "@/components/ui";
import { EquityChart } from "@/components/charts/EquityChart";
import { RunBacktest } from "@/components/RunBacktest";
import { StrategyConditions } from "@/components/StrategyConditions";
import { BacktestHistoryComparison } from "@/components/BacktestHistoryComparison";
import { engine, engineConfigured } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";
import Link from "next/link";
import { Fragment, type ReactNode } from "react";
import type { Condor, EquityPoint, CampaignStepInfo } from "@/lib/types";

/** The latest job, or null — never a reason to fail the whole page. */
async function latestJob() {
  if (!engineConfigured()) return null;
  const token = await getSessionToken();
  if (!token) return null;
  try {
    return (await engine.backtestStatus(token)).job;
  } catch {
    /* the banner below already explains an unreachable engine */
    return null;
  }
}

export default async function Overview({
  searchParams,
}: {
  searchParams?: Promise<{ run?: string }>;
}) {
  const resolvedParams = searchParams ? await searchParams : {};
  const currentRunId = resolvedParams.run || null;

  const [dataset, job, historyRuns] = await Promise.all([
    getDataset(currentRunId),
    latestJob(),
    getBacktestHistory(30),
  ]);
  const { metrics: m, attribution: attr, equity, netting, condors, params, provenance, campaigns, rolls } = dataset;

  const hasData = condors.length > 0;
  // A finished run that opened nothing is not the same as never having run.
  const ranButEmpty = !hasData && job?.status === "done";

  const directionLabel =
    params.direction === "both"
      ? "Two-way (±100 pts)"
      : params.direction === "up"
      ? "Up-only (+100 pts)"
      : "Down-only (-100 pts)";

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle={
          <>
            A fresh iron condor at every {num(params.step)}-point step in NIFTY ({directionLabel.toLowerCase()}), each one{" "}
            <strong>short &plusmn;{num(params.short_offset)}</strong> and{" "}
            <strong>long &plusmn;{num(params.long_offset)}</strong> around its level.{" "}
            {provenance.range[0]} &rarr; {provenance.range[1]}.
          </>
        }
        right={
          <div style={{ display: "flex", gap: 7, alignItems: "center", flexWrap: "wrap" }}>
            <Badge
              tone="brand"
              title={(provenance.lot_sizes?.length ?? 0) > 1
                ? "Each contract at the lot size it traded at; NIFTY's lot has changed over the years."
                : undefined}
            >
              {(provenance.lot_sizes?.length ?? 0) > 1
                ? `${params.lots} lot · ${provenance.lot_sizes!.join(" → ")} qty by contract`
                : params.qty > 0 ? `${params.lots} lot · ${num(params.qty)} qty` : "lot size from scrip master"}
            </Badge>
            <Badge tone={params.direction === "both" ? "warn" : "neutral"}>
              {directionLabel}
            </Badge>
            {params.anchor_mode && (
              <Badge tone="neutral">{params.anchor_mode} anchor</Badge>
            )}
            <Badge title="Maximum condors open at once, so a long trend cannot keep opening positions">
              max {params.max_condors} condors
            </Badge>
          </div>
        }
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={m.real_price_fraction}
          backupFraction={provenance.provider?.backup_fraction ?? 0}
          historyFraction={provenance.provider?.history_fraction ?? 0}
          exchangeFraction={provenance.provider?.exchange_fraction ?? 0}
          exchange={provenance.exchange}
          note={provenance.note}
          awaiting={provenance.awaiting_connection ?? false}
          hasData={condors.length > 0}
          legs={{
            total: provenance.legs_total,
            real: provenance.legs_real,
            empty: provenance.legs_empty,
            unused: provenance.legs_unused,
            unresolved: provenance.legs_unresolved,
            emptyExpiries: provenance.empty_expiries,
            backup: provenance.legs_backup,
            history: provenance.legs_history,
            notAsked: provenance.legs_not_asked,
          }}
        />

        {ranButEmpty && (
          <div className="card" style={{ padding: "13px 15px", borderColor: "var(--warn)" }}>
            <strong style={{ fontSize: 13 }}>The run finished, but opened no condors.</strong>
            <p style={{ margin: "6px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.65, maxWidth: "76ch" }}>
              {params.direction === "both" ? (
                <>
                  The ladder opens a condor at each {num(params.step)}-point move{" "}
                  <em>in either direction</em>, so a window in which NIFTY never
                  travelled a full step from its anchor produces nothing.
                </>
              ) : params.direction === "up" ? (
                <>
                  This ladder is up-only: it opens a condor at each {num(params.step)}-point{" "}
                  <em>rise</em>, so a window in which NIFTY never rose a full step produces
                  nothing.
                </>
              ) : (
                <>
                  This ladder is down-only: it opens a condor at each {num(params.step)}-point{" "}
                  <em>decline</em>, so a window in which NIFTY never fell a full step produces
                  nothing.
                </>
              )}{" "}
              This is a real result, not a failure. Try a longer range, or a smaller
              step, to give the ladder something to trigger on.
            </p>
          </div>
        )}

        {hasData && (
          <StrategyConditions params={params} provenance={provenance} />
        )}

        <RunBacktest
          initialJob={job}
          hasData={hasData}
          currentLabel={hasData ? `${provenance.range[0]} to ${provenance.range[1]} at ${provenance.resolution === "D" ? "daily" : provenance.resolution + "-min"} bars` : undefined}
        />

        {hasData && (
        <>
        <StatGrid>
          <Stat
            label={(m.open_positions ?? 0) > 0 ? "Net P&L (closed)" : "Net P&L"}
            value={inr(m.net_pnl, { sign: true })}
            tone={m.net_pnl > 0 ? "pos" : m.net_pnl < 0 ? "neg" : "neutral"}
            delta={`after ${inr(m.total_costs)} costs`}
          />
          {(m.open_positions ?? 0) > 0 && (
            <Stat
              label="Open P&L"
              value={inr(m.open_pnl ?? 0, { sign: true })}
              tone={(m.open_pnl ?? 0) > 0 ? "pos" : (m.open_pnl ?? 0) < 0 ? "neg" : "neutral"}
              delta={`${m.open_positions} still open, marked at the last price`}
              hint="not yet a profit or a loss: these have not expired"
            />
          )}
          {attr && (attr.up_condors > 0 || params.direction === "both") ? (
            <>
              <Stat
                label="Down-side P&L"
                value={inr(attr.down_pnl, { sign: true })}
                tone={attr.down_pnl > 0 ? "pos" : attr.down_pnl < 0 ? "neg" : "neutral"}
                delta={`${attr.down_condors} condors · ${inr(attr.down_credit)} credit`}
              />
              <Stat
                label="Up-side P&L"
                value={inr(attr.up_pnl, { sign: true })}
                tone={attr.up_pnl > 0 ? "pos" : attr.up_pnl < 0 ? "neg" : "neutral"}
                delta={`${attr.up_condors} condors · ${inr(attr.up_credit)} credit`}
              />
              {(attr.anchor_condors ?? 0) > 0 && (
                <Stat
                  label="Anchor P&L"
                  value={inr(attr.anchor_pnl ?? 0, { sign: true })}
                  tone={(attr.anchor_pnl ?? 0) > 0 ? "pos" : (attr.anchor_pnl ?? 0) < 0 ? "neg" : "neutral"}
                  delta={`${attr.anchor_condors} condors · one per campaign`}
                />
              )}
            </>
          ) : (
            <Stat label="Win rate" value={pct(m.win_rate)} delta={`${m.wins}W / ${m.losses}L of ${m.condors} closed`} />
          )}
          <Stat
            label="Profit factor"
            value={
              // Null is not zero. The engine sends infinity when every closed
              // trade won -- there is no gross loss to divide by -- and that
              // arrives as null, on which `>= 1` is false: a flawless run was
              // painted red and told it was "losing more than winning".
              m.profit_factor == null && m.wins > 0 && m.losses === 0
                ? "∞"
                : ratio(m.profit_factor)
            }
            tone={
              m.profit_factor == null
                ? m.wins > 0 && m.losses === 0 ? "pos" : undefined
                : m.profit_factor >= 1 ? "pos" : "neg"
            }
            delta={
              m.profit_factor == null
                ? m.wins > 0 && m.losses === 0
                  ? "no losing trade to divide by"
                  : "nothing closed yet"
                : m.profit_factor >= 1 ? "gross win / gross loss" : "losing more than winning"
            }
          />
          <Stat
            label="Max drawdown"
            value={inr(m.max_drawdown)}
            tone={m.max_drawdown < 0 ? "neg" : "neutral"}
            delta={pct(m.max_drawdown_pct) + " of peak risk"}
          />
          <Stat label="Avg per condor" value={inr(m.expectancy, { sign: true })}
                tone={m.expectancy > 0 ? "pos" : "neg"} delta={`avg hold ${m.avg_days_held.toFixed(1)}d`} />
          <Stat label="Peak capital at risk" value={inr(m.capital_at_risk)}
                delta={`${m.max_concurrent} condors open at once`} />
          <Stat label="Expiry campaigns" value={num(campaigns ?? 1)}
                delta={(rolls?.length ?? 0) > 0
                  ? `${rolls!.length} roll${rolls!.length === 1 ? "" : "s"}`
                  : "single expiry"}
                hint="ladder re-anchors each expiry" />
        </StatGrid>
        </>
        )}

        {hasData && (
        <>
        <Card
          title="Cumulative P&L"
          hint="Mark-to-market equity across the campaign, with the drawdown envelope beneath. Hover for any bar."
        >
          <EquityChart points={equity} />
        </Card>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16 }}>
          <Card
            title="How much the ladder self-hedges"
            hint="The long wing of one condor lands on the same strike as the short of the condor two steps below, so the pair cancels."
          >
            <StatGrid min={130}>
              <Stat label="Offset" value={pct(netting.offset_ratio)} hint="of gross quantity" />
              <Stat label="Strikes flat" value={`${netting.strikes_fully_offset}/${netting.strikes_touched}`}
                    hint="net to exactly zero" />
              <Stat label="Gross qty" value={num(netting.gross_qty)} hint="all legs" />
              <Stat label="Net qty" value={num(netting.net_qty)} hint="what the broker holds" />
            </StatGrid>
            <p style={{ fontSize: 12, color: "var(--ink-muted)", margin: "12px 0 0", lineHeight: 1.6 }}>
              {pct(netting.offset_ratio)} of everything traded cancels internally. The book the broker
              actually carries is {pct(1 - netting.offset_ratio)} of the gross leg count.{" "}
              <Link href="/ladder" style={{ color: "var(--brand)", fontWeight: 600 }}>
                See the strike matrix &rarr;
              </Link>
            </p>
          </Card>

          <Card title="Risk &amp; return" hint="Returns are on peak capital at risk, since a credit ladder deploys no fixed capital. Sharpe and Sortino use daily returns with no risk-free rate subtracted, annualised over 252 days.">
            <StatGrid min={130}>
              <Stat label="Sharpe" value={ratio(m.sharpe)} hint="risk-free rate 0" />
              <Stat label="Sortino" value={ratio(m.sortino)} hint="risk-free rate 0" />
              {/* Untoned when unknown: red on a dash reads as a bad result
                  rather than an absent one, which is what it did on every
                  run shorter than a quarter. */}
              <Stat
                label="Annual return"
                value={pct(m.annual_return ?? null)}
                tone={m.annual_return == null ? "neutral" : m.annual_return > 0 ? "pos" : "neg"}
                hint={m.annual_return == null ? "needs a quarter of data" : "simple, on peak risk"}
              />
              {/* From the value, not from the label. A run where everything
                  lost has a "best" that is still a loss, and painting it green
                  said the opposite of what the number did. */}
              <Stat label="Best condor" value={inr(m.best, { sign: true })}
                    tone={m.best > 0 ? "pos" : m.best < 0 ? "neg" : undefined} />
              <Stat label="Worst condor" value={inr(m.worst, { sign: true })}
                    tone={m.worst > 0 ? "pos" : m.worst < 0 ? "neg" : undefined} />
              <Stat label="Total credit" value={inr(m.total_credit)} hint="premium collected" />
              {(m.total_debit ?? 0) > 0 && (
                <Stat label="Total debit" value={inr(m.total_debit ?? 0)} hint="premium paid for bought spreads" />
              )}
            </StatGrid>
          </Card>
        </div>

        {historyRuns && historyRuns.length > 0 && (
          <BacktestHistoryComparison
            runs={historyRuns}
            currentRunId={currentRunId}
          />
        )}

        {dataset.campaign_steps && dataset.campaign_steps.length > 0 && (
          <Card
            title="Monthly Dynamic VIX Grid Calculations"
            hint="Step sizes derived from India VIX and spot at the start of each monthly campaign."
          >
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>Campaign Expiry</th>
                    <th>Anchor Date</th>
                    <th style={{ textAlign: "right" }}>NIFTY Spot</th>
                    <th style={{ textAlign: "right" }}>India VIX</th>
                    <th style={{ textAlign: "right" }}>1-Mo Vol %</th>
                    <th style={{ textAlign: "right" }}>Expected Move</th>
                    <th style={{ textAlign: "right" }}>Target Condors</th>
                    <th style={{ textAlign: "right" }}>Raw Step</th>
                    <th style={{ textAlign: "right" }}>Applied Grid Step</th>
                  </tr>
                </thead>
                <tbody>
                  {dataset.campaign_steps.map((s) => (
                    <tr key={s.expiry}>
                      <td style={{ fontWeight: 600 }}>{shortDate(s.expiry)}</td>
                      <td style={{ color: "var(--ink-2)" }}>{shortDate(s.when)}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{num(s.spot)}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{s.vix != null ? s.vix.toFixed(2) : "—"}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{s.monthly_vol_pct != null ? `${s.monthly_vol_pct.toFixed(2)}%` : "—"}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{s.expected_move != null ? `${num(s.expected_move)} pts` : "—"}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{s.target_condors} / side</td>
                      <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>{s.raw_step != null ? `${num(s.raw_step)} pts` : "—"}</td>
                      <td className="tnum" style={{ textAlign: "right", fontWeight: 700 }}>
                        <Badge tone="brand">{num(s.step)} points</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 12, padding: "8px 12px", background: "var(--surface-2)", borderRadius: 6, fontSize: 11.5, color: "var(--ink-2)" }}>
              <strong>Calculation Formula:</strong> 1-Mo Vol % = VIX / &radic;12 &nbsp;&bull;&nbsp; Expected Move = Spot &times; Vol % &nbsp;&bull;&nbsp; Raw Step = Expected Move &divide; Target Condors &nbsp;&bull;&nbsp; Applied Step = max(100, round(Raw Step / 50) &times; 50).
            </div>
          </Card>
        )}

        <Card
          title="Condors opened"
          hint={`${condors.length} condors opened. Each row is one step; a shaded row marks where one expiry's campaign ends and the next begins.`}
          pad={0}
        >
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Level</th>
                  <th>Side</th>
                  <th>Opened</th>
                  <th>Square-Off Date</th>
                  <th>Expiry</th>
                  <th style={{ textAlign: "right" }}>Credit</th>
                  <th style={{ textAlign: "right" }}>Square-Off Price</th>
                  <th style={{ textAlign: "right" }}>Net P&amp;L</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {condors.map((c, i) => (
                  <Fragment key={c.index}>
                  {(i === 0 || c.expiry !== condors[i - 1].expiry) && (
                    <tr>
                      <td
                        colSpan={10}
                        style={{
                          background: "var(--surface-2)",
                          fontSize: 12,
                          color: "var(--ink-2)",
                          padding: "7px 12px",
                          lineHeight: 1.5,
                          whiteSpace: "normal",
                        }}
                      >
                        {campaignNote(c, i === 0 ? null : condors[i - 1], equity, dataset.campaign_steps)}
                      </td>
                    </tr>
                  )}
                  <tr>
                    <td className="tnum" style={{ color: "var(--ink-muted)" }}>{c.index + 1}</td>
                    <td className="tnum" style={{ fontWeight: 600 }}>{num(c.level)}</td>
                    <td>
                      <Badge tone={c.side === "up" ? "warn" : c.side === "anchor" ? "brand" : "neutral"}>
                        {(c.side ?? "down").toUpperCase()}
                      </Badge>
                    </td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(c.entry_time)}</td>
                    <td style={{ color: "var(--ink-2)" }}>
                      {c.status === "OPEN" ? (
                        <span style={{ color: "var(--brand)", fontWeight: 600 }}>Active</span>
                      ) : (
                        <span title={c.exit_time ? `Squared off on ${shortDate(c.exit_time)}` : `Settled at expiry on ${shortDate(c.expiry)}`}>
                          {c.exit_time ? shortDate(c.exit_time) : `${shortDate(c.expiry)} (Expiry)`}
                        </span>
                      )}
                    </td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(c.expiry)}</td>
                    <td className="tnum" style={{ textAlign: "right" }}>{inr(c.credit)}</td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {c.status === "OPEN" ? "--" : c.exit_total != null ? inr(c.exit_total) : "--"}
                    </td>
                    <td
                      className="tnum"
                      style={{
                        textAlign: "right",
                        fontWeight: 700,
                        color: c.pnl > 0 ? "var(--pos)" : c.pnl < 0 ? "var(--neg)" : "var(--ink)",
                      }}
                    >
                      {inr(c.pnl, { sign: true })}
                    </td>
                    <td>
                      <Outcome status={c.status} reason={c.exit_reason} />
                    </td>
                  </tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        </>
        )}
      </div>
    </>
  );
}

/** Where NIFTY stood at a moment, from the replay's own equity points. */
function spotAt(equity: EquityPoint[], iso: string): number | null {
  const t = new Date(iso).getTime();
  for (const p of equity) {
    if (new Date(p.ts).getTime() >= t && p.spot != null) return p.spot;
  }
  return null;
}

/**
 * The line above a campaign's first condor. Without it the table read as one
 * continuous ladder, and a new month starting at 24,000 straight after a
 * 24,500 rung looked like a mistake: it is a new campaign, re-anchored where
 * NIFTY stood when the old contracts expired, because offsetting only works
 * within one expiry.
 */
function campaignNote(
  first: Condor,
  previous: Condor | null,
  equity: EquityPoint[],
  campaignSteps?: CampaignStepInfo[],
): ReactNode {
  const spot = spotAt(equity, first.entry_time);
  const where = spot != null ? ` (NIFTY ${num(Math.round(spot))})` : "";
  const stepInfo = campaignSteps?.find((s) => s.expiry === first.expiry);
  const dynText = stepInfo ? (
    <span style={{ marginLeft: 8, color: "var(--brand)", fontWeight: 600 }}>
      &bull; Dynamic Step {num(stepInfo.step)} pts (VIX {stepInfo.vix != null ? stepInfo.vix.toFixed(2) : "—"} &rarr; 1-Mo move {stepInfo.expected_move != null ? num(stepInfo.expected_move) : "—"} pts &divide; {stepInfo.target_condors} = {stepInfo.raw_step != null ? num(stepInfo.raw_step) : "—"} pts)
    </span>
  ) : null;

  if (previous == null) {
    return (
      <>
        Campaign for the {shortDate(first.expiry)} expiry: anchored at {num(first.level)}{where} on {shortDate(first.entry_time)}.
        {dynText}
      </>
    );
  }
  return (
    <>
      New campaign for the {shortDate(first.expiry)} expiry: the {shortDate(previous.expiry)} contracts expired,{" "}
      so it re-anchored at {num(first.level)}{where} on {shortDate(first.entry_time)}. Levels count from there.
      {dynText}
    </>
  );
}

function Outcome({ status, reason }: { status: string; reason: string | null }) {
  const map: Record<string, { tone: "pos" | "neg" | "neutral" | "brand"; label: string }> = {
    EXPIRED: { tone: "neutral", label: "Held to expiry" },
    CLOSED_TARGET: { tone: "pos", label: "Target hit" },
    CLOSED_STOP: { tone: "neg", label: "Stopped out" },
    CLOSED_TRAILING_STOP: { tone: "brand", label: "Trailing stop" },
    CLOSED: { tone: "neutral", label: "Closed" },
    OPEN: { tone: "neutral", label: "Still open" },
  };
  const item = map[status] ?? { tone: "neutral" as const, label: status };
  return <Badge tone={item.tone} title={reason ?? undefined}>{item.label}</Badge>;
}
