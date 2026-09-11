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
        subtitle="What the ladder does, why the legs cancel, and how Ladder v2 operates in both directions."
      />

      <div style={{ display: "grid", gap: 16, maxWidth: 1000 }}>
        <Card title="The rule">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            Open an iron condor at the anchor level. Every further{" "}
            <strong>{num(params.step)}-point step</strong> in NIFTY opens another condor around the
            new level. Each condor is built the same way:
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

        <Card title="Ladder v2: Direction & Two-Way Expansion" hint="Configurable direction with independent per-side caps and anchor modes.">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            Ladder v2 introduces two-way grid mechanics so that rallies and declines can both be managed:
          </p>
          <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li>
              <strong>Direction Mode (<code>down</code> | <code>both</code> | <code>up</code>):</strong>{" "}
              In <code>down</code> mode (v1 default), only declines below the anchor fire condors. In{" "}
              <code>both</code> or <code>up</code> mode, every new {num(params.step)}-point high above the anchor
              opens a condor built exactly the same way.
            </li>
            <li>
              <strong>Anchor Modes:</strong> <code>floor</code> anchors to the floor level (ideal for down-only),
              while <code>nearest</code> snaps to the nearest {num(params.step)}-point round level (recommended for two-way symmetry).
            </li>
            <li>
              <strong>Per-Side Caps:</strong> In addition to the total limit (<code>max_condors</code>),
              you can set independent caps for <code>max_down</code> (e.g. 20) and <code>max_up</code> (e.g. 10).
              Rally-side credits are typically thinner due to lower IV on rallies, so a smaller up-side cap limits upside drift.
            </li>
            <li>
              <strong>Attribution:</strong> P&amp;L and credit collected are tracked and displayed separately for
              down-side and up-side condors.
            </li>
          </ul>
        </Card>

        <Card title="Why the legs cancel (Two-Way Netting)"
              hint="This is the mechanism the whole approach rests on.">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            The ladder steps {num(params.step)} points at a time, but the wings sit{" "}
            {num(params.short_offset)} and {num(params.long_offset)} points out. That gap is{" "}
            {num(params.long_offset - params.short_offset)} points &mdash;{" "}
            <strong>exactly {(params.long_offset - params.short_offset) / params.step} steps</strong>.
            So the long put of one condor lands on the same strike as the short put of the condor two
            steps below, and the long call of a lower condor cancels the short call of a condor two steps above:
          </p>
          <div
            className="card"
            style={{ margin: "14px 0 0", padding: 14, background: "var(--surface-3)", fontSize: 13 }}
          >
            <div className="mono" style={{ lineHeight: 2 }}>
              Condor {num(L)}&nbsp;&nbsp;&rarr;&nbsp; <span style={{ color: "var(--c1)" }}>BUY&nbsp; {num(L - params.long_offset)} PE</span> &nbsp;&middot;&nbsp; <span style={{ color: "var(--c2)" }}>SELL {num(L + params.short_offset)} CE</span>
              <br />
              Condor {num(L - 2 * params.step)}&nbsp;&nbsp;&rarr;&nbsp; <span style={{ color: "var(--c2)" }}>SELL {num(L - 2 * params.step - params.short_offset)} PE</span> &nbsp;&middot;&nbsp; <span style={{ color: "var(--c1)" }}>BUY&nbsp; {num(L - 2 * params.step + params.long_offset)} CE</span>
              <br />
              <span style={{ color: "var(--ink-muted)" }}>
                ────────────────────────────────────────────────────────
              </span>
              <br />
              <strong>NET&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; 0 PE &nbsp;&middot;&nbsp; 0 CE</strong>{" "}
              <span style={{ color: "var(--ink-muted)" }}>&larr; same strikes, opposite sides cancel on both wings</span>
            </div>
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.75, margin: "14px 0 0" }}>
            Across the backtested campaign this cancelled <strong>{pct(netting.offset_ratio)}</strong>{" "}
            of all quantity traded &mdash; {num(netting.strikes_fully_offset)} of{" "}
            {num(netting.strikes_touched)} strikes netted to exactly zero. On a continuous ladder whose highest level is <em>T</em> and lowest is <em>B</em>, the broker always carries at most <strong>8 legs</strong> in total regardless of how many rungs have fired.
          </p>
        </Card>

        <Card title="Where the risk is">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li>
              <strong>Per condor, loss is bounded</strong> at{" "}
              <span className="mono">
                {num(params.long_offset - params.short_offset)} &times; {num(params.qty)} &minus; credit
              </span>
              . Only one side can finish in the money, so you are never exposed to both wings at once.
            </li>
            <li>
              <strong>Firing rule:</strong> Each level fires at most once per campaign. A sustained one-way
              grind adds condors while earlier ones are losing &mdash; the concentration risk is a persistent trend.
            </li>
            <li>
              <strong>Gap fills:</strong> A gap-down or gap-up fires every level it skipped (toggleable),
              so an overnight move builds the same ladder a gradual move would.
            </li>
            <li>
              <strong>Caps:</strong> Condors are bounded by <code>max_condors</code> (e.g. 20) and optional per-side caps (<code>max_down</code>, <code>max_up</code>).
            </li>
          </ul>
        </Card>

        <Card title="Current parameters">
          <div className="scroll-x">
            <table>
              <tbody>
                {[
                  ["Direction mode", (params.direction ?? "down").toUpperCase()],
                  ["Step between condors", `${num(params.step)} pts`],
                  ["Short strike offset", `${num(params.short_offset)} pts`],
                  ["Long strike offset", `${num(params.long_offset)} pts`],
                  ["Wing width", `${num(params.long_offset - params.short_offset)} pts`],
                  ["Lots per condor", `${params.lots} (${num(params.qty)} shares)`],
                  ["Max condors (total)", num(params.max_condors)],
                  ["Max down / Max up", `${params.max_down ?? "—"} / ${params.max_up ?? "—"}`],
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
