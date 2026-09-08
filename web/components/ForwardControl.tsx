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
export function ForwardControl({ initial }: { initial: LiveState | null }) {
  const [state, setState] = useState<LiveState | null>(initial);
  const [mode, setMode] = useState<"paper" | "live">("paper");
  const [lots, setLots] = useState(1);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmLive, setConfirmLive] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Rolling tick history for the live chart. Kept client-side so the line
  // builds up as the run progresses without the server storing a series.
  const [ticks, setTicks] = useState<LivePoint[]>([]);

  const session = state?.session;
  const running = session?.status === "running";
  const openPositions = (state?.positions ?? []).filter((p) => p.status === "OPEN");

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/forward/state", { cache: "no-store" });
      const body = await res.json();
      if (res.ok && body.state) {
        const next = body.state as LiveState;
        setState(next);
        recordTick(next);
      }
    } catch {
      /* transient; the next tick retries */
    }
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
    if (state) recordTick(state);
    // Seed from whatever the first render carried.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!running) {
      if (timer.current) clearTimeout(timer.current);
      return;
    }
    timer.current = setTimeout(refresh, 4000);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [running, state, refresh]);

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
    post("/api/forward/start", { mode, lots, step: 100, arm: false, poll_seconds: 10 }, "starting");
  const stop = () => post("/api/forward/stop", undefined, "stopping");
  const arm = () => {
    setConfirmLive(false);
    return post("/api/forward/arm", undefined, "arming");
  };

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
          {running && <Badge tone={session?.mode === "live" ? "neg" : "brand"}>{session?.mode?.toUpperCase()}</Badge>}
          {running && session?.armed && session.mode === "live" && <Badge tone="neg">ARMED</Badge>}
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
                Mode
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value as "paper" | "live")}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 190 }}
                >
                  <option value="paper">Paper — simulated fills</option>
                  <option value="live">Live — real orders</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                Lots per condor
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={lots}
                  onChange={(e) => setLots(Math.max(1, Number(e.target.value)))}
                  className="auth-input"
                  style={{ marginTop: 5, width: 100 }}
                />
              </label>

              <button
                onClick={start}
                disabled={busy !== null}
                className="auth-submit"
                style={{ marginTop: 0, minWidth: 150, background: mode === "live" ? "var(--neg)" : undefined }}
              >
                {busy === "starting" ? "Starting…" : mode === "live" ? "Start live run" : "Start paper run"}
              </button>
            </div>

            {mode === "live" && (
              <p style={{ fontSize: 12, color: "var(--neg)", margin: "12px 0 0", lineHeight: 1.6, maxWidth: "80ch" }}>
                A live run still will not place an order until you press <strong>Arm</strong> afterwards.
                Starting it only connects the ladder to real quotes.
              </p>
            )}
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
              <Metric label="NIFTY" value={state?.market.spot != null ? num(state.market.spot) : "—"} />
              <Metric
                label="Total P&L"
                value={inr(state?.pnl.total ?? 0, { sign: true })}
                tone={(state?.pnl.total ?? 0) > 0 ? "pos" : (state?.pnl.total ?? 0) < 0 ? "neg" : undefined}
              />
              <Metric label="Open condors" value={num(state?.pnl.open_condors ?? 0)} />
              <Metric
                label="Next entry at"
                value={state?.ladder.next_trigger != null ? num(state.ladder.next_trigger) : "—"}
              />
              <Metric label="Self-hedged" value={pct(state?.netting.offset_ratio ?? 0)} />
              <Metric label="Last tick" value={session?.last_tick ? dateTime(session.last_tick).split(", ")[1] ?? "—" : "—"} />
            </div>

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={stop} disabled={busy !== null} className="btn-danger">
                {busy === "stopping" ? "Stopping…" : "Stop run"}
              </button>

              {session?.mode === "live" && !session.armed && (
                confirmLive ? (
                  <>
                    <button onClick={arm} disabled={busy !== null} className="btn-danger">
                      {busy === "arming" ? "Arming…" : "Yes — place real orders"}
                    </button>
                    <button onClick={() => setConfirmLive(false)} className="btn-quiet">
                      Cancel
                    </button>
                  </>
                ) : (
                  <button onClick={() => setConfirmLive(true)} className="btn-danger">
                    Arm for real orders
                  </button>
                )
              )}

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
                              color: (p.pnl ?? 0) > 0 ? "var(--pos)" : (p.pnl ?? 0) < 0 ? "var(--neg)" : "var(--ink)",
                            }}
                          >
                            {inr(p.pnl ?? 0, { sign: true })}
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
