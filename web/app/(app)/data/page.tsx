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
  const backup = provenance.backup;
  const gate = provenance.vix_gate;
  const settlement = provenance.settlement;
  // Results from before the backup existed carry only the combined count.
  const choiceQuotes = p.choice_quotes ?? p.real_quotes;
  const backupQuotes = p.backup_quotes ?? 0;

  return (
    <>
      <PageHeader
        title="Data Health"
        subtitle="Choice FinX is the source for every price, historical and live. A backtest turns to its backup source only for what Choice did not supply — an option contract with no usable history, or a trading day with no NIFTY or India VIX bars — and everything it took is listed here. Live runs never use it. A failed fetch is shown as a failure, with the broker's own message, rather than silently becoming an empty result."
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
              value={num(choiceQuotes)}
              tone={choiceQuotes > 0 ? "pos" : "neg"}
              hint={pct(p.total_quotes ? choiceQuotes / p.total_quotes : 0)}
            />
            {backupQuotes > 0 && (
              <Stat
                label="Real (backup)"
                value={num(backupQuotes)}
                tone="pos"
                hint={pct(p.backup_fraction ?? 0)}
              />
            )}
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
          hint="Every series below is served by Choice FinX, except where a badge says BACKUP: the backtest's backup source, used only where Choice had nothing."
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
                  note="Index token resolved from the scrip master, candles via api/OpenGraph/ChartData. A trading day Choice returned no bars for is taken from the backup source and listed below."
                />
                <Row
                  name="India VIX"
                  source={provenance.vol_source}
                  ok={!awaiting && provenance.vol_source.startsWith("choice")}
                  awaiting={awaiting}
                  note="Sets the at-the-money volatility of every modelled premium, read as it stood at that moment rather than at the day's close. A day Choice has no data for is taken from the backup source; a flat default is used only if neither has a series."
                />
                {gate && (
                  <Row
                    name="India VIX, entry rule"
                    source={`INDIAVIX ${gate.resolution === "D" ? "daily" : `${gate.resolution}-min`} bars`}
                    ok={!awaiting && (gate.readings.backup ?? 0) === 0 && gate.bars_without_vix === 0}
                    awaiting={awaiting}
                    fromBackup={(gate.readings.backup ?? 0) > 0}
                    note={`No new positions while VIX was above ${gate.limit}: paused on ${pct(gate.paused_fraction)} of bars (${num(gate.paused_bars)} of ${num(gate.bars)}) in ${num(gate.spells)} spell${gate.spells === 1 ? "" : "s"}; ${num(gate.levels_passed)} level${gate.levels_passed === 1 ? "" : "s"} passed while paused.${gate.bars_without_vix ? ` ${num(gate.bars_without_vix)} bars had no reading and could not open anything.` : ""}`}
                  />
                )}
                <Row
                  name="Option premiums"
                  source={provenance.premium_source}
                  ok={!awaiting && legsTotal > 0 && legsReal === legsTotal}
                  awaiting={awaiting}
                  fromBackup={(provenance.legs_backup ?? 0) > 0}
                  note={`Historical candles per option leg. A leg Choice has no usable history for is taken from the backup source where it has one (badged BACKUP${provenance.legs_backup ? `; ${num(provenance.legs_backup)} leg(s) in this run` : ""}), and modelled with Black-76 otherwise (badged MODELED).`}
                />
                {settlement && (
                  <Row
                    name="Expiry settlement"
                    source="NIFTY official close"
                    ok={!awaiting && settlement.last_bar.length === 0}
                    awaiting={awaiting}
                    fromBackup={(backup?.settlement_days?.length ?? 0) > 0}
                    note={`What NSE settles index options against, from Choice's daily candle. ${num(settlement.official_close)} expir${settlement.official_close === 1 ? "y" : "ies"} settled on it${settlement.last_bar.length ? `; ${settlement.last_bar.join(", ")} had no official close and settled at the last bar` : ""}.`}
                  />
                )}
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

        {backup && (backup.used || backup.notes.length > 0) && (
          <Card
            title="Backup data"
            hint="What Choice did not supply, and what filled it. Everything here is a real traded price; nothing from the backup source is ever used in a live run."
          >
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
              {(backup.option_legs_used ?? 0) > 0 && (
                <li>
                  Option legs priced from the backup source: {num(backup.option_legs_used ?? 0)} (of{" "}
                  {num(backup.option_legs_asked ?? 0)} Choice had no usable history for)
                </li>
              )}
              {backup.nifty_days.length > 0 && (
                <li>NIFTY from the backup source on {backup.nifty_days.length} day(s): {backup.nifty_days.join(", ")}</li>
              )}
              {backup.vix_bar_days.length > 0 && (
                <li>India VIX bars from the backup source on {backup.vix_bar_days.length} day(s): {backup.vix_bar_days.join(", ")}</li>
              )}
              {backup.vix_close_days.length > 0 && (
                <li>India VIX closes from the backup source on {backup.vix_close_days.length} day(s): {backup.vix_close_days.join(", ")}</li>
              )}
              {(backup.settlement_days?.length ?? 0) > 0 && (
                <li>Official closes from the backup source for expiry {backup.settlement_days!.join(", ")}</li>
              )}
              {backup.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </Card>
        )}

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
  fromBackup,
}: {
  name: string;
  source: string;
  ok: boolean;
  awaiting: boolean;
  note: string;
  /** Some of this series came from the backup source. Read from the source
   *  label when not given. */
  fromBackup?: boolean;
}) {
  const backup = fromBackup ?? source.includes("backup");
  const badge = awaiting ? (
    <Badge tone="brand">NOT CONNECTED</Badge>
  ) : backup ? (
    <Badge tone="brand">{source.startsWith("backup") ? "BACKUP" : "CHOICE + BACKUP"}</Badge>
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
