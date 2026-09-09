/** Indian-numbering helpers. A lakh-grouped figure is what the user expects. */

/**
 * Every timestamp in this app is an Indian market time, and these render on
 * the server -- which on Vercel runs in UTC. Without an explicit zone a trade
 * stamped 10:18 IST is shown to every user as 04:48, and a pre-05:30 stamp
 * moves to the previous calendar day. The zone is pinned, not inherited.
 */
const IST = "Asia/Kolkata";

/**
 * Collapse negative zero, and values that round to it, onto plain zero.
 * Float noise on a leg's P&L otherwise reaches the screen as "-₹0".
 */
function snapZero(value: number, decimals: number): number {
  return Math.abs(value) < 0.5 / 10 ** decimals ? 0 : value;
}

export function inr(value: number, opts: { decimals?: number; sign?: boolean } = {}): string {
  const { decimals = 0, sign = false } = opts;
  if (!Number.isFinite(value)) return "--";
  value = snapZero(value, decimals);
  const formatted = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(Math.abs(value));
  const prefix = value < 0 ? "-" : sign ? "+" : "";
  return `${prefix}\u20B9${formatted}`;
}

/** Compact form for axis ticks: 1.2L, 45.0k. */
export function inrCompact(value: number): string {
  if (!Number.isFinite(value)) return "--";
  const abs = Math.abs(value);
  const s = value < 0 ? "-" : "";
  if (abs >= 1e7) return `${s}\u20B9${(abs / 1e7).toFixed(1)}Cr`;
  if (abs >= 1e5) return `${s}\u20B9${(abs / 1e5).toFixed(1)}L`;
  if (abs >= 1e3) return `${s}\u20B9${(abs / 1e3).toFixed(0)}k`;
  return `${s}\u20B9${abs.toFixed(0)}`;
}

export function num(value: number, decimals = 0): string {
  if (!Number.isFinite(value)) return "--";
  value = snapZero(value, decimals);
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
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleDateString("en-IN", { timeZone: IST, day: "2-digit", month: "short", year: "2-digit" });
}

export function dateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--";
  return d.toLocaleString("en-IN", {
    timeZone: IST,
    day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export const signClass = (v: number) => (v > 0 ? "pos" : v < 0 ? "neg" : "flat");

/**
 * Axis and crosshair labels for lightweight-charts, in IST.
 *
 * The library renders UNIX timestamps in UTC unless told otherwise, so a tick
 * stamped 09:33 IST appeared on the axis as 04:03 -- next to a "Last tick"
 * metric that read 09:33 am, because that one goes through dateTime(). Same
 * data, two clocks, five and a half hours apart.
 */
export function istClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString("en-IN", {
    timeZone: IST,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function istDay(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleDateString("en-IN", {
    timeZone: IST,
    day: "2-digit",
    month: "short",
  });
}

