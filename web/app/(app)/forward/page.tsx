import Link from "next/link";
import { getLiveState } from "@/lib/live";
import { dateTime, inr, num, pct } from "@/lib/format";
import { AwaitingConnection, Badge, Card, Empty, PageHeader, Stat, StatGrid } from "@/components/ui";

export default function ForwardPage() {
  const s = getLiveState();
  const { session, market, ladder, pnl, positions, netting } = s;
  const connected = session.connected;
  const open = positions.filter((p) => p.status === "OPEN");

  return (
    <>
      <PageHeader
        title="Forward Test"
        subtitle="The same ladder engine as the backtest, driven by live Choice quotes instead of historical bars. Paper mode simulates fills at the live LTP; live mode places real orders and must be armed explicitly."
        right={
          <div style={{ display: "flex", gap: 7, alignItems: "center" }}>
            <Badge tone={session.market_open ? "pos" : "neutral"}>
              {session.market_open ? "MARKET OPEN" : "MARKET CLOSED"}
            </Badge>
            <Badge tone={session.mode === "live" ? "neg" : "brand"}>{session.mode.toUpperCase()}</Badge>
            <Badge tone={connected ? (session.status === "running" ? "pos" : "warn") : "neutral"}>
              {connected ? session.status.toUpperCase() : "DISCONNECTED"}
            </Badge>
          </div>
        }
      />

      <div style={{ display: "grid", gap: 16 }}>
        {!connected && (
          <AwaitingConnection
            note={
              session.stopped_reason ??
              "Forward testing needs a live Choice session, which requires credentials and must run from the declared static IP."
            }
          />
        )}

        {!connected && (
          <Card title="Start a forward test" hint="Once credentials are in place on the static-IP machine.">
            <div className="mono" style={{ fontSize: 12, lineHeight: 2, color: "var(--ink-2)" }}>
              python -m engine.tools.doctor
              <span style={{ color: "var(--ink-muted)" }}> &nbsp;# verify the connection</span>
              <br />
              python -m engine.tools.live
              <span style={{ color: "var(--ink-muted)" }}> &nbsp;# paper mode, polls every 15s</span>
              <br />
              python -m engine.tools.live --mode live --arm
              <span style={{ color: "var(--neg)" }}> &nbsp;# REAL ORDERS</span>
            </div>
            <p style={{ fontSize: 12, color: "var(--ink-muted)", margin: "12px 0 0", lineHeight: 1.6, maxWidth: "82ch" }}>
              The runner writes <code className="mono">web/data/live.json</code> on every tick, which is what
              this page reads. Live mode refuses to place orders without <code className="mono">--arm</code>,
              caps concurrent rungs, and trips a daily-loss kill switch.
            </p>
          </Card>
        )}

        {connected && (
          <>
            <StatGrid>
              <Stat label="NIFTY" value={market.spot != null ? num(market.spot) : "--"}
                    hint={market.ts ? dateTime(market.ts) : "no tick yet"} />
              <Stat label="Total P&L" value={inr(pnl.total, { sign: true })}
                    tone={pnl.total > 0 ? "pos" : pnl.total < 0 ? "neg" : "neutral"}
                    delta={`${inr(pnl.realised, { sign: true })} realised`} />
              <Stat label="Open rungs" value={num(pnl.open_rungs)} hint={`${pnl.total_rungs} opened today`} />
              <Stat label="Next trigger" value={ladder.next_trigger != null ? num(ladder.next_trigger) : "--"}
                    hint={ladder.distance != null ? `${num(ladder.distance)} pts away` : undefined} />
              <Stat label="Anchor" value={ladder.anchor != null ? num(ladder.anchor) : "--"}
                    hint={`${num(ladder.step)}-pt steps`} />
              <Stat label="Self-hedged" value={pct(netting.offset_ratio)} hint="of gross quantity" />
            </StatGrid>

            <Card title="Ladder state" hint="Levels already fired this session. Each fires at most once.">
              {ladder.fired.length === 0 ? (
                <Empty>No rungs fired yet. The anchor opens on the first tick.</Empty>
              ) : (
                <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
                  {ladder.fired.map((lv) => (
                    <Badge key={lv} tone="brand">{num(lv)}</Badge>
                  ))}
                </div>
              )}
            </Card>

            <Card title="Open positions" pad={0}
                  hint="Live rungs with their entry credit and worst case. Legs show the Choice token they were filled on.">
              {open.length === 0 ? (
                <Empty>No open positions.</Empty>
              ) : (
                <div className="scroll-x">
                  <table>
                    <thead>
                      <tr>
                        <th>Rung</th><th>Opened</th><th>Expiry</th>
                        <th style={{ textAlign: "right" }}>Credit</th>
                        <th style={{ textAlign: "right" }}>Max loss</th>
                        <th>Legs</th>
                      </tr>
                    </thead>
                    <tbody>
                      {open.map((p) => (
                        <tr key={p.index}>
                          <td className="tnum" style={{ fontWeight: 700 }}>{num(p.level)}</td>
                          <td style={{ color: "var(--ink-2)" }}>{dateTime(p.entry_time)}</td>
                          <td style={{ color: "var(--ink-2)" }}>{p.expiry}</td>
                          <td className="tnum" style={{ textAlign: "right" }}>{inr(p.credit)}</td>
                          <td className="tnum" style={{ textAlign: "right", color: "var(--neg)" }}>
                            {inr(-p.max_loss)}
                          </td>
                          <td>
                            <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                              {p.legs.map((l, i) => (
                                <Badge key={i} tone={l.side === "SELL" ? "warn" : "brand"}>
                                  {l.side === "SELL" ? "S" : "B"} {num(l.strike)}{l.right} @{l.entry_price.toFixed(2)}
                                </Badge>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </>
        )}

        <Card title="Safety" hint="What stops a forward run from doing damage.">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li><strong>Paper by default.</strong> Live orders require <code className="mono">--mode live --arm</code>; the flag alone is not enough.</li>
            <li><strong>All-or-nothing rungs.</strong> If any of the four legs has no live quote, the rung is skipped rather than half-opened, so no naked short is ever created.</li>
            <li><strong>Wings first.</strong> Protective longs are sent before the shorts, so margin never spikes mid-structure.</li>
            <li><strong>Limit orders only.</strong> Choice supports no market order, so each leg is priced through the touch by a slippage buffer.</li>
            <li><strong>Kill switch.</strong> A daily-loss breach disarms the runner and stops new rungs.</li>
          </ul>
          <p style={{ margin: "12px 0 0", fontSize: 12 }}>
            <Link href="/forward/log" style={{ color: "var(--brand)", fontWeight: 600 }}>
              Activity log &amp; trade history &rarr;
            </Link>
          </p>
        </Card>
      </div>
    </>
  );
}
