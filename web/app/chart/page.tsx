import { getDataset } from "@/lib/data";
import { num, shortDate } from "@/lib/format";
import { Badge, Card, PageHeader, Stat, StatGrid } from "@/components/ui";
import { PriceChart } from "@/components/charts/PriceChart";

export default function ChartPage() {
  const { equity, triggers, params } = getDataset();
  const spots = equity.map((p) => p.spot).filter((s): s is number => s != null);
  const high = Math.max(...spots);
  const low = Math.min(...spots);
  const last = spots[spots.length - 1];
  const anchor = triggers[0]?.level;
  const deepest = triggers.length ? Math.min(...triggers.map((t) => t.level)) : null;

  return (
    <>
      <PageHeader
        title="Price &amp; Trigger Levels"
        subtitle={`NIFTY with every fired rung drawn as a dashed level. A rung fires the moment price trades at or below its level, and each level fires at most once.`}
      />

      <div style={{ display: "grid", gap: 16 }}>
        <StatGrid>
          <Stat label="Last" value={num(last)} hint={shortDate(equity[equity.length - 1].ts)} />
          <Stat label="Range high" value={num(high)} />
          <Stat label="Range low" value={num(low)} />
          <Stat label="Anchor rung" value={anchor ? num(anchor) : "--"} hint="first condor" />
          <Stat label="Deepest rung" value={deepest ? num(deepest) : "--"}
                hint={anchor && deepest ? `${num(anchor - deepest)} pts below anchor` : undefined} />
          <Stat label="Rungs fired" value={num(triggers.length)} hint={`${num(params.step)}-pt steps`} />
        </StatGrid>

        <Card
          title="NIFTY with ladder levels"
          hint="Drag to pan, scroll to zoom. Dashed lines mark the reference level of each condor; labels on the right axis are rung numbers."
          pad={8}
        >
          <PriceChart points={equity} triggers={triggers} />
        </Card>

        <Card title="Trigger log" pad={0}
              hint="A gap-fill entry means price jumped past a level, so the rung it skipped was opened at the same bar.">
          <div className="scroll-x" style={{ maxHeight: "50vh", overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Level</th>
                  <th>Fired at</th>
                  <th style={{ textAlign: "right" }}>NIFTY then</th>
                  <th>Trigger</th>
                  <th>Short PE</th>
                  <th>Long PE</th>
                  <th>Short CE</th>
                  <th>Long CE</th>
                </tr>
              </thead>
              <tbody>
                {triggers.map((t, i) => (
                  <tr key={`${t.level}-${t.time}`}>
                    <td className="tnum" style={{ color: "var(--ink-muted)" }}>{i + 1}</td>
                    <td className="tnum" style={{ fontWeight: 700 }}>{num(t.level)}</td>
                    <td style={{ color: "var(--ink-2)" }}>{shortDate(t.time)}</td>
                    <td className="tnum" style={{ textAlign: "right" }}>{num(t.spot)}</td>
                    <td>
                      <Badge tone={t.reason === "anchor" ? "brand" : t.reason === "gap-fill" ? "warn" : "neutral"}>
                        {t.reason}
                      </Badge>
                    </td>
                    <td className="tnum" style={{ color: "var(--c2)" }}>{num(t.level - params.short_offset)}</td>
                    <td className="tnum" style={{ color: "var(--c1)" }}>{num(t.level - params.long_offset)}</td>
                    <td className="tnum" style={{ color: "var(--c2)" }}>{num(t.level + params.short_offset)}</td>
                    <td className="tnum" style={{ color: "var(--c1)" }}>{num(t.level + params.long_offset)}</td>
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
