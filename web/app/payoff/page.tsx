import { getDataset } from "@/lib/data";
import { inr, num } from "@/lib/format";
import { Card, PageHeader, ProvenanceBanner, Stat, StatGrid } from "@/components/ui";
import { PayoffChart } from "@/components/charts/PayoffChart";

export default function PayoffPage() {
  const { payoff, equity, condors, params, provenance, metrics } = getDataset();
  const lastSpot = [...equity].reverse().find((p) => p.spot != null)?.spot ?? null;

  const best = payoff.reduce((a, b) => (b.pnl > a.pnl ? b : a), payoff[0]);
  const worst = payoff.reduce((a, b) => (b.pnl < a.pnl ? b : a), payoff[0]);
  const totalMaxLoss = condors.reduce((s, c) => s + c.max_loss, 0);
  const totalCredit = condors.reduce((s, c) => s + c.credit, 0);

  return (
    <>
      <PageHeader
        title="Payoff at Expiry"
        subtitle="Combined profit and loss across every rung, if all of them settled at the same NIFTY level. Breakevens are marked where the curve crosses zero."
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={metrics.real_price_fraction}
          note={provenance.note}
        />

        <StatGrid>
          <Stat label="Peak payoff" value={inr(best?.pnl ?? 0, { sign: true })} tone="pos"
                hint={`at NIFTY ${num(best?.spot ?? 0)}`} />
          <Stat label="Worst payoff" value={inr(worst?.pnl ?? 0, { sign: true })} tone="neg"
                hint={`at NIFTY ${num(worst?.spot ?? 0)}`} />
          <Stat label="Credit collected" value={inr(totalCredit)} hint="across all rungs" />
          <Stat label="Sum of rung max-loss" value={inr(totalMaxLoss)}
                hint="before any offsetting" />
        </StatGrid>

        <Card
          title="Combined expiry payoff"
          hint="Gains shaded above the zero line, losses below. The gold marker is the last observed NIFTY level."
        >
          <PayoffChart data={payoff} spot={lastSpot} />
        </Card>

        <Card title="Per-rung structure" pad={0}
              hint={`Each rung risks at most one ${num(params.long_offset - params.short_offset)}-point wing, because only one side can finish in the money.`}>
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Rung</th>
                  <th style={{ textAlign: "right" }}>Credit</th>
                  <th style={{ textAlign: "right" }}>Max profit</th>
                  <th style={{ textAlign: "right" }}>Max loss</th>
                  <th style={{ textAlign: "right" }}>Lower BE</th>
                  <th style={{ textAlign: "right" }}>Upper BE</th>
                </tr>
              </thead>
              <tbody>
                {condors.map((c) => (
                  <tr key={c.index}>
                    <td className="tnum" style={{ fontWeight: 600 }}>{num(c.level)}</td>
                    <td className="tnum" style={{ textAlign: "right" }}>{inr(c.credit)}</td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--pos)" }}>
                      {inr(c.max_profit)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--neg)" }}>
                      {inr(-c.max_loss)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {num(c.breakevens[0], 0)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {num(c.breakevens[1], 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </>
  );
}
