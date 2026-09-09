export const dynamic = "force-dynamic";

import { getDataset } from "@/lib/data";
import { Card, PageHeader, ProvenanceBanner } from "@/components/ui";
import { CondorBlotter } from "@/components/CondorBlotter";

export default async function TradesPage() {
  const { condors, provenance, metrics } = await getDataset();
  const legCount = condors.reduce((n, c) => n + c.legs.length, 0);

  return (
    <>
      <PageHeader
        title="Trades"
        subtitle={`${condors.length} condors, ${legCount} legs. One row per condor with its net P&L; open a row to see the four legs behind it.`}
      />

      <div style={{ display: "grid", gap: 16 }}>
        <ProvenanceBanner
          verified={provenance.verified}
          realFraction={metrics.real_price_fraction}
          note={provenance.note}
          awaiting={(provenance.awaiting_connection ?? false)}
          hasData={condors.length > 0}
        />

        <Card
          title="Condors"
          pad={0}
          hint="Net P&L is after entry and exit costs. Click a row for its four legs and the reconciliation."
        >
          <CondorBlotter condors={condors} />
        </Card>
      </div>
    </>
  );
}
