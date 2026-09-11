export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { num, shortDate } from "@/lib/format";
import { AwaitingConnection, Badge, Card, PageHeader, Stat, StatGrid } from "@/components/ui";
import { PriceChart } from "@/components/charts/PriceChart";

export default async function ChartPage() {
  const { equity, triggers, params, provenance } = await getDataset();
  const awaiting = (provenance.awaiting_connection ?? false);
  const spots = equity.map((p) => p.spot).filter((s): s is number => s != null);
  const hasData = spots.length > 0;

  // Guarded: with no Choice connection these arrays are empty, and spreading
  // an empty array into Math.max yields -Infinity.
  const high = hasData ? Math.max(...spots) : null;
  const low = hasData ? Math.min(...spots) : null;
  const last = hasData ? spots[spots.length - 1] : null;
  const lastTs = equity.length ? equity[equity.length - 1].ts : null;
  const anchor = triggers[0]?.level ?? null;
  const deepest = triggers.length ? Math.min(...triggers.map((t) => t.level)) : null;
  const dash = (v: number | null) => (v == null ? "--" : num(v));

  return (
    <>
      <PageHeader
        title="Price &amp; Trigger Levels"
        subtitle="NIFTY with every fired condor drawn as a dashed level. A condor fires the moment price trades at or below its level, and each level fires at most once."
      />

      <div style={{ display: "grid", gap: 16 }}>
        {awaiting && <AwaitingConnection note={provenance.note} />}

        {!awaiting && (
        <StatGrid>
          <Stat label="Last" value={dash(last)} hint={lastTs ? shortDate(lastTs) : undefined} />
          <Stat label="Range high" value={dash(high)} />
          <Stat label="Range low" value={dash(low)} />
          <Stat label="Anchor condor" value={dash(anchor)} hint="first condor" />
          <Stat label="Deepest condor" value={dash(deepest)}
                hint={anchor != null && deepest != null ? `${num(anchor - deepest)} pts below anchor` : undefined} />
          <Stat label="Condors opened" value={num(triggers.length)} hint={`${num(params.step)}-pt steps`} />
        </StatGrid>
        )}

        {!awaiting && (
        <>
        <Card
          title="NIFTY with ladder levels"
          hint="Drag to pan, scroll to zoom. Dashed lines mark the reference level of each condor; labels on the right axis are condor numbers."
          pad={8}
        >
          <PriceChart points={equity} triggers={triggers} />
        </Card>

        <Card title="Trigger log" pad={0}
              hint="A gap-fill entry means price jumped past a level, so the condor it skipped was opened at the same bar.">
          <div className="scroll-x" style={{ maxHeight: "50vh", overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Level</th>
                  <th>Side</th>
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
                    <td>
                      <Badge tone={t.side === "up" ? "warn" : t.side === "anchor" ? "brand" : "neutral"}>
                        {(t.side ?? "down").toUpperCase()}
                      </Badge>
                    </td>
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
        </>
        )}
      </div>
    </>
  );
}
