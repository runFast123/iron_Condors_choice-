export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { inr, num } from "@/lib/format";
import { Card, PageHeader, ProvenanceBanner, Stat, StatGrid } from "@/components/ui";
import { PayoffChart } from "@/components/charts/PayoffChart";
import { UnitKindBadge } from "@/components/UnitKindBadge";

/**
 * Breakevens as text, however many there are.
 *
 * A condor has two, a vertical spread has one, and a spread whose payoff never
 * crosses zero has none. Two fixed columns rendered NaN for the second of
 * those and had nothing to say about the third.
 */
function formatBreakevens(points: number[] | undefined): string {
  if (!points || points.length === 0) return "—";
  return points.map((p) => num(p, 0)).join(" / ");
}

export default async function PayoffPage() {
  const { payoff, equity, condors, params, provenance, metrics, payoff_campaign } =
    await getDataset();
  const lastSpot = [...equity].reverse().find((p) => p.spot != null)?.spot ?? null;

  const best = payoff.reduce((a, b) => (b.pnl > a.pnl ? b : a), payoff[0]);
  const worst = payoff.reduce((a, b) => (b.pnl < a.pnl ? b : a), payoff[0]);

  // The tiles describe the book the chart draws, which is one campaign. Summed
  // over every condor in the run they described a position nobody ever held:
  // a six-month backtest re-anchors at each expiry, so its rungs belong to
  // twenty-odd separate books, and adding their risk together made a longer
  // run look more dangerous for no reason but its length.
  const book = payoff_campaign;
  const drawn = book?.expiry ? condors.filter((c) => c.expiry === book.expiry) : condors;
  const maxLoss = book ? book.max_loss : drawn.reduce((s, c) => s + c.max_loss, 0);
  // Sold and bought kept apart. Netting a debit spread's cost against a
  // condor's credit and labelling the difference "Credit collected" understates
  // the credit and hides the outlay entirely.
  const credit = book ? book.credit : drawn.reduce((s, c) => s + Math.max(c.credit, 0), 0);
  const debit = book ? book.debit : drawn.reduce((s, c) => s - Math.min(c.credit, 0), 0);
  const rolling = (book?.campaigns ?? 1) > 1;
  const forExpiry = book?.expiry
    ? `for ${new Date(book.expiry).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}`
    : "";

  return (
    <>
      <PageHeader
        title="Payoff at Expiry"
        subtitle={
          rolling
            ? `Profit and loss across one expiry's book, if all of it settled at the same NIFTY level. This run rolled through ${book?.campaigns} expiries; the chart shows the one that carried the most risk, since positions from different expiries were never held together.`
            : "Combined profit and loss across every position, if all of it settled at the same NIFTY level. Breakevens are marked where the curve crosses zero."
        }
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={metrics.real_price_fraction}
          note={provenance.note}
          awaiting={(provenance.awaiting_connection ?? false)}
          hasData={condors.length > 0}
          legs={{
            total: provenance.legs_total,
            real: provenance.legs_real,
            empty: provenance.legs_empty,
            unused: provenance.legs_unused,
            unresolved: provenance.legs_unresolved,
            emptyExpiries: provenance.empty_expiries,
          }}
        />

        <StatGrid>
          <Stat label="Peak payoff" value={inr(best?.pnl ?? 0, { sign: true })} tone="pos"
                hint={`at NIFTY ${num(best?.spot ?? 0)}`} />
          <Stat label="Worst payoff" value={inr(worst?.pnl ?? 0, { sign: true })} tone="neg"
                hint={`at NIFTY ${num(worst?.spot ?? 0)}`} />
          {debit > 0 ? (
            <Stat label="Net of credit and debit" value={inr(credit - debit)}
                  hint={`${inr(credit)} sold, ${inr(debit)} paid ${forExpiry}`} />
          ) : (
            <Stat label="Credit collected" value={inr(credit)}
                  hint={`across ${drawn.length} position${drawn.length === 1 ? "" : "s"} ${forExpiry}`} />
          )}
          <Stat label="Sum of max-loss" value={inr(maxLoss)}
                hint={rolling ? "this expiry's book, before offsetting" : "before any offsetting"} />
        </StatGrid>

        <Card
          title={rolling ? `Expiry payoff ${forExpiry}` : "Combined expiry payoff"}
          hint={
            "Gains shaded above the zero line, losses below. The gold marker is the last observed NIFTY level." +
            (rolling
              ? ` Of the ${book?.campaigns} expiries this run traded, this is the deepest single book — the others are charted no more than this one because they were closed before it opened.`
              : "")
          }
        >
          <PayoffChart data={payoff} spot={lastSpot} />
        </Card>

        <Card title="Per-condor structure" pad={0}
              hint={
                `A condor risks at most one ${num(params.long_offset - params.short_offset)}-point wing, since only one of its sides can finish in the money. A bought spread risks only what it cost, and both of its legs can finish in the money.` +
                (rolling ? " Every position the run opened, across all expiries — not only the book charted above." : "")
              }>
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Unit</th>
                  <th>Level</th>
                  <th style={{ textAlign: "right" }}>Credit</th>
                  <th style={{ textAlign: "right" }}>Max profit</th>
                  <th style={{ textAlign: "right" }}>Max loss</th>
                  <th style={{ textAlign: "right" }}>Breakeven(s)</th>
                </tr>
              </thead>
              <tbody>
                {condors.map((c) => (
                  <tr key={c.index}>
                    <td><UnitKindBadge kind={c.kind} k={c.k} /></td>
                    <td className="tnum" style={{ fontWeight: 600 }}>{num(c.level)}</td>
                    <td className="tnum" style={{ textAlign: "right" }}>{inr(c.credit)}</td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--pos)" }}>
                      {inr(c.max_profit)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--neg)" }}>
                      {inr(-c.max_loss)}
                    </td>
                    <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {formatBreakevens(c.breakevens)}
                    </td>
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
