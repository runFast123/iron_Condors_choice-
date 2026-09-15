import Link from "next/link";
import type { ForwardRunSummary } from "@/lib/engine";
import { inr } from "@/lib/format";

/**
 * Which forward test the page below is about.
 *
 * With several runs going at once, every number on a page -- P&L, fills, the
 * event log, the ladder's fired levels -- belongs to exactly one of them, and
 * they look identical. A page that does not say which run it is showing is
 * not merely unhelpful, it is misleading: the reader has no way to tell a
 * winning run from a losing one that happens to be on screen.
 *
 * So this sits at the top of every run-scoped page, names the run, and lets
 * the reader move between them without losing their place.
 */
export function RunScope({
  runs,
  active,
  basePath,
}: {
  runs: ForwardRunSummary[];
  active: string;
  /** Where the tabs point. The run travels in the query string. */
  basePath: string;
}) {
  if (runs.length === 0) return null;

  // With a single run there is nothing to confuse it with, so a full switcher
  // would be noise. The name is still worth stating.
  const single = runs.length === 1;
  const current = runs.find((r) => r.run_key === active);

  return (
    <div
      className="card"
      style={{
        display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center",
        padding: "9px 13px", fontSize: 12.5,
      }}
    >
      <span style={{ color: "var(--ink-muted)", fontWeight: 600 }}>
        {single ? "Showing run" : "Showing"}
      </span>

      {single ? (
        <strong>{current?.label ?? active}</strong>
      ) : (
        <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {runs.map((r) => {
            const selected = r.run_key === active;
            return (
              <Link
                key={r.run_key}
                href={`${basePath}?run=${encodeURIComponent(r.run_key)}`}
                aria-current={selected ? "page" : undefined}
                style={{
                  padding: "4px 10px", borderRadius: "var(--radius)", fontWeight: 600,
                  border: `1px solid ${selected ? "var(--brand)" : "var(--border)"}`,
                  background: selected ? "var(--brand-soft)" : "var(--surface)",
                  color: "var(--ink)", textDecoration: "none",
                }}
              >
                {r.label}
                {!r.running && (
                  <span style={{ fontWeight: 400, color: "var(--ink-muted)", marginLeft: 5, fontSize: 11 }}>
                    stopped
                  </span>
                )}
              </Link>
            );
          })}
        </span>
      )}

      {current && (
        <span
          className="tnum"
          style={{ marginLeft: "auto", color: "var(--ink-muted)", fontSize: 11.5 }}
        >
          {current.strategy}
          {current.direction ? ` · ${current.direction}` : ""} · {current.lots} lot
          {current.lots === 1 ? "" : "s"} · {inr(current.pnl?.total ?? 0, { sign: true })}
        </span>
      )}
    </div>
  );
}

/**
 * The run a page should show, from its own query string.
 *
 * Falls back to the first run the user actually has rather than to a fixed
 * name, so a page reached with no run, by someone whose only test is called
 * something else, shows that test instead of an empty panel about a run that
 * does not exist.
 */
export function resolveRun(requested: string | undefined, runs: ForwardRunSummary[]): string {
  if (requested && runs.some((r) => r.run_key === requested)) return requested;
  if (requested && runs.length === 0) return requested;
  return runs[0]?.run_key ?? "ladder";
}
