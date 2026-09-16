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
        subtitle="The two strategies this platform runs: the condor ladder, which earns while the market stalls, and the hybrid iron condor, which earns while a move keeps going. Both trade the same 100-point trigger on the same live ticks, so a month tells you which kind of month it was."
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

        <Card
          title="Hybrid iron condor (HIC)"
          hint="The second strategy. Same trigger, same anchor — but what it opens depends on how far the level has travelled."
        >
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            The ladder sells a condor at every rung, so it earns while NIFTY stays inside the
            strikes and bleeds when a move keeps running. HIC is built for the opposite month. It
            sells condors only <em>near</em> the anchor, and past that it stops selling and starts{" "}
            <strong>buying</strong> — a two-leg vertical spread per step, paid for rather than
            sold, which is worth more the further the move goes.
          </p>

          <div className="scroll-x" style={{ marginTop: 14 }}>
            <table style={{ minWidth: 620 }}>
              <thead>
                <tr>
                  <th>Steps from the anchor</th>
                  <th>What opens</th>
                  <th>Legs at level <span className="mono">L</span></th>
                  <th style={{ textAlign: "right" }}>Cash</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="mono" style={{ fontSize: 12 }}>|k| &le; band</td>
                  <td><Badge tone="brand">Iron condor</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    the same four legs the ladder opens
                  </td>
                  <td style={{ textAlign: "right", color: "var(--pos)", fontWeight: 600 }}>credit</td>
                </tr>
                <tr>
                  <td className="mono" style={{ fontSize: 12 }}>k &lt; &minus;band (below)</td>
                  <td><Badge tone="pos">Put spread</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    BUY L&nbsp;&minus;&nbsp;{num(params.short_offset)} PE ·
                    SELL L&nbsp;&minus;&nbsp;{num(params.long_offset)} PE
                  </td>
                  <td style={{ textAlign: "right", color: "var(--neg)", fontWeight: 600 }}>debit</td>
                </tr>
                <tr>
                  <td className="mono" style={{ fontSize: 12 }}>k &gt; +band (above)</td>
                  <td><Badge tone="pos">Call spread</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    BUY L&nbsp;+&nbsp;{num(params.short_offset)} CE ·
                    SELL L&nbsp;+&nbsp;{num(params.long_offset)} CE
                  </td>
                  <td style={{ textAlign: "right", color: "var(--neg)", fontWeight: 600 }}>debit</td>
                </tr>
              </tbody>
            </table>
          </div>

          <p style={{ fontSize: 12.5, color: "var(--ink-muted)", lineHeight: 1.75, margin: "14px 0 0" }}>
            The spread is the condor&rsquo;s own put side at that level with the two sides swapped:
            bought near, sold far. Setting <strong>Spread strikes</strong> to &ldquo;bought at the
            level&rdquo; shifts the whole pair {num(params.short_offset)} points toward the money —
            it costs more and starts paying sooner. Bought legs are written first, the same order
            the ladder uses, so one fill log reads the same whichever strategy wrote it.
          </p>

          <h4 style={{ fontSize: 12.5, fontWeight: 700, color: "var(--ink)", margin: "18px 0 8px" }}>
            The two settings that shape it
          </h4>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li>
              <strong>Core band</strong> — how many steps either side of the anchor still sell a
              full condor. A band of 1 sells three condors (the anchor and one step each way) and
              buys a spread at every step beyond. A band of 0 sells only at the anchor.
            </li>
            <li>
              <strong>Put spreads / Call spreads</strong> — how many bought spreads each side may
              open. These set how far the ladder runs: the band plus the spread count{" "}
              <em>is</em> the per-side cap, so HIC has no separate Max down and Max up. It is
              symmetric by construction and the engine keeps it that way.
            </li>
          </ul>

          <p style={{ fontSize: 12.5, color: "var(--ink-muted)", lineHeight: 1.75, margin: "14px 0 0" }}>
            HIC is always two-way and always centred on the nearest step. A one-directional HIC
            would be a different strategy wearing the name, and an anchor rounded down would put
            the spot up to {num(params.step - 1)} points off centre in a structure whose whole point
            is symmetry.
          </p>
        </Card>

        <Card title="Where the risk is">
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li>
              <strong>A sold condor&rsquo;s loss is bounded</strong> at{" "}
              <span className="mono">
                {num(params.long_offset - params.short_offset)} &times; {num(params.qty)} &minus; credit
              </span>
              . Only one side can finish in the money, so you are never exposed to both wings at once.
            </li>
            <li>
              <strong>A bought spread risks only what it cost.</strong> The debit paid is the whole
              of the downside — there is no wing to subtract a credit from — and unlike a condor,{" "}
              <em>both</em> its legs can finish in the money, which is the case it is bought for.
              It has one breakeven rather than two. A stop-loss set as a multiple of what was paid
              can therefore never trigger on one: the position has no room to lose more than the
              debit. That is honest rather than broken, but it means a 2&times; stop, sensible on a
              condor, does nothing here.
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
              <strong>Caps:</strong> positions are bounded by <code>max_condors</code> (e.g. 20),
              and the ladder takes optional per-side caps (<code>max_down</code>,{" "}
              <code>max_up</code>) on top. HIC derives its own from the band and the spread counts.
              Where the two disagree the tighter one wins and the run says so in its log, rather
              than dropping the furthest rungs silently.
            </li>
            <li>
              <strong>Both strategies hold to expiry</strong> by default, and settle at intrinsic
              against the closing index level on expiry day. A campaign lives inside one expiry:
              when it settles, the ladder re-anchors, because a long put in September offsets
              nothing in October.
            </li>
          </ul>
        </Card>

        <Card
          title="Current parameters"
          hint="From the most recent backtest on this dashboard. A live forward run carries its own settings, shown on the Live Monitor."
        >
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
