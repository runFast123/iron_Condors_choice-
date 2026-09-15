export const dynamic = "force-dynamic";

import Link from "next/link";
import { getForwardRuns, getLiveState } from "@/lib/live";
import { num } from "@/lib/format";
import { Badge, Card, PageHeader } from "@/components/ui";
import { ForwardControl } from "@/components/ForwardControl";
import { RunScope, resolveRun } from "@/components/RunScope";

export default async function ForwardPage({
  searchParams,
}: {
  searchParams: Promise<{ run?: string }>;
}) {
  // The run travels in the query string so a reload, a bookmark or a link to
  // someone else lands on the same test rather than on whichever one happens
  // to be first.
  const { run } = await searchParams;
  const runs = await getForwardRuns();
  const active = resolveRun(run, runs);
  const { state, engineError } = await getLiveState(active);
  const ladder = state?.ladder;

  return (
    <>
      <PageHeader
        title="Forward Test"
        subtitle="The same ladder engine as the backtest, driven by live Choice quotes instead of historical bars. Paper only — this platform places no orders. Start it here; nothing needs to be run from a terminal."
      />

      <div style={{ display: "grid", gap: 16 }}>
        {engineError && (
          <div className="card" style={{ padding: "11px 14px", borderColor: "var(--warn)", fontSize: 12.5 }}>
            <strong>Engine unreachable.</strong>{" "}
            <span style={{ color: "var(--ink-muted)" }}>{engineError}</span>
          </div>
        )}

        <RunScope runs={runs} active={active} basePath="/forward" />

        <ForwardControl initial={state} runKey={active} />

        {ladder && ladder.fired.length > 0 && (
          <Card
            title="Ladder state"
            hint={`Levels opened by the ${active} run. Each fires at most once.`}
          >
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

        <Card title="How these numbers are produced" hint="Applied automatically to every run.">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li><strong>Paper only, structurally.</strong> There is no order-placement code in the engine at all, so no run can place one.</li>
            <li><strong>Fills cross the spread.</strong> Buys lift the offer and sells hit the bid. Where Choice returns no depth, a spread is modelled rather than assumed to be zero, and the run reports what fraction was priced on a real book.</li>
            <li><strong>All-or-nothing entries.</strong> If any of the four legs has no quote, or its book is too wide to trade through, the condor is skipped rather than half-opened.</li>
            <li><strong>Mid-marked while open.</strong> Unrealised P&amp;L is marked to fair value; the cost of crossing is charged on entry and exit, not smeared across every tick.</li>
            <li><strong>Survives a restart.</strong> Ladder state, open condors and tick history are stored on the engine, so a restart resumes the run instead of losing it.</li>
            <li><strong>Knows the calendar.</strong> Weekends and holidays are skipped rather than spent logging "no quotes".</li>
          </ul>
          <p style={{ margin: "12px 0 0", fontSize: 12 }}>
            {/* Carries the run, so the log opens on the same test rather
                than on whichever one it would pick by itself. */}
            <Link
              href={`/forward/log?run=${encodeURIComponent(active)}`}
              style={{ color: "var(--brand)", fontWeight: 600 }}
            >
              Activity log &amp; trade history for {active} &rarr;
            </Link>
          </p>
        </Card>
      </div>
    </>
  );
}
