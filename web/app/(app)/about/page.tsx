export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { num, pct } from "@/lib/format";
import { Badge, Card, PageHeader } from "@/components/ui";

export default async function AboutPage() {
  const { params, netting } = await getDataset();
  const L = 24000;

  return (
    <>
      <PageHeader
        title="The Strategy"
        subtitle="What the ladder does, why the legs cancel, and exactly where the risk sits."
      />

      <div style={{ display: "grid", gap: 16, maxWidth: 1000 }}>
        <Card title="The rule">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            Open an iron condor at the anchor level. Every further{" "}
            <strong>{num(params.step)}-point decline</strong> in NIFTY opens another one around the
            new level. Each rung is built the same way:
          </p>
          <div className="scroll-x" style={{ marginTop: 14 }}>
            <table style={{ minWidth: 520 }}>
              <thead>
                <tr>
                  <th>Leg</th>
                  <th>Rule</th>
                  <th>At level {num(L)}</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><Badge tone="brand">BUY PE</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>level &minus; {num(params.long_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L - params.long_offset)} PE</td>
                </tr>
                <tr>
                  <td><Badge tone="warn">SELL PE</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>level &minus; {num(params.short_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L - params.short_offset)} PE</td>
                </tr>
                <tr>
                  <td><Badge tone="warn">SELL CE</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>level + {num(params.short_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L + params.short_offset)} CE</td>
                </tr>
                <tr>
                  <td><Badge tone="brand">BUY CE</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>level + {num(params.long_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L + params.long_offset)} CE</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-muted)", lineHeight: 1.75, margin: "14px 0 0" }}>
            The protective wings are bought <em>before</em> the shorts are sold, so the account never
            momentarily shows a naked short position &mdash; which would spike the margin requirement
            and can get the remaining legs rejected mid-structure.
          </p>
        </Card>

        <Card title="Why the legs cancel"
              hint="This is the mechanism the whole approach rests on.">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            The ladder steps {num(params.step)} points at a time, but the wings sit{" "}
            {num(params.short_offset)} and {num(params.long_offset)} points out. That gap is{" "}
            {num(params.long_offset - params.short_offset)} points &mdash;{" "}
            <strong>exactly {(params.long_offset - params.short_offset) / params.step} steps</strong>.
            So the long put of one rung lands on the same strike as the short put of the rung two
            steps below:
          </p>
          <div
            className="card"
            style={{ margin: "14px 0 0", padding: 14, background: "var(--surface-3)", fontSize: 13 }}
          >
            <div className="mono" style={{ lineHeight: 2 }}>
              Rung {num(L)}&nbsp;&nbsp;&rarr;&nbsp; <span style={{ color: "var(--c1)" }}>BUY&nbsp; {num(L - params.long_offset)} PE</span>
              <br />
              Rung {num(L - 2 * params.step)}&nbsp;&nbsp;&rarr;&nbsp; <span style={{ color: "var(--c2)" }}>SELL {num(L - 2 * params.step - params.short_offset)} PE</span>
              <br />
              <span style={{ color: "var(--ink-muted)" }}>
                ──────────────────────────────
              </span>
              <br />
              <strong>NET&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; 0</strong>{" "}
              <span style={{ color: "var(--ink-muted)" }}>&larr; same strike, opposite sides</span>
            </div>
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.75, margin: "14px 0 0" }}>
            Across the backtested campaign this cancelled <strong>{pct(netting.offset_ratio)}</strong>{" "}
            of all quantity traded &mdash; {num(netting.strikes_fully_offset)} of{" "}
            {num(netting.strikes_touched)} strikes netted to exactly zero. The book the broker
            actually carries is far smaller than the raw leg count implies, which is what keeps
            margin from compounding as the ladder extends.
          </p>
        </Card>

        <Card title="Where the risk is">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li>
              <strong>Per rung, loss is bounded</strong> at{" "}
              <span className="mono">
                {num(params.long_offset - params.short_offset)} &times; {num(params.qty)} &minus; credit
              </span>
              . Only one side can finish in the money, so you are never exposed to both wings at once.
            </li>
            <li>
              <strong>The ladder is down-only.</strong> Rallies open nothing, and each level fires at
              most once. A sustained grind lower therefore keeps adding rungs while the earlier ones
              are still losing &mdash; the concentration risk is a trend, not a spike.
            </li>
            <li>
              <strong>A gap-down fires every level it skipped</strong> (toggleable), so an overnight
              drop builds the same ladder a gradual decline would.
            </li>
            <li>
              <strong>Rungs are capped at {num(params.max_condors)}</strong>. Without a cap a long
              decline would open positions indefinitely.
            </li>
          </ul>
        </Card>

        <Card title="Current parameters">
          <div className="scroll-x">
            <table>
              <tbody>
                {[
                  ["Step between rungs", `${num(params.step)} pts`],
                  ["Short strike offset", `${num(params.short_offset)} pts`],
                  ["Long strike offset", `${num(params.long_offset)} pts`],
                  ["Wing width", `${num(params.long_offset - params.short_offset)} pts`],
                  ["Lots per rung", `${params.lots} (${num(params.qty)} shares)`],
                  ["Max rungs", num(params.max_condors)],
                  ["Fill skipped levels on a gap", params.fill_gaps ? "Yes" : "No"],
                  ["Anchor mode", params.anchor_mode],
                  ["Take profit", params.take_profit_pct == null ? "Off - held to expiry" : pct(params.take_profit_pct) + " of credit"],
                  ["Stop loss", params.stop_loss_mult == null ? "Off - held to expiry" : `${params.stop_loss_mult}x credit`],
                ].map(([k, v]) => (
                  <tr key={k}>
                    <td style={{ color: "var(--ink-muted)", width: 260 }}>{k}</td>
                    <td style={{ fontWeight: 600 }}>{v}</td>
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
