import { getDataset } from "@/lib/data";
import { dateTime, num, pct } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, StatGrid } from "@/components/ui";

export default function DataHealthPage() {
  const { provenance, warnings, skipped, triggers } = getDataset();
  const p = provenance.provider;

  return (
    <>
      <PageHeader
        title="Data Health"
        subtitle="Where every number on this site came from, and what is missing. A failed fetch is shown as a failure here rather than silently becoming an empty result."
      />

      <div style={{ display: "grid", gap: 16 }}>
        <StatGrid>
          <Stat label="Spot bars" value={num(provenance.bars)} hint={provenance.spot_source} />
          <Stat label="Option quotes" value={num(p.total_quotes)} hint="premium lookups" />
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
          <Stat label="Ladder triggers" value={num(triggers.length)} hint="rungs fired" />
          <Stat
            label="Skipped rungs"
            value={num(skipped.length)}
            tone={skipped.length ? "neg" : "pos"}
            hint="no price available"
          />
        </StatGrid>

        <Card
          title="Sources"
          hint="Choice FinX is the only permitted market-data source. Yahoo is a strictly underlying-only fallback."
          pad={0}
        >
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Series</th>
                  <th>Source</th>
                  <th>Status</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style={{ fontWeight: 600 }}>NIFTY spot</td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{provenance.spot_source}</td>
                  <td><Badge tone="warn">FALLBACK</Badge></td>
                  <td style={{ whiteSpace: "normal", maxWidth: "44ch", color: "var(--ink-muted)" }}>
                    Real index history. Choice ChartData replaces this once credentials and the
                    declared static IP are configured.
                  </td>
                </tr>
                <tr>
                  <td style={{ fontWeight: 600 }}>India VIX</td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{provenance.vol_source}</td>
                  <td><Badge tone="warn">FALLBACK</Badge></td>
                  <td style={{ whiteSpace: "normal", maxWidth: "44ch", color: "var(--ink-muted)" }}>
                    Drives the at-the-money volatility level for modeled premiums.
                  </td>
                </tr>
                <tr>
                  <td style={{ fontWeight: 600 }}>Option premiums</td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{provenance.premium_source}</td>
                  <td><Badge tone="neg">MODELED</Badge></td>
                  <td style={{ whiteSpace: "normal", maxWidth: "44ch", color: "var(--ink-muted)" }}>
                    Yahoo carries no Indian option chain, so there is no market premium to fall back
                    to. Black-76 with a strike skew is used instead.
                  </td>
                </tr>
                <tr>
                  <td style={{ fontWeight: 600 }}>Expiries</td>
                  <td className="mono" style={{ fontSize: 11.5 }}>synthesised weekly</td>
                  <td><Badge tone="warn">SYNTHETIC</Badge></td>
                  <td style={{ whiteSpace: "normal", maxWidth: "44ch", color: "var(--ink-muted)" }}>
                    Generated on a fixed weekday. Real expiries come from the Choice scrip master.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </Card>

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

        <Card
          title="Skipped rungs"
          hint="A rung is skipped outright rather than opened partially: three of four legs would leave a naked short in the book."
        >
          {skipped.length === 0 ? (
            <Empty>No rungs were skipped &mdash; every trigger got a full four-leg fill.</Empty>
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

        <Card
          title="Connecting Choice FinX"
          hint="What has to be true before these numbers become broker-verified."
        >
          <ol
            style={{
              margin: 0,
              paddingLeft: 18,
              fontSize: 12.5,
              lineHeight: 1.9,
              color: "var(--ink-2)",
              maxWidth: "82ch",
            }}
          >
            <li>
              Generate an API key at finx.choiceindia.com &rarr; Profile &rarr; Settings &rarr;
              Generate API Key.
            </li>
            <li>
              Declare the <strong>static IP</strong> of the machine that will run the engine.
              Requests from any other IP are rejected, and VPNs or proxies always fail this check.
            </li>
            <li>
              Fill <code className="mono">CHOICE_VENDOR_ID</code>,{" "}
              <code className="mono">CHOICE_API_KEY</code> and{" "}
              <code className="mono">CHOICE_MOBILE_NO</code> in <code className="mono">.env</code> on
              that machine.
            </li>
            <li>
              Run <code className="mono">python -m engine.tools.doctor</code> to verify login, scrip
              master, option resolution and ChartData end to end.
            </li>
            <li>
              Re-run <code className="mono">python -m engine.tools.seed</code> to rebuild this
              dataset with real premiums.
            </li>
          </ol>
          <p style={{ fontSize: 12, color: "var(--ink-muted)", marginTop: 12, marginBottom: 0 }}>
            Generated {dateTime(provenance.generated_at)}.
          </p>
        </Card>
      </div>
    </>
  );
}
