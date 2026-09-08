export const dynamic = "force-dynamic";

import Link from "next/link";
import { getLiveState } from "@/lib/live";
import { dateTime, inr, num } from "@/lib/format";
import { Badge, Card, Empty, PageHeader } from "@/components/ui";
import { ForwardControl } from "@/components/ForwardControl";

export default async function ForwardPage() {
  const { state, engineError } = await getLiveState();
  const positions = state?.positions ?? [];
  const open = positions.filter((p) => p.status === "OPEN");
  const ladder = state?.ladder;

  return (
    <>
      <PageHeader
        title="Forward Test"
        subtitle="The same ladder engine as the backtest, driven by live Choice quotes instead of historical bars. Start it here — nothing needs to be run from a terminal."
      />

      <div style={{ display: "grid", gap: 16 }}>
        {engineError && (
          <div className="card" style={{ padding: "11px 14px", borderColor: "var(--warn)", fontSize: 12.5 }}>
            <strong>Engine unreachable.</strong>{" "}
            <span style={{ color: "var(--ink-muted)" }}>{engineError}</span>
          </div>
        )}

        <ForwardControl initial={state} />

        {ladder && ladder.fired.length > 0 && (
          <Card title="Ladder state" hint="Levels already opened this session. Each fires at most once.">
            <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
              {ladder.fired.map((lv) => (
                <Badge key={lv} tone="brand">{num(lv)}</Badge>
              ))}
            </div>
            {ladder.anchor != null && (
              <p style={{ fontSize: 12, color: "var(--ink-muted)", margin: "10px 0 0" }}>
                Anchored at <strong>{num(ladder.anchor)}</strong>, stepping {num(ladder.step)} points.
              </p>
            )}
          </Card>
        )}

        <Card
          title="Open positions"
          pad={0}
          hint="Live condors with their entry credit and worst case. Legs show the Choice token each was filled on."
        >
          {open.length === 0 ? (
            <Empty>
              No open positions. The anchor condor opens on the first tick once a run is started.
            </Empty>
          ) : (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>Level</th><th>Opened</th><th>Expiry</th>
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

        <Card title="What protects you" hint="These apply automatically to every run.">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li><strong>Paper by default.</strong> A live run still places nothing until you press Arm, which asks for confirmation.</li>
            <li><strong>All-or-nothing entries.</strong> If any of the four legs has no live quote, the condor is skipped rather than half-opened, so no naked short is ever created.</li>
            <li><strong>Protective legs first.</strong> The long wings are bought before the shorts are sold, so margin never spikes mid-structure.</li>
            <li><strong>Limit orders only.</strong> Choice supports no market order, so each leg is priced through the touch by a slippage buffer.</li>
            <li><strong>Loss kill switch.</strong> Breaching the daily loss limit disarms the run and stops new entries.</li>
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
