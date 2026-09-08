import { getDataset } from "@/lib/data";
import { inr, num, shortDate } from "@/lib/format";
import { Badge, Card, PageHeader, ProvenanceBanner } from "@/components/ui";

export default function TradesPage() {
  const { condors, provenance, metrics } = getDataset();
  const legs = condors.flatMap((c) => c.legs.map((l) => ({ ...l, condor: c })));

  return (
    <>
      <PageHeader
        title="Trades"
        subtitle={`Every leg the campaign placed: ${legs.length} legs across ${condors.length} rungs. Each carries the source of its price.`}
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={metrics.real_price_fraction}
          note={provenance.note}
          awaiting={provenance.awaiting_connection ?? false}
        />

        <Card
          title="Leg blotter"
          pad={0}
          hint="Entry and exit are per share; P&L is for the whole leg. SOURCE says whether the premium came from Choice or from the Black-76 model."
        >
          <div className="scroll-x" style={{ maxHeight: "70vh", overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Rung</th>
                  <th>Opened</th>
                  <th>Side</th>
                  <th>Strike</th>
                  <th style={{ textAlign: "right" }}>Qty</th>
                  <th style={{ textAlign: "right" }}>Entry</th>
                  <th style={{ textAlign: "right" }}>Exit</th>
                  <th style={{ textAlign: "right" }}>Leg P&amp;L</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {legs.map((l, i) => {
                  const pnl =
                    l.exit_price == null ? null : (l.exit_price - l.entry_price) * l.signed_qty;
                  return (
                    <tr key={i}>
                      <td className="tnum" style={{ color: "var(--ink-muted)" }}>
                        {num(l.condor.level)}
                      </td>
                      <td style={{ color: "var(--ink-2)" }}>{shortDate(l.condor.entry_time)}</td>
                      <td>
                        <Badge tone={l.side === "SELL" ? "warn" : "brand"}>
                          {l.side} {l.right}
                        </Badge>
                      </td>
                      <td className="tnum" style={{ fontWeight: 600 }}>
                        {num(l.strike)}
                      </td>
                      <td className="tnum" style={{ textAlign: "right" }}>
                        {num(l.qty)}
                      </td>
                      <td className="tnum" style={{ textAlign: "right" }}>
                        {l.entry_price.toFixed(2)}
                      </td>
                      <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                        {l.exit_price == null ? "--" : l.exit_price.toFixed(2)}
                      </td>
                      <td
                        className="tnum"
                        style={{
                          textAlign: "right",
                          fontWeight: 600,
                          color:
                            pnl == null
                              ? "var(--ink)"
                              : pnl > 0
                                ? "var(--pos)"
                                : pnl < 0
                                  ? "var(--neg)"
                                  : "var(--ink)",
                        }}
                      >
                        {pnl == null ? "--" : inr(pnl, { sign: true })}
                      </td>
                      <td>
                        <Badge tone={l.source === "choice" ? "pos" : "warn"}>
                          {l.source === "choice" ? "CHOICE" : "MODELED"}
                        </Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </>
  );
}
