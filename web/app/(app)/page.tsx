import { getDataset } from "@/lib/data";
import { inr, num, pct, ratio, shortDate } from "@/lib/format";
import { Badge, Card, PageHeader, ProvenanceBanner, Stat, StatGrid } from "@/components/ui";
import { EquityChart } from "@/components/charts/EquityChart";
import Link from "next/link";

export default function Overview() {
  const { metrics: m, equity, netting, condors, params, provenance, campaigns, rolls } = getDataset();
  const awaiting = provenance.awaiting_connection ?? false;
  const lastSpot = [...equity].reverse().find((p) => p.spot != null)?.spot ?? null;

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle={
          <>
            A fresh iron condor at every {num(params.step)}-point decline in NIFTY, each one{" "}
            <strong>short &plusmn;{num(params.short_offset)}</strong> and{" "}
            <strong>long &plusmn;{num(params.long_offset)}</strong> around its level.{" "}
            {provenance.range[0]} &rarr; {provenance.range[1]}.
          </>
        }
        right={
          <div style={{ display: "flex", gap: 7, alignItems: "center" }}>
            <Badge tone="brand">
              {params.qty > 0 ? `${params.lots} lot · ${num(params.qty)} qty` : "lot size from scrip master"}
            </Badge>
            <Badge>{params.max_condors} rung cap</Badge>
          </div>
        }
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={m.real_price_fraction}
          note={provenance.note}
          awaiting={provenance.awaiting_connection ?? false}
        />

        {!awaiting && (
        <StatGrid>
          <Stat
            label="Net P&L"
            value={inr(m.net_pnl, { sign: true })}
            tone={m.net_pnl > 0 ? "pos" : m.net_pnl < 0 ? "neg" : "neutral"}
            delta={`after ${inr(m.total_costs)} costs`}
          />
          <Stat label="Win rate" value={pct(m.win_rate)} delta={`${m.wins}W / ${m.losses}L of ${m.condors}`} />
          <Stat
            label="Profit factor"
            value={ratio(m.profit_factor)}
            tone={m.profit_factor >= 1 ? "pos" : "neg"}
            delta={m.profit_factor >= 1 ? "gross win / gross loss" : "losing more than winning"}
          />
          <Stat
            label="Max drawdown"
            value={inr(m.max_drawdown)}
            tone={m.max_drawdown < 0 ? "neg" : "neutral"}
            delta={pct(m.max_drawdown_pct) + " of peak risk"}
          />
          <Stat label="Expectancy / rung" value={inr(m.expectancy, { sign: true })}
                tone={m.expectancy > 0 ? "pos" : "neg"} delta={`avg hold ${m.avg_days_held.toFixed(1)}d`} />
          <Stat label="Peak capital at risk" value={inr(m.capital_at_risk)}
                delta={`${m.max_concurrent} rungs open at once`} />
          <Stat label="Expiry campaigns" value={num(campaigns ?? 1)}
                delta={(rolls?.length ?? 0) > 0
                  ? `${rolls!.length} roll${rolls!.length === 1 ? "" : "s"}`
                  : "single expiry"}
                hint="ladder re-anchors each expiry" />
        </StatGrid>
        )}

        {!awaiting && (
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
            hint="The long wing of one rung lands on the same strike as the short of the rung two steps below, so the pair cancels."
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

          <Card title="Risk &amp; return" hint="Ratios use peak capital at risk as the denominator, since a credit ladder deploys no fixed capital.">
            <StatGrid min={130}>
              <Stat label="Sharpe" value={ratio(m.sharpe)} />
              <Stat label="Sortino" value={ratio(m.sortino)} />
              <Stat label="CAGR" value={pct(m.cagr)} tone={m.cagr > 0 ? "pos" : "neg"} />
              <Stat label="Best rung" value={inr(m.best, { sign: true })} tone="pos" />
              <Stat label="Worst rung" value={inr(m.worst, { sign: true })} tone="neg" />
              <Stat label="Total credit" value={inr(m.total_credit)} hint="premium collected" />
            </StatGrid>
          </Card>
        </div>

        <Card
          title="Ladder rungs"
          hint={`${condors.length} condors opened. Each row is one 100-point step down.`}
          pad={0}
        >
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Level</th>
                  <th>Opened</th>
                  <th>Expiry</th>
                  <th style={{ textAlign: "right" }}>Credit</th>
                  <th style={{ textAlign: "right" }}>Max loss</th>
                  <th style={{ textAlign: "right" }}>P&amp;L</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {condors.map((c) => (
                  <tr key={c.index}>
                    <td className="tnum" style={{ color: "var(--ink-muted)" }}>{c.index + 1}</td>
                    <td className="tnum" style={{ fontWeight: 600 }}>{num(c.level)}</td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(c.entry_time)}</td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(c.expiry)}</td>
                    <td className="tnum" style={{ textAlign: "right" }}>{inr(c.credit)}</td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {inr(c.max_loss)}
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

function Outcome({ status, reason }: { status: string; reason: string | null }) {
  const map: Record<string, { tone: "pos" | "neg" | "neutral"; label: string }> = {
    EXPIRED: { tone: "neutral", label: "Held to expiry" },
    CLOSED_TARGET: { tone: "pos", label: "Target hit" },
    CLOSED_STOP: { tone: "neg", label: "Stopped out" },
    OPEN: { tone: "neutral", label: "Open" },
  };
  const item = map[status] ?? { tone: "neutral" as const, label: status };
  return <Badge tone={item.tone} title={reason ?? undefined}>{item.label}</Badge>;
}
