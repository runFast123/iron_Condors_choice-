/** Indian-numbering helpers. A lakh-grouped figure is what the user expects. */

export function inr(value: number, opts: { decimals?: number; sign?: boolean } = {}): string {
  const { decimals = 0, sign = false } = opts;
  if (!Number.isFinite(value)) return "--";
  const formatted = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(Math.abs(value));
  const prefix = value < 0 ? "-" : sign ? "+" : "";
  return `${prefix}\u20B9${formatted}`;
}

/** Compact form for axis ticks: 1.2L, 45.0k. */
export function inrCompact(value: number): string {
  const abs = Math.abs(value);
  const s = value < 0 ? "-" : "";
  if (abs >= 1e7) return `${s}\u20B9${(abs / 1e7).toFixed(1)}Cr`;
  if (abs >= 1e5) return `${s}\u20B9${(abs / 1e5).toFixed(1)}L`;
  if (abs >= 1e3) return `${s}\u20B9${(abs / 1e3).toFixed(0)}k`;
  return `${s}\u20B9${abs.toFixed(0)}`;
}

export function num(value: number, decimals = 0): string {
  if (!Number.isFinite(value)) return "--";
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

export function pct(value: number, decimals = 1): string {
  if (!Number.isFinite(value)) return "--";
  return `${(value * 100).toFixed(decimals)}%`;
}

export function ratio(value: number, decimals = 2): string {
  if (!Number.isFinite(value)) return "\u221E";
  return value.toFixed(decimals);
}

export function shortDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "2-digit" });
}

export function dateTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export const signClass = (v: number) => (v > 0 ? "pos" : v < 0 ? "neg" : "flat");
