"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { LiveState } from "@/lib/live";
import { inr, num, pct, dateTime } from "@/lib/format";
import { Badge } from "@/components/ui";
import { LiveChart, type LivePoint } from "@/components/charts/LiveChart";

/**
 * Start, watch and stop a forward test from the browser.
 *
 * The engine ticks on its own thread, so the run continues whether or not this
 * page is open; this polls for state rather than driving the ladder itself.
 */
/** Matched to the engine's own poll cadence; polling faster only re-ships
 *  the same session payload. */
const POLL_MS = 10_000;

export function ForwardControl({ initial }: { initial: LiveState | null }) {
  const [state, setState] = useState<LiveState | null>(initial);
  const [lots, setLots] = useState(1);
  const [cadence, setCadence] = useState<"weekly" | "monthly">("weekly");
  const [direction, setDirection] = useState<"down" | "up" | "both">("down");
  const [anchorMode, setAnchorMode] = useState<"floor" | "nearest" | "round">("floor");
  const [maxDown, setMaxDown] = useState<number | "">(20);
  const [maxUp, setMaxUp] = useState<number | "">(10);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Tick history for the live chart. Seeded from the engine's database so a
  // reload shows the whole session rather than restarting from an empty line,
  // then extended in place as new ticks arrive.
  const [ticks, setTicks] = useState<LivePoint[]>([]);

  const session = state?.session;
  const running = session?.status === "running";
  const openPositions = (state?.positions ?? []).filter((p) => p.status === "OPEN");
  // Positions with no mark yet. Their value is unknown, not zero -- after an
  // engine restart outside market hours there is no tick to compute one.
  const unmarked = state?.pnl.unmarked_condors ?? 0;

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/forward/state", { cache: "no-store" });
      if (res.status === 401) {
        window.location.href = "/login?reason=expired";
        return;
      }
      const body = await res.json();
      if (res.ok && body.state) {
        setState(body.state as LiveState);
        recordTick(body.state as LiveState);
        setError(null);
      } else if (!res.ok) {
        setError(body.error ?? `Lost contact with the engine (${res.status}).`);
      }
    } catch (err) {
      // Swallowed, this froze the panel on "RUNNING" with a stale P&L and no
      // explanation: one blip meant no setState, so the effect never re-ran
      // and no further poll was ever scheduled.
      setError(`Lost contact with the engine (${(err as Error).message}).`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // One point per distinct tick timestamp, capped so a long session cannot
  // grow the array without bound.
  const recordTick = useCallback((s: LiveState) => {
    const price = s.market?.spot;
    const stamp = s.market?.ts ?? s.session?.last_tick;
    if (price == null || !stamp) return;
    const t = Math.floor(new Date(stamp).getTime() / 1000);
    if (!Number.isFinite(t)) return;
    setTicks((prev) => {
      if (prev.length && prev[prev.length - 1].t >= t) return prev;
      const next = [...prev, { t, price }];
      return next.length > 900 ? next.slice(next.length - 900) : next;
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    // The stored series first, so the chart is never briefly empty on a run
    // that has been going for hours.
    (async () => {
      try {
        const res = await fetch("/api/forward/ticks", { cache: "no-store" });
        const body = await res.json();
        if (cancelled || !res.ok || !Array.isArray(body.ticks)) return;
        const points = (body.ticks as { ts: string; spot: number }[])
          .map((t) => ({ t: Math.floor(new Date(t.ts).getTime() / 1000), price: t.spot }))
          .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.price));
        if (points.length) setTicks(points);
      } catch {
        /* the live poll below still builds a series from here on */
      }
    })();
    if (state) recordTick(state);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keyed only on `running`, so a failed poll cannot end the loop, and paused
  // while the tab is hidden -- there is nobody watching a chart they cannot
  // see, and each response carries the whole session.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void refresh();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [running, refresh]);

  async function post(path: string, body?: unknown, label = "working") {
    setError(null);
    setBusy(label);
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const payload = await res.json();
      if (!res.ok) setError(payload.error ?? `Request failed (${res.status}).`);
      else if (payload.state) setState(payload.state as LiveState);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const start = () =>
    post(
      "/api/forward/start",
      {
        lots,
        step: 100,
        poll_seconds: 10,
        expiry_cadence: cadence,
        direction,
        anchor_mode: anchorMode,
        max_down: maxDown !== "" ? Number(maxDown) : undefined,
        max_up: maxUp !== "" ? Number(maxUp) : undefined,
      },
      "starting",
    );
  const stop = () => post("/api/forward/stop", undefined, "stopping");

  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <div
        style={{
          padding: "13px 16px",
          borderBottom: "1px solid var(--border)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <div>
          <h2 style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>Forward test control</h2>
          <p style={{ margin: "3px 0 0", fontSize: 12, color: "var(--ink-muted)" }}>
            Runs on the engine and keeps ticking after you close this page.
          </p>
        </div>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <Badge tone={session?.market_open ? "pos" : "neutral"}>
            {session?.market_open ? "MARKET OPEN" : "MARKET CLOSED"}
          </Badge>
          {running && <Badge tone="brand">PAPER</Badge>}
          <Badge tone={running ? "pos" : "neutral"}>{running ? "RUNNING" : "STOPPED"}</Badge>
        </div>
      </div>

      <div style={{ padding: 16 }}>
        {error && (
          <div className="auth-alert auth-alert-error" role="alert" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}

        {!running ? (
          <>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                Expiry
                <select
                  value={cadence}
                  onChange={(e) => setCadence(e.target.value as "weekly" | "monthly")}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 110 }}
                >
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                Direction (v2)
                <select
                  value={direction}
                  onChange={(e) => {
                    const nextDir = e.target.value as "down" | "up" | "both";
                    setDirection(nextDir);
                    if (nextDir === "both" || nextDir === "up") {
                      if (anchorMode === "floor") setAnchorMode("nearest");
                    } else if (nextDir === "down") {
                      if (anchorMode === "nearest") setAnchorMode("floor");
                    }
                  }}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 140 }}
                >
                  <option value="down">Down-only (v1)</option>
                  <option value="both">Two-way / Both (v2)</option>
                  <option value="up">Up-only (v2)</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                Anchor Mode
                <select
                  value={anchorMode}
                  onChange={(e) => setAnchorMode(e.target.value as "floor" | "nearest" | "round")}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 110 }}
                >
                  <option value="floor">Floor</option>
                  <option value="nearest">Nearest</option>
                  <option value="round">Round</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                Lots
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={lots}
                  onChange={(e) => setLots(Math.max(1, Number(e.target.value)))}
                  className="auth-input"
                  style={{ marginTop: 5, width: 70 }}
                />
              </label>

              {direction !== "up" && (
                <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                  Max Down
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={maxDown}
                    onChange={(e) => setMaxDown(e.target.value === "" ? "" : Number(e.target.value))}
                    className="auth-input"
                    style={{ marginTop: 5, width: 80 }}
                  />
                </label>
              )}

              {direction !== "down" && (
                <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                  Max Up
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={maxUp}
                    onChange={(e) => setMaxUp(e.target.value === "" ? "" : Number(e.target.value))}
                    className="auth-input"
                    style={{ marginTop: 5, width: 80 }}
                  />
                </label>
              )}

              <button
                onClick={start}
                disabled={busy !== null}
                className="auth-submit"
                style={{ marginTop: 0, minWidth: 140 }}
              >
                {busy === "starting" ? "Starting…" : "Start paper run"}
              </button>
            </div>

            <p style={{ fontSize: 12, color: "var(--ink-muted)", margin: "12px 0 0", lineHeight: 1.6, maxWidth: "80ch" }}>
              Runs against live Choice quotes and records simulated fills. This platform places no
              orders — there is no live-trading path to switch into.
            </p>
          </>
        ) : (
          <>
            {session?.last_error && (
              <div className="auth-alert auth-alert-error" style={{ marginBottom: 14 }}>
                <strong>No live quotes.</strong> {session.last_error}
              </div>
            )}

            <div
              style={{
                border: "1px solid var(--border)",
                borderRadius: 10,
                overflow: "hidden",
                marginBottom: 14,
                background: "var(--surface)",
              }}
            >
              <div
                style={{
                  padding: "8px 12px",
                  borderBottom: "1px solid var(--border)",
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: 11.5,
                  color: "var(--ink-muted)",
                }}
              >
                <span>
                  NIFTY live &middot; {ticks.length} tick{ticks.length === 1 ? "" : "s"}
                </span>
                <span style={{ display: "flex", gap: 12 }}>
                  <span style={{ color: "var(--c3)" }}>&#9473; condor open</span>
                  <span style={{ color: "var(--accent)" }}>&#9476; next entry</span>
                </span>
              </div>
              {ticks.length > 0 ? (
                <LiveChart
                  points={ticks}
                  firedLevels={state?.ladder.fired ?? []}
                  nextTrigger={state?.ladder.next_trigger ?? null}
                />
              ) : (
                <div style={{ padding: "36px 16px", textAlign: "center", fontSize: 12.5, color: "var(--ink-muted)" }}>
                  Waiting for the first live quote&hellip;
                </div>
              )}
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
                gap: 10,
                marginBottom: 14,
              }}
            >
              <Metric
                label={state?.market.stale ? "NIFTY (last candle)" : "NIFTY"}
                value={state?.market.spot != null ? num(state.market.spot) : "—"}
              />
              <Metric
                label={unmarked > 0 ? "Total P&L (partial)" : "Total P&L"}
                value={
                  unmarked > 0 && (state?.pnl.open_condors ?? 0) === unmarked
                    ? "--"
                    : inr(state?.pnl.total ?? 0, { sign: true })
                }
                tone={(state?.pnl.total ?? 0) > 0 ? "pos" : (state?.pnl.total ?? 0) < 0 ? "neg" : undefined}
              />
              {state?.ladder.direction === "both" && (
                <>
                  <Metric
                    label="Down-side P&L"
                    value={inr(state?.pnl.down_pnl ?? 0, { sign: true })}
                    tone={(state?.pnl.down_pnl ?? 0) > 0 ? "pos" : (state?.pnl.down_pnl ?? 0) < 0 ? "neg" : undefined}
                  />
                  <Metric
                    label="Up-side P&L"
                    value={inr(state?.pnl.up_pnl ?? 0, { sign: true })}
                    tone={(state?.pnl.up_pnl ?? 0) > 0 ? "pos" : (state?.pnl.up_pnl ?? 0) < 0 ? "neg" : undefined}
                  />
                </>
              )}
              <Metric label="Open condors" value={num(state?.pnl.open_condors ?? 0)} />
              <Metric
                label={state?.ladder.direction === "both" ? "Next down" : "Next entry at"}
                value={state?.ladder.next_down != null ? num(state.ladder.next_down) : state?.ladder.next_trigger != null ? num(state.ladder.next_trigger) : "—"}
              />
              {state?.ladder.direction === "both" && (
                <Metric
                  label="Next up"
                  value={state?.ladder.next_up != null ? num(state.ladder.next_up) : "—"}
                />
              )}
              <Metric label="Self-hedged" value={pct(state?.netting.offset_ratio ?? 0)} />
              <Metric label="Last tick" value={session?.last_tick ? dateTime(session.last_tick).split(", ")[1] ?? "—" : "—"} />
            </div>

            {unmarked > 0 && (
              <p style={{ fontSize: 11.5, color: "var(--ink-muted)", margin: "-4px 0 10px", lineHeight: 1.6 }}>
                {unmarked} open {unmarked === 1 ? "condor has" : "condors have"} no live mark yet, so
                {unmarked === (state?.pnl.open_condors ?? 0) ? " no" : " the"} unrealised P&amp;L
                {unmarked === (state?.pnl.open_condors ?? 0) ? " can be shown" : " above is partial"}.
                Marks are computed on each tick; the run resumes marking when the market reopens.
              </p>
            )}

            {state?.market.stale && (
              <p style={{ fontSize: 11.5, color: "var(--ink-muted)", margin: "-4px 0 10px", lineHeight: 1.6 }}>
                Choice&rsquo;s live-quote endpoint does not serve index tokens, so NIFTY is being read
                from the most recent traded candle. A real price, one bar behind the touch.
              </p>
            )}

            {state?.fill_quality && state.fill_quality.legs_on_real_depth +
              state.fill_quality.legs_on_modelled_spread > 0 && (
              <p style={{ fontSize: 11.5, color: "var(--ink-muted)", margin: "-4px 0 14px", lineHeight: 1.6 }}>
                <strong style={{ color: "var(--ink-2)" }}>Fill quality:</strong>{" "}
                {pct(state.fill_quality.real_depth_fraction)} of legs filled against a real order
                book; the rest were charged a modelled spread. Total slippage paid{" "}
                {inr(state.fill_quality.total_slippage)} per share across all legs.
              </p>
            )}

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={stop} disabled={busy !== null} className="btn-danger">
                {busy === "stopping" ? "Stopping…" : "Stop run"}
              </button>

              <button onClick={refresh} className="btn-quiet">
                Refresh now
              </button>
            </div>

            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink-2)", marginBottom: 7 }}>
                Open positions
              </div>
              {openPositions.length === 0 ? (
                <div style={{ padding: "18px 12px", textAlign: "center", fontSize: 12.5, color: "var(--ink-muted)", background: "var(--surface-3)", borderRadius: 8 }}>
                  No condors open yet. The first one opens on the next tick.
                </div>
              ) : (
                <div className="scroll-x">
                  <table>
                    <thead>
                      <tr>
                        <th>Level</th><th>Expiry</th>
                        <th style={{ textAlign: "right" }}>Credit</th>
                        <th style={{ textAlign: "right" }}>Live P&amp;L</th>
                        <th style={{ textAlign: "right" }}>Max loss</th>
                        <th>Legs</th>
                      </tr>
                    </thead>
                    <tbody>
                      {openPositions.map((p) => (
                        <tr key={p.index}>
                          <td className="tnum" style={{ fontWeight: 700 }}>{num(p.level)}</td>
                          <td style={{ color: "var(--ink-2)" }}>{p.expiry}</td>
                          <td className="tnum" style={{ textAlign: "right" }}>{inr(p.credit)}</td>
                          <td
                            className="tnum"
                            style={{
                              textAlign: "right", fontWeight: 700,
                              color: p.pnl == null ? "var(--ink-muted)"
                                : p.pnl > 0 ? "var(--pos)" : p.pnl < 0 ? "var(--neg)" : "var(--ink)",
                            }}
                          >
                            {p.pnl == null ? "--" : inr(p.pnl, { sign: true })}
                          </td>
                          <td className="tnum" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                            {inr(-p.max_loss)}
                          </td>
                          <td>
                            <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                              {p.legs.map((l, i) => (
                                <Badge key={i} tone={l.side === "SELL" ? "warn" : "brand"}>
                                  {l.side === "SELL" ? "S" : "B"} {num(l.strike)}{l.right}
                                </Badge>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {session?.stopped_reason && (
              <p style={{ fontSize: 12, color: "var(--neg)", margin: "12px 0 0" }}>
                <strong>Stopped:</strong> {session.stopped_reason}
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  const color = tone === "pos" ? "var(--pos)" : tone === "neg" ? "var(--neg)" : "var(--ink)";
  return (
    <div style={{ background: "var(--surface-3)", borderRadius: 8, padding: "9px 11px" }}>
      <div style={{ fontSize: 10.5, color: "var(--ink-muted)", fontWeight: 600 }}>{label}</div>
      <div className="tnum" style={{ fontSize: 17, fontWeight: 700, color, marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}
