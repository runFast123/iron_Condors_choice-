export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { dateTime, num, pct } from "@/lib/format";
import { AwaitingConnection, Badge, Card, Empty, PageHeader, Stat, StatGrid } from "@/components/ui";

export default async function DataHealthPage() {
  const { provenance, warnings, skipped, triggers } = await getDataset();
  const p = provenance.provider;
  const awaiting = (provenance.awaiting_connection ?? false);
  const failures = provenance.failures ?? [];
  const legsTotal = provenance.legs_requested ?? 0;
  const legsReal = provenance.legs_with_choice_data ?? 0;

  return (
    <>
      <PageHeader
        title="Data Health"
        subtitle="Choice FinX is the only data source this project uses, for both historical and live prices. A failed fetch is shown here as a failure, with the broker's own message, rather than silently becoming an empty result."
      />

      <div style={{ display: "grid", gap: 16 }}>
        {awaiting && <AwaitingConnection note={provenance.note} />}

        {!awaiting && (
          <StatGrid>
            <Stat label="Spot bars" value={num(provenance.bars)} hint={provenance.spot_source} />
            <Stat
              label="Option legs"
              value={`${num(legsReal)}/${num(legsTotal)}`}
              tone={legsTotal > 0 && legsReal === legsTotal ? "pos" : "neg"}
              hint="with Choice history"
            />
            <Stat label="Quotes" value={num(p.total_quotes)} hint="premium lookups" />
            <Stat
              label="Real (Choice)"
              value={num(p.real_quotes)}
              tone={p.real_quotes > 0 ? "pos" : "neg"}
              hint={pct(p.real_fraction)}
            />
            <Stat
              label="Modeled"
              value={num(p.modeled_quotes)}
              tone={p.modeled_quotes > 0 ? "neg" : "pos"}
              hint={pct(1 - p.real_fraction)}
            />
            <Stat label="Ladder triggers" value={num(triggers.length)} hint="condors fired" />
          </StatGrid>
        )}

        <Card
          title="Sources"
          hint="Every series below is served by Choice FinX. There is no third-party data vendor in this project."
          pad={0}
        >
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Series</th>
                  <th>Choice source</th>
                  <th>Status</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                <Row
                  name="NIFTY spot"
                  source={provenance.spot_source}
                  ok={!awaiting}
                  awaiting={awaiting}
                  note="Index token resolved from the scrip master, candles via api/OpenGraph/ChartData."
                />
                <Row
                  name="India VIX"
                  source={provenance.vol_source}
                  ok={!awaiting && provenance.vol_source.startsWith("choice")}
                  awaiting={awaiting}
                  note="Drives the at-the-money volatility level. Falls back to a flat default only if Choice has no INDIAVIX series."
                />
                <Row
                  name="Option premiums"
                  source={provenance.premium_source}
                  ok={!awaiting && legsTotal > 0 && legsReal === legsTotal}
                  awaiting={awaiting}
                  note="Historical candles per option leg. Any leg Choice cannot serve is modeled with Black-76 and badged MODELED."
                />
                <Row
                  name="Expiries & strikes"
                  source={provenance.expiry_source ?? "choice:scripmaster"}
                  ok={!awaiting}
                  awaiting={awaiting}
                  note="Real listed contracts from the daily scrip master, including lot size and the strike grid."
                />
                <Row
                  name="Live quotes"
                  source="choice:MultipleTouchline + price feed"
                  ok={false}
                  awaiting={awaiting}
                  note="Snapshot LTP and the FIX3.0 streaming feed. Used by forward testing, which needs a live session."
                />
              </tbody>
            </table>
          </div>
        </Card>

        {failures.length > 0 && (
          <Card
            title="Failed fetches"
            hint="Exactly what Choice said. This is the diagnosis the upstream SDK threw away by returning an empty frame."
            pad={0}
          >
            <div className="scroll-x" style={{ maxHeight: "40vh", overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th>Token</th>
                    <th>Resolution</th>
                    <th>Range</th>
                    <th>Status</th>
                    <th>Choice error</th>
                  </tr>
                </thead>
                <tbody>
                  {failures.map((f, i) => (
                    <tr key={i}>
                      <td className="tnum">{f.token}</td>
                      <td>{f.resolution}</td>
                      <td style={{ color: "var(--ink-2)" }}>{f.range}</td>
                      <td>
                        <Badge tone={f.status === "no_data" ? "warn" : "neg"}>{f.status}</Badge>
                      </td>
                      <td style={{ whiteSpace: "normal", maxWidth: "60ch", color: "var(--ink-muted)" }}>
                        {f.error}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        <Card title="Warnings" hint="Raised by the backtest engine during this run.">
          {warnings.length === 0 ? (
            <Empty>No warnings.</Empty>
          ) : (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
        </Card>

        {!awaiting && (
          <Card
            title="Skipped condors"
            hint="A condor is skipped outright rather than opened partially: three of four legs would leave a naked short in the book."
          >
            {skipped.length === 0 ? (
              <Empty>No condors were skipped &mdash; every trigger got a full four-leg fill.</Empty>
            ) : (
              <div className="scroll-x">
                <table>
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Level</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {skipped.map(([when, level, why], i) => (
                      <tr key={i}>
                        <td>{dateTime(when)}</td>
                        <td className="tnum">{num(level)}</td>
                        <td style={{ color: "var(--ink-muted)" }}>{why}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        )}

        <p style={{ fontSize: 11.5, color: "var(--ink-muted)", margin: 0 }}>
          Dataset generated {dateTime(provenance.generated_at)}.
        </p>
      </div>
    </>
  );
}

function Row({
  name,
  source,
  ok,
  awaiting,
  note,
}: {
  name: string;
  source: string;
  ok: boolean;
  awaiting: boolean;
  note: string;
}) {
  const badge = awaiting ? (
    <Badge tone="brand">NOT CONNECTED</Badge>
  ) : ok ? (
    <Badge tone="pos">CHOICE</Badge>
  ) : (
    <Badge tone="warn">PARTIAL</Badge>
  );
  return (
    <tr>
      <td style={{ fontWeight: 600 }}>{name}</td>
      <td className="mono" style={{ fontSize: 11.5 }}>
        {source}
      </td>
      <td>{badge}</td>
      <td style={{ whiteSpace: "normal", maxWidth: "50ch", color: "var(--ink-muted)" }}>{note}</td>
    </tr>
  );
}
