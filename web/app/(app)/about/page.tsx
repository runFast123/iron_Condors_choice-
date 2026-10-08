export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { inr, num, pct } from "@/lib/format";
import { Badge, Card, PageHeader } from "@/components/ui";

export default async function AboutPage() {
  const { params, netting } = await getDataset();
  const L = 24000;

  const tslVal =
    params.campaign_trailing_sl_pct ??
    params.campaign_trailing_sl ??
    params.trailing_sl_mult;
  const tslHurdle =
    params.campaign_trailing_sl_trigger ??
    params.trailing_sl_trigger;
  const slVal =
    params.campaign_stop_loss ??
    params.stop_loss_mult;

  return (
    <>
      <PageHeader
        title="The Strategy"
        subtitle="Comprehensive reference for the quantitative trading models implemented on this platform: the Condor Ladder (v1 & v2), the Hybrid Iron Condor (HIC), two-way strike netting, and multi-tier capital risk guardrails."
      />

      <div style={{ display: "grid", gap: 16, maxWidth: 1000 }}>
        {/* SECTION 1: Core Condor Ladder Rules */}
        <Card title="1. The Condor Ladder: Core Rules & Geometry">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            An initial 4-leg iron condor is deployed at the starting anchor level. Every subsequent{" "}
            <strong>{num(params.step)}-point move</strong> in NIFTY opens another condor around the
            new level. Each condor is structured with identical geometry around its reference level{" "}
            <span className="mono">L</span>:
          </p>
          <div className="scroll-x" style={{ marginTop: 14 }}>
            <table style={{ minWidth: 540 }}>
              <thead>
                <tr>
                  <th>Leg</th>
                  <th>Action</th>
                  <th>Strike Rule</th>
                  <th>At Reference Level {num(L)}</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><Badge tone="brand">BUY PE</Badge></td>
                  <td>Long Protection Put</td>
                  <td className="mono" style={{ fontSize: 12 }}>level &minus; {num(params.long_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L - params.long_offset)} PE</td>
                </tr>
                <tr>
                  <td><Badge tone="warn">SELL PE</Badge></td>
                  <td>Short Credit Put</td>
                  <td className="mono" style={{ fontSize: 12 }}>level &minus; {num(params.short_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L - params.short_offset)} PE</td>
                </tr>
                <tr>
                  <td><Badge tone="warn">SELL CE</Badge></td>
                  <td>Short Credit Call</td>
                  <td className="mono" style={{ fontSize: 12 }}>level + {num(params.short_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L + params.short_offset)} CE</td>
                </tr>
                <tr>
                  <td><Badge tone="brand">BUY CE</Badge></td>
                  <td>Long Protection Call</td>
                  <td className="mono" style={{ fontSize: 12 }}>level + {num(params.long_offset)}</td>
                  <td className="tnum" style={{ fontWeight: 600 }}>{num(L + params.long_offset)} CE</td>
                </tr>
              </tbody>
            </table>
          </div>
          <ul style={{ margin: "14px 0 0", paddingLeft: 18, fontSize: 12.5, lineHeight: 1.85, color: "var(--ink-muted)" }}>
            <li>
              <strong>Execution Sequencing:</strong> The outer protective wings are bought <em>before</em> the short legs are sold, ensuring the account never experiences momentary naked short exposure (which would trigger margin spikes or broker order rejections).
            </li>
            <li>
              <strong>Fire-on-Reach:</strong> A rung triggers the moment price trades at or beyond that exact level.
            </li>
            <li>
              <strong>Single Fire per Cycle:</strong> Each distinct level fires at most once per expiry campaign, preventing churning around volatile price levels.
            </li>
            <li>
              <strong>Gap-Fill Logic:</strong> Overnight gap-downs or gap-ups trigger all intermediate skipped levels in order, ensuring an overnight gap builds the exact same hedged structure as a gradual intraday move.
            </li>
            <li>
              <strong>Dynamic VIX Grid Step (Optional):</strong> Instead of a static 100-point grid, the engine can dynamically size the step for each month at the campaign anchor using India VIX. The expected 1-month NIFTY move is calculated as:
              <div className="mono" style={{ margin: "6px 0", padding: "6px 12px", background: "var(--surface)", borderRadius: 4, fontSize: 11.5 }}>
                Expected Move = Spot &times; (VIX / &radic;12 / 100) &nbsp;&nbsp;|&nbsp;&nbsp; Step = max(100, round((Expected Move / N) / 50) &times; 50)
              </div>
              Where <em>N</em> is the target condors per side (default 5). For example, at spot 24,335 and VIX 11.10: 11.10 / &radic;12 = 3.21%, 24,335 &times; 3.21% = 781 points. Divided by 5 condors = 156 pts &rarr; rounded to <strong>150 points</strong>! Only the moving grid step dynamically adjusts (e.g. 150 points), while the short strike offset (&plusmn;200 points) and long protection offset (&plusmn;400 points) remain fixed as configured, keeping the wing width at a consistent 200 points across all rungs while spacing out entries during higher volatility regimes.
            </li>
          </ul>
        </Card>

        {/* SECTION 2: Ladder v2 Two-Way Mechanics */}
        <Card title="2. Ladder v2: Direction & Two-Way Expansion" hint="Configurable direction with independent per-side caps and anchor modes.">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            Ladder v2 expands the system from a down-only ladder into an asymmetric two-way engine capable of handling market rallies and sell-offs:
          </p>
          <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 13, lineHeight: 1.9, color: "var(--ink-2)" }}>
            <li>
              <strong>Direction Mode (<code>down</code> | <code>both</code> | <code>up</code>):</strong>{" "}
              In <code>down</code> mode (v1 original), only market declines below the anchor open condors. In{" "}
              <code>both</code> mode (Two-way), market rallies above the anchor open matching condors on every {num(params.step)}-point rise.
            </li>
            <li>
              <strong>Anchor Modes:</strong>{" "}
              <code>floor</code> rounds down to the nearest grid step (optimal for down-only), whereas{" "}
              <code>nearest</code> snaps to the closest round multiple (recommended for two-way symmetry).
            </li>
            <li>
              <strong>Asymmetric Per-Side Caps (<code>max_down</code> &amp; <code>max_up</code>):</strong>{" "}
              In Indian markets (NIFTY), market rallies typically compress volatility (lower IV, thinner call credit), while subsequent market crashes are violent. Setting an asymmetric rally cap (e.g. <code>max_up = 2</code> or <code>10</code>) alongside <code>max_down = 20</code> prevents accumulating excessive top-of-rally positions that turn into liabilities during market reversals.
            </li>
            <li>
              <strong>Attribution Tracking:</strong> P&amp;L, collected credits, and position counts are segregated into Down-side, Up-side, and Anchor components across all analytics pages.
            </li>
          </ul>
        </Card>

        {/* SECTION 3: Two-Way Strike Netting & Self-Hedging */}
        <Card title="3. Strike Netting & The 8-Leg Invariant" hint="The core mathematical mechanism that cuts portfolio margin and risk.">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            The ladder steps {num(params.step)} points at a time, but the wings sit{" "}
            {num(params.short_offset)} and {num(params.long_offset)} points out. That gap is{" "}
            {num(params.long_offset - params.short_offset)} points &mdash;{" "}
            <strong>exactly {(params.long_offset - params.short_offset) / params.step} steps</strong>.
            Consequently, the long put of one condor lands on the exact same strike as the short put of the condor two steps below, and the long call of a lower condor cancels the short call of a condor two steps above:
          </p>
          <div
            className="card"
            style={{ margin: "14px 0 0", padding: 14, background: "var(--surface-3)", fontSize: 13 }}
          >
            <div className="mono" style={{ lineHeight: 2 }}>
              Condor {num(L)}&nbsp;&nbsp;&rarr;&nbsp; <span style={{ color: "var(--brand)" }}>BUY&nbsp; {num(L - params.long_offset)} PE</span> &nbsp;&middot;&nbsp; <span style={{ color: "var(--warn)" }}>SELL {num(L + params.short_offset)} CE</span>
              <br />
              Condor {num(L - 2 * params.step)}&nbsp;&nbsp;&rarr;&nbsp; <span style={{ color: "var(--warn)" }}>SELL {num(L - 2 * params.step - params.short_offset)} PE</span> &nbsp;&middot;&nbsp; <span style={{ color: "var(--brand)" }}>BUY&nbsp; {num(L - 2 * params.step + params.long_offset)} CE</span>
              <br />
              <span style={{ color: "var(--ink-muted)" }}>
                ─────────────────────────────────────────────────────────────────
              </span>
              <br />
              <strong>NET RESULT:&nbsp;&nbsp; 0 PE &nbsp;&middot;&nbsp; 0 CE</strong>{" "}
              <span style={{ color: "var(--ink-muted)" }}>&larr; identical strikes, opposite sides cancel to zero!</span>
            </div>
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.8, margin: "14px 0 0" }}>
            Across campaigns, this cancels <strong>{pct(netting.offset_ratio)}</strong> of all gross quantity traded &mdash;{" "}
            {num(netting.strikes_fully_offset)} of {num(netting.strikes_touched)} touched strikes net to exactly zero.
          </p>
          <div style={{ marginTop: 10, padding: "10px 14px", background: "var(--surface-2)", borderRadius: 6, fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
            <strong>The 8-Leg Invariant:</strong> On a continuous ladder whose highest level is <em>T</em> and lowest level is <em>B</em>, all internal legs completely cancel out. The broker always carries at most <strong>8 active legs</strong> in total regardless of whether 5, 10, or 20 condors have been triggered:
            <div className="mono" style={{ marginTop: 6, fontSize: 11.5 }}>
              • SELL PE at T &minus; 200, T &minus; 300 &nbsp;&nbsp;|&nbsp;&nbsp; BUY PE at B &minus; 300, B &minus; 400<br />
              • SELL CE at B + 200, B + 300 &nbsp;&nbsp;|&nbsp;&nbsp; BUY CE at T + 300, T + 400
            </div>
          </div>
        </Card>

        {/* SECTION 4: Hybrid Iron Condor (HIC) */}
        <Card
          title="4. Hybrid Iron Condor (HIC): Asymmetric Trend Monetization"
          hint="The mirror-image strategy: sells condors in the core band and buys directional spreads on breakouts."
        >
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            While the ladder sells condors to monetize sideways consolidation, HIC is built for trending regimes. It sells credit condors inside a narrow core band, and outside that band, it transitions into <strong>buying directional debit spreads</strong> to capture sustained directional moves:
          </p>

          <div className="scroll-x" style={{ marginTop: 14 }}>
            <table style={{ minWidth: 620 }}>
              <thead>
                <tr>
                  <th>Zone / Steps from Anchor</th>
                  <th>Structure Opened</th>
                  <th>Legs at Reference Level <span className="mono">L</span></th>
                  <th style={{ textAlign: "right" }}>Cashflow</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="mono" style={{ fontSize: 12 }}>|k| &le; Core Band</td>
                  <td><Badge tone="brand">Full Iron Condor</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    4-leg condor (+400/&minus;200 PE &amp; &minus;200/+400 CE)
                  </td>
                  <td style={{ textAlign: "right", color: "var(--pos)", fontWeight: 600 }}>Credit Received</td>
                </tr>
                <tr>
                  <td className="mono" style={{ fontSize: 12 }}>k &lt; &minus;Band (Downside)</td>
                  <td><Badge tone="pos">Put Debit Spread</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    BUY L&nbsp;&minus;&nbsp;{num(params.short_offset)} PE ·
                    SELL L&nbsp;&minus;&nbsp;{num(params.long_offset)} PE
                  </td>
                  <td style={{ textAlign: "right", color: "var(--neg)", fontWeight: 600 }}>Debit Paid</td>
                </tr>
                <tr>
                  <td className="mono" style={{ fontSize: 12 }}>k &gt; +Band (Upside)</td>
                  <td><Badge tone="pos">Call Debit Spread</Badge></td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    BUY L&nbsp;+&nbsp;{num(params.short_offset)} CE ·
                    SELL L&nbsp;+&nbsp;{num(params.long_offset)} CE
                  </td>
                  <td style={{ textAlign: "right", color: "var(--neg)", fontWeight: 600 }}>Debit Paid</td>
                </tr>
              </tbody>
            </table>
          </div>

          <p style={{ fontSize: 12.5, color: "var(--ink-muted)", lineHeight: 1.75, margin: "14px 0 0" }}>
            <strong>Spread Strike Shift (<code>debit_shift</code>):</strong> Setting shift to 0 mirrors the condor&rsquo;s strikes. Setting shift to 200 buys the spread directly at the breakout level, paying slightly more upfront premium but participating in profit immediately.
          </p>
        </Card>

        {/* SECTION 5: Risk Management, Circuit Breakers & Square-Off Logic */}
        <Card title="5. Risk Management, Circuit Breakers &amp; Stop-Loss Rules" hint="Multi-tier safety architecture protecting capital across individual trades, months, and campaigns.">
          <div style={{ display: "grid", gap: 14 }}>
            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 6, borderLeft: "3px solid var(--neg)" }}>
              <strong style={{ fontSize: 13, color: "var(--ink)" }}>A. Monthly Campaign Stop Loss (Fixed Rupee Ceiling)</strong>
              <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
                Enforces an absolute monthly loss ceiling (e.g. ₹20,000 or ₹25,000) across the entire active monthly expiry cycle. When cumulative monthly loss (realised + unrealised MTM) breaches this threshold, all open condors are immediately squared off and no further condors open for that month.
              </p>
            </div>

            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 6, borderLeft: "3px solid var(--brand)" }}>
              <strong style={{ fontSize: 13, color: "var(--ink)" }}>B. Campaign Trailing SL (% of Peak Capital)</strong>
              <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
                Protects accumulated multi-month profits from severe late-cycle drawdowns (such as the September 2026 crash where peak capital reached ₹1.47 Lakhs). The engine tracks the high-water mark of capital at monthly campaign settlements. If capital pulls back from peak by the selected percentage (e.g. <strong>15%</strong>), all open positions are immediately squared off.
              </p>
            </div>

            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 6, borderLeft: "3px solid var(--pos)" }}>
              <strong style={{ fontSize: 13, color: "var(--ink)" }}>C. TSL Activation Hurdle (Min Profit to Arm)</strong>
              <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
                Prevents premature exits from small intraday wiggles during early trading (e.g. on early ₹5,000 profit, a 15% stop is only ₹750, which would be tripped by normal bid-ask noise). The Trailing Stop remains dormant until peak capital reaches the activation hurdle (e.g. <strong>₹75,000</strong> or <strong>₹1,00,000</strong>). Once armed, it locks in peak gains.
              </p>
            </div>

            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 6, borderLeft: "3px solid var(--warn)" }}>
              <strong style={{ fontSize: 13, color: "var(--ink)" }}>D. Pre-Entry Drawdown Guard</strong>
              <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
                Before opening any new condors on an incoming price bar, the engine evaluates existing open positions against stop thresholds. If open positions are already in drawdown at the current spot (e.g. during a 350-point overnight gap-down), the stop triggers immediately and squares off open positions <strong>without opening any new condors into the plunge</strong>.
              </p>
            </div>

            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 6, borderLeft: "3px solid #38bdf8" }}>
              <strong style={{ fontSize: 13, color: "var(--ink)" }}>E. Monthly Expiry Continuation (Trading with Remaining Capital)</strong>
              <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
                When a monthly campaign stops out, the halt applies only to that monthly campaign. On expiry day roll to the subsequent month, the ladder re-anchors at the prevailing spot and resumes trading fresh with the remaining capital, resetting the trailing baseline.
              </p>
            </div>

            <div style={{ padding: "12px 14px", background: "var(--surface-2)", borderRadius: 6, borderLeft: "3px solid var(--ink-muted)" }}>
              <strong style={{ fontSize: 13, color: "var(--ink)" }}>F. India VIX Volatility Gate &amp; Daily Circuit Breaker</strong>
              <p style={{ margin: "5px 0 0", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.7 }}>
                • <strong>VIX Gate:</strong> No new positions open while India VIX is above the configured limit (e.g. 15.0). Entries resume once VIX cools down.<br />
                • <strong>Daily Loss Limit:</strong> Intraday circuit breaker in rupees (e.g. ₹25,000/day) halting entries for the remainder of the session during flash crashes.
              </p>
            </div>
          </div>
        </Card>

        {/* SECTION 6: Square-off Pricing & Exact Math Reconciliation */}
        <Card title="6. Square-Off Execution &amp; Mathematical Accounting Audit">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            When a condor exits (at settlement or via stop loss), every leg is executed through a realistic fill model crossing the bid-ask spread. Every trade is audited to the paisa through the strict accounting identity:
          </p>
          <div
            className="card mono"
            style={{ margin: "12px 0", padding: "12px 16px", background: "var(--surface-3)", fontSize: 13, color: "var(--ink)" }}
          >
            Net P&amp;L = Entry Credit &minus; Square-Off Debit &minus; (Entry Costs + Exit Costs)
          </div>
          <p style={{ fontSize: 12.5, color: "var(--ink-muted)", lineHeight: 1.75, margin: 0 }}>
            Both the Overview table and Trades blotter display the exact <strong>Square-Off Date</strong> and total <strong>Square-Off Price</strong> (exit debit) for complete transparency. Expanding any row details individual per-share entry and exit fill prices.
          </p>
        </Card>

        {/* SECTION 7: Pricing Hierarchy & Data Provenance */}
        <Card title="7. Pricing Hierarchy &amp; Market Data Provenance">
          <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.8, color: "var(--ink-2)" }}>
            Choice FinX broker API is the authoritative source for every price. Where historical option contracts have expired and are no longer served by the broker, the engine resolves prices through a transparent 5-tier fallback hierarchy:
          </p>
          <div className="scroll-x" style={{ marginTop: 12 }}>
            <table style={{ minWidth: 540 }}>
              <thead>
                <tr>
                  <th>Badge</th>
                  <th>Source</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><Badge tone="pos">CHOICE</Badge></td>
                  <td>Choice FinX API</td>
                  <td>Real 1-minute historical candles from Choice ChartData or live touchline quotes.</td>
                </tr>
                <tr>
                  <td><Badge tone="brand">HISTORY</Badge></td>
                  <td>Local DuckDB Store</td>
                  <td>Real exchange-exact 1-minute bars of all NIFTY option contracts since 2018 in local <span className="mono">nifty.db</span>.</td>
                </tr>
                <tr>
                  <td><Badge tone="brand">BACKUP</Badge></td>
                  <td>Secondary Broker API</td>
                  <td>Real historical candles for expired contracts when Choice history is incomplete.</td>
                </tr>
                <tr>
                  <td><Badge tone="brand">EXCHANGE</Badge></td>
                  <td>The Exchange's Daily Record</td>
                  <td>Real closing trade prices from the official exchange record.</td>
                </tr>
                <tr>
                  <td><Badge tone="warn">MODELED</Badge></td>
                  <td>Anchored Black-76 Smile</td>
                  <td>Black-76 implied volatility smile anchored to the exchange&rsquo;s previous session closing trades.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </Card>

        {/* SECTION 8: Current Parameters Table */}
        <Card
          title="8. Current Strategy Parameters"
          hint="Active settings loaded from the latest backtest run on this dashboard."
        >
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 280 }}>Parameter</th>
                  <th>Value</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Direction mode", (params.direction ?? "down").toUpperCase(), "Grid expansion direction (Down-only, Two-way, or Up-only)"],
                  ["Dynamic VIX grid step", params.dynamic_step ? "Enabled (VIX-based)" : "Disabled (Fixed step)", "Dynamically sizes step at campaign start from spot & VIX"],
                  ["Target condors / side", params.dynamic_step ? String(params.dynamic_step_condors ?? 5) : "—", "Target condor density across expected monthly move"],
                  ["Step interval", `${num(params.step)} pts`, "Index point distance between consecutive ladder rungs"],
                  ["Short strike offset", `${num(params.short_offset)} pts`, "Strike distance for sold put and call legs (Level ± Short Offset)"],
                  ["Long strike offset", `${num(params.long_offset)} pts`, "Strike distance for bought protective wings (Level ± Long Offset)"],
                  ["Wing width", `${num(params.long_offset - params.short_offset)} pts`, "Protected wing width between short and long strikes"],
                  ["Lots per condor", `${params.lots} (${num(params.qty)} shares)`, "Position sizing per triggered rung"],
                  ["Max condors (total cap)", num(params.max_condors), "Global ceiling on maximum concurrent positions"],
                  ["Max down / Max up caps", `${params.max_down ?? "—"} / ${params.max_up ?? "—"}`, "Directional position caps to prevent rally-side overexposure"],
                  ["Gap-fill on overnight moves", params.fill_gaps ? "Enabled" : "Disabled", "Fires skipped levels on overnight market gaps"],
                  ["Anchor mode", params.anchor_mode, "Grid reference anchoring ('floor' for down-only, 'nearest' for two-way)"],
                  ["Skip late entries (Min DTE)", params.min_entry_dte == null ? "Off" : `Under ${params.min_entry_dte} days to expiry`, "Rejects opening new condors near expiry to avoid gamma risk"],
                  ["Minimum credit ratio", params.min_credit_ratio == null ? "Off" : `${pct(params.min_credit_ratio)} of wing`, "Rejects opening condors when option premiums are depressed"],
                  ["Pause above India VIX", params.max_entry_vix == null ? "Off" : `≤ ${params.max_entry_vix}`, "Volatility circuit breaker pausing entries in panic regimes"],
                  ["Take profit target", params.take_profit_pct == null ? "Off (Hold to expiry)" : `${pct(params.take_profit_pct)} of credit`, "Locks in profit when condor captures target % of credit"],
                  ["Monthly Campaign Stop Loss", slVal == null ? "Off (Hold to expiry)" : `₹${Math.round(slVal).toLocaleString()}`, "Rupee loss ceiling for the active monthly cycle"],
                  ["Campaign Trailing SL (% of Peak Capital)", tslVal == null ? "Off" : `${tslVal <= 1 ? Math.round(tslVal * 100) : Math.round(tslVal)}% of Peak Capital`, "Trails peak campaign-end capital to lock in accumulated profits"],
                  ["TSL Activation Hurdle", tslHurdle == null || tslHurdle === 0 ? "Immediate (from ₹0)" : `Arms after ₹${Math.round(tslHurdle).toLocaleString()} profit`, "Minimum profit required before Trailing Stop activates"],
                  ["Roll to next expiry", params.roll_to_next_expiry !== false ? "Enabled" : "Single campaign", "Re-anchors fresh ladder upon expiry settlement"],
                ].map(([k, v, desc]) => (
                  <tr key={k}>
                    <td style={{ color: "var(--ink)", fontWeight: 600 }}>{k}</td>
                    <td style={{ fontWeight: 700, color: "var(--brand)" }}>{v}</td>
                    <td style={{ color: "var(--ink-muted)", fontSize: 12 }}>{desc}</td>
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
