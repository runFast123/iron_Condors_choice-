import { getLiveState, type LiveFill } from "@/lib/live";
import { dateTime, inr, num } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, StatGrid } from "@/components/ui";

const LEVEL_TONE = {
  trade: "pos",
  warn: "warn",
  error: "neg",
  info: "neutral",
} as const;

export const dynamic = "force-dynamic";

export default async function ForwardLogPage() {
  const { state } = await getLiveState();
  const session = state?.session ?? null;
  const events = state?.events ?? [];
  const fills = state?.fills ?? [];
  const marks = state?.marks ?? {};

  /** Where this contract trades now, or null if it has not been marked. */
  const markOf = (f: LiveFill): number | null =>
    f.token != null && marks[String(f.token)] != null ? marks[String(f.token)] : null;

  /**
   * Green when the move since the fill helped, red when it hurt.
   *
   * Which direction is "good" depends on the side, not on whether the number
   * went up: a short leg gains when its premium falls. Colouring by raw
   * direction would tell half the rows the opposite of the truth.
   */
  const cmpColour = (f: LiveFill): string => {
    const mark = markOf(f);
    if (mark == null) return "var(--ink-muted)";
    const move = mark - f.price;
    const favourable = f.side === "SELL" ? -move : move;
    if (Math.abs(move) < 0.005) return "var(--ink)";
    return favourable > 0 ? "var(--pos)" : "var(--neg)";
  };

  const cmpTitle = (f: LiveFill): string => {
    const mark = markOf(f);
    if (mark == null) return "Not marked yet — the next tick will price it.";
    const move = mark - f.price;
    const per = move >= 0 ? `+${move.toFixed(2)}` : move.toFixed(2);
    const total = (f.side === "SELL" ? -move : move) * f.qty;
    return `${per} per share since the fill — ${total >= 0 ? "+" : ""}${total.toFixed(0)} on this leg`;
  };
  const positions = state?.positions ?? [];
  const pnl = state?.pnl ?? { realised: 0, unrealised: 0, total: 0, open_condors: 0, total_condors: 0 };
  const closed = positions.filter((p) => p.status !== "OPEN");
  const opens = fills.filter((f) => f.action === "OPEN").length;
  const closes = fills.filter((f) => f.action === "CLOSE").length;

  return (
    <>
      <PageHeader
        title="Activity Log &amp; Trade History"
        subtitle="Every tick, trigger, fill and rejection the forward runner recorded, newest first. This is the audit trail — Choice requires API users to retain their own request logs."
        right={
          <Badge tone={session ? "pos" : "neutral"}>
            {session ? session.mode.toUpperCase() : "NO RUN YET"}
          </Badge>
        }
      />

      <div style={{ display: "grid", gap: 16 }}>
        <StatGrid>
          <Stat label="Log entries" value={num(events.length)} />
          <Stat label="Fills" value={num(fills.length)} hint={`${opens} open / ${closes} close`} />
          <Stat label="Condors closed" value={num(closed.length)} />
          <Stat label="Realised P&L" value={inr(pnl.realised, { sign: true })}
                tone={pnl.realised > 0 ? "pos" : pnl.realised < 0 ? "neg" : "neutral"} />
          <Stat label="Last tick" value={session?.last_tick ? dateTime(session.last_tick) : "--"} />
        </StatGrid>

        <Card
          title="Trade history"
          pad={0}
          hint="One row per leg fill. PRICE is what it filled at; CMP is where that contract trades now, as of the last tick, coloured by whether the move since the fill helped or hurt that side. Only legs of open condors are still quoted, so closed ones show --."
        >
          {fills.length === 0 ? (
            <Empty>
              No fills yet. Trade history appears here once a forward run opens its first condor.
            </Empty>
          ) : (
            <div className="scroll-x" style={{ maxHeight: "56vh", overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Action</th>
                    <th>Condor</th>
                    <th>Side</th>
                    <th>Strike</th>
                    <th style={{ textAlign: "right" }}>Qty</th>
                    <th style={{ textAlign: "right" }}>Price</th>
                    <th style={{ textAlign: "right" }}>Value</th>
                    <th style={{ textAlign: "right" }}>CMP</th>
                    <th>Token</th>
                    <th>Mode</th>
                  </tr>
                </thead>
                <tbody>
                  {fills.map((f, i) => (
                    <tr key={i}>
                      <td style={{ color: "var(--ink-2)" }}>{dateTime(f.ts)}</td>
                      <td>
                        <Badge tone={f.action === "OPEN" ? "brand" : "neutral"}>{f.action}</Badge>
                      </td>
                      <td className="tnum">{num(f.condor_level)}</td>
                      <td>
                        <Badge tone={f.side === "SELL" ? "warn" : "brand"}>
                          {f.side} {f.right}
                        </Badge>
                      </td>
                      <td className="tnum" style={{ fontWeight: 600 }}>{num(f.strike)}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{num(f.qty)}</td>
                      <td className="tnum" style={{ textAlign: "right" }}>{f.price.toFixed(2)}</td>
                      <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                        {inr(f.price * f.qty)}
                      </td>
                      <td
                        className="tnum"
                        style={{ textAlign: "right", fontWeight: 600, color: cmpColour(f) }}
                        title={cmpTitle(f)}
                      >
                        {markOf(f) == null ? "--" : num(markOf(f) as number, 2)}
                      </td>
                      <td className="tnum mono" style={{ fontSize: 11, color: "var(--ink-muted)" }}>
                        {f.token ?? "--"}
                      </td>
                      <td>
                        <Badge tone={f.mode === "live" ? "neg" : "neutral"}>{f.mode}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title="Closed condors"
          pad={0}
          hint="Completed condors with the reason each one was closed."
        >
          {closed.length === 0 ? (
            <Empty>No condors closed yet.</Empty>
          ) : (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>Condor</th><th>Opened</th><th>Status</th>
                    <th style={{ textAlign: "right" }}>Credit</th>
                    <th style={{ textAlign: "right" }}>P&amp;L</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {closed.map((p) => (
                    <tr key={p.index}>
                      <td className="tnum" style={{ fontWeight: 700 }}>{num(p.level)}</td>
                      <td style={{ color: "var(--ink-2)" }}>{dateTime(p.entry_time)}</td>
                      <td>
                        <Badge tone={p.status === "CLOSED_TARGET" ? "pos" : "neg"}>
                          {p.status.replace("CLOSED_", "")}
                        </Badge>
                      </td>
                      <td className="tnum" style={{ textAlign: "right" }}>{inr(p.credit)}</td>
                      <td
                        className="tnum"
                        style={{
                          textAlign: "right", fontWeight: 700,
                          color: (p.pnl ?? 0) > 0 ? "var(--pos)" : (p.pnl ?? 0) < 0 ? "var(--neg)" : "var(--ink)",
                        }}
                      >
                        {p.pnl != null ? inr(p.pnl, { sign: true }) : "--"}
                      </td>
                      <td style={{ color: "var(--ink-muted)", whiteSpace: "normal", maxWidth: "40ch" }}>
                        {p.exit_reason ?? "--"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title="Run log"
          pad={0}
          hint="Structured events from the runner: triggers, fills, rejections, warnings and kill-switch trips."
        >
          {events.length === 0 ? (
            <Empty>
              No log entries. The runner writes here on every tick once a forward test is started.
            </Empty>
          ) : (
            <div className="scroll-x" style={{ maxHeight: "50vh", overflowY: "auto" }}>
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Level</th>
                    <th>Message</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((e, i) => (
                    <tr key={i}>
                      <td style={{ color: "var(--ink-2)" }}>{dateTime(e.ts)}</td>
                      <td>
                        <Badge tone={LEVEL_TONE[e.level] ?? "neutral"}>{e.level}</Badge>
                      </td>
                      <td style={{ fontWeight: 500 }}>{e.message}</td>
                      <td
                        className="mono"
                        style={{ fontSize: 11, color: "var(--ink-muted)", whiteSpace: "normal", maxWidth: "52ch" }}
                      >
                        {Object.entries(e.detail ?? {})
                          .map(([k, v]) => `${k}=${String(v)}`)
                          .join("  ") || "--"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
