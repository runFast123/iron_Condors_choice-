import type { UnitKind } from "@/lib/types";
import { Badge } from "@/components/ui";

/**
 * What shape a position is.
 *
 * Without it a condor and a vertical spread are both just rows of legs, and
 * they are opposite trades: one is sold for a credit and loses if the market
 * runs, the other is bought and only pays if it does. Telling them apart by
 * counting legs is not something a reader should have to do.
 */
const LABELS: Record<UnitKind, { label: string; tone: "brand" | "pos" | "warn" | "neutral" }> = {
  condor: { label: "Condor", tone: "brand" },
  put_debit_spread: { label: "Put spread", tone: "pos" },
  call_debit_spread: { label: "Call spread", tone: "pos" },
  put_credit_spread: { label: "Put credit", tone: "warn" },
  call_credit_spread: { label: "Call credit", tone: "warn" },
};

const PAID_FOR: UnitKind[] = ["put_debit_spread", "call_debit_spread"];

export function UnitKindBadge({ kind, k }: { kind?: UnitKind; k?: number | null }) {
  // Absent on anything opened before HIC existed, all of which was a condor.
  const resolved: UnitKind = kind ?? "condor";
  const { label, tone } = LABELS[resolved] ?? LABELS.condor;
  const step = k == null ? "" : ` at ${k > 0 ? "+" : ""}${k}`;

  return (
    <Badge
      tone={tone}
      title={
        `${label}${step}. ` +
        (PAID_FOR.includes(resolved)
          ? "Bought: it costs a debit and pays only if the move continues."
          : "Sold: it takes in a credit and keeps it if the market stays put.")
      }
    >
      {label}
    </Badge>
  );
}

/** Whether a structure was paid for rather than sold. */
export function isBought(kind?: UnitKind): boolean {
  return PAID_FOR.includes(kind ?? "condor");
}
