export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { num, pct, inr } from "@/lib/format";
import { Card, PageHeader, ProvenanceBanner, Stat, StatGrid } from "@/components/ui";
import { StrikeMatrix } from "@/components/charts/StrikeMatrix";

export default async function LadderPage() {
  const { strike_matrix, condors, netting, params, provenance, metrics } = await getDataset();

  return (
    <>
      <PageHeader
        title="Strike Ladder Matrix"
        subtitle="Every strike the campaign touched, and which condor put it there. Where a long wing meets a lower condor's short at the same strike, the pair cancels and NET reads zero."
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={metrics.real_price_fraction}
          note={provenance.note}
          awaiting={(provenance.awaiting_connection ?? false)}
          hasData={condors.length > 0}
        />

        <StatGrid>
          <Stat label="Offset ratio" value={pct(netting.offset_ratio)} hint="of gross quantity self-hedged" />
          <Stat label="Strikes touched" value={num(netting.strikes_touched)} />
          <Stat label="Fully offset" value={num(netting.strikes_fully_offset)} hint="net exactly zero" />
          <Stat label="Gross qty" value={num(netting.gross_qty)} hint="sum of all legs" />
          <Stat label="Net qty" value={num(netting.net_qty)} hint="what the broker carries" />
        </StatGrid>

        <Card
          title="Positions by strike"
          hint={`Level structure: short at ${num(params.short_offset)} points, long wing at ${num(params.long_offset)} points. A ${num(params.step)}-point step means condor L's long put shares a strike with condor L-${num(params.step * 2)}'s short put.`}
        >
          <StrikeMatrix rows={strike_matrix} condors={condors} />
        </Card>

        <Card
          title="Why the offsetting happens"
          hint="Worked through with the default parameters."
        >
          <div className="scroll-x">
            <table style={{ minWidth: 560 }}>
              <thead>
                <tr>
                  <th>Level</th>
                  <th>Long PE</th>
                  <th>Short PE</th>
                  <th>Short CE</th>
                  <th>Long CE</th>
                </tr>
              </thead>
              <tbody>
                {[0, 1, 2, 3].map((i) => {
                  const level = 24000 - i * params.step;
                  return (
                    <tr key={level}>
                      <td className="tnum" style={{ fontWeight: 700 }}>{num(level)}</td>
                      <td className="tnum">{num(level - params.long_offset)}</td>
                      <td className="tnum">{num(level - params.short_offset)}</td>
                      <td className="tnum">{num(level + params.short_offset)}</td>
                      <td className="tnum">{num(level + params.long_offset)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7, margin: "14px 0 0", maxWidth: "80ch" }}>
            Read down the <strong>Long PE</strong> column and across the <strong>Short PE</strong> column:
            condor 24,000 buys the 23,600 put, and condor 23,800 sells it. Same strike, opposite sides,
            equal size &mdash; the pair nets flat. The same happens on the call side two condors later.
            Offsetting is always <strong>within one expiry</strong>: a long 23,600 PE in March and a
            short 23,600 PE in April are different instruments and net to nothing, so the ladder
            re-anchors at each new expiry rather than carrying a stale reference level across one.
            Across this campaign the mechanism cancelled{" "}
            <strong>{pct(netting.offset_ratio)}</strong> of everything traded, which is why the book stays
            far smaller than the {condors.length}&times;4 = {condors.length * 4} legs suggest.
          </p>
        </Card>
      </div>
    </>
  );
}
