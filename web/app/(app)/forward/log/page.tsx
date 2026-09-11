import { getLiveState } from "@/lib/live";
import { dateTime, inr, num } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Stat, StatGrid } from "@/components/ui";
import { LiveCondorBlotter } from "@/components/LiveCondorBlotter";

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
          hint="One row per condor. Click one to see its legs: what each filled at, and CMP, where that contract trades now as of the last tick, coloured by whether the move since the fill helped or hurt that side. A closed condor holds eight fills, four going in and four coming out. Only legs of open condors are still quoted, so closed ones show --."
        >
          {positions.length === 0 ? (
            <Empty>
              No fills yet. Trade history appears here once a forward run opens its first condor.
            </Empty>
          ) : (
            <LiveCondorBlotter
              positions={positions}
              fills={fills}
              marks={marks}
              pnl={pnl}
            />
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
