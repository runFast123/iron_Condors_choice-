"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { BacktestJob } from "@/lib/engine";
import { InfoTooltip } from "./InfoTooltip";

/** The first day Choice serves intraday NIFTY and India VIX bars. Option
 *  prices before that exist in the recorded history, but a replay needs the
 *  index too. */
const DATA_START = Date.UTC(2018, 10, 22);
const ALL_DATA_DAYS = Math.floor((Date.now() - DATA_START) / 86_400_000);

const RANGES = [
  { days: 30, label: "1 month" },
  { days: 90, label: "3 months" },
  { days: 180, label: "6 months" },
  { days: 365, label: "1 year" },
  { days: 730, label: "2 years" },
  { days: 1095, label: "3 years" },
  { days: 1826, label: "5 years" },
  { days: ALL_DATA_DAYS, label: "All data (from Nov 2018)" },
];

/**
 * Starts a backtest for the signed-in user and follows its progress.
 *
 * A run fetches historical candles for every option leg the ladder touches,
 * which takes minutes against a real broker -- so the server runs it on a
 * worker thread and this polls, rather than holding a request open and timing
 * out halfway through.
 */
export function RunBacktest({
  initialJob = null,
  hasData = false,
  currentLabel,
}: {
  initialJob?: BacktestJob | null;
  /** When a result is already on screen, this collapses to a summary bar. */
  hasData?: boolean;
  currentLabel?: string;
}) {
  const [job, setJob] = useState<BacktestJob | null>(initialJob);
  const [days, setDays] = useState(90);
  const [lots, setLots] = useState(1);
  const [resolution, setResolution] = useState("D");
  const [cadence, setCadence] = useState<"weekly" | "monthly">("weekly");
  const [strategy, setStrategy] = useState<"ladder" | "hic">("ladder");
  const [bandSteps, setBandSteps] = useState(1);
  const [halfMode, setHalfMode] = useState<"buy" | "sell">("buy");
  const [debitShift, setDebitShift] = useState<0 | 200>(0);
  const [direction, setDirection] = useState<"down" | "up" | "both">("down");
  const [anchorMode, setAnchorMode] = useState<"floor" | "nearest" | "round">("floor");
  const [maxDown, setMaxDown] = useState<number | "">(20);
  const [maxUp, setMaxUp] = useState<number | "">(10);
  // Ladder-only entry filters. "" is off, which is the default.
  const [minDte, setMinDte] = useState<number | "">("");
  const [minCredit, setMinCredit] = useState<number | "">("");
  // The VIX rule, both strategies: no new positions while India VIX is above
  // this. On at 15; cleared ("") switches it off.
  const [maxVix, setMaxVix] = useState<number | "">(15);
  const [takeProfit, setTakeProfit] = useState<number | "">("");
  const [stopLoss, setStopLoss] = useState<number | "">("");
  const [trailingSl, setTrailingSl] = useState<number | "">("");
  const [trailingTrigger, setTrailingTrigger] = useState<number | "">("");
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  // Collapsed once there is something to look at, so the controls stay
  // available without pushing the results down the page.
  const [open, setOpen] = useState(!hasData);
  // True once a run has been observed in flight during this page's life.
  const sawActive = useRef(false);

  const active = job?.status === "queued" || job?.status === "running";

  const poll = useCallback(async () => {
    try {
      const res = await fetch("/api/backtest/status", { cache: "no-store" });
      const body = await res.json();
      if (res.ok) {
        setJob(body.job ?? null);
        setError(null);
      } else if (res.status === 401) {
        window.location.href = "/login?reason=expired";
      } else {
        setError(body.error ?? `Could not read progress (${res.status}).`);
      }
    } catch (err) {
      // Not swallowed: the interval keeps polling, but the user is told the
      // bar has stopped moving because we lost the engine, not because the
      // run stalled.
      setError(`Lost contact with the engine while the run was in flight (${(err as Error).message}).`);
    }
  }, []);

  // A repeating interval keyed only on `active`.
  //
  // The previous version chained a setTimeout off `state` changing identity,
  // so a single failed poll -- a blip, a 500, an expired session -- meant no
  // setState, no re-render, and therefore no next timer. The progress bar
  // froze at its last percentage forever while the run finished on the server.
  useEffect(() => {
    if (!active) return;
    sawActive.current = true;
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, [active, poll]);

  useEffect(() => {
    // Reload only when a run finished *while this page was open*. Reloading
    // whenever the job is "done" was an infinite loop: the completed job is
    // still the latest one after the reload, so the effect fired again
    // immediately and the page reloaded forever.
    if (!active && job?.status === "done" && sawActive.current) {
      sawActive.current = false;
      window.location.reload();
    }
  }, [active, job]);

  async function start() {
    setError(null);
    setStarting(true);
    try {
      const res = await fetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          days,
          lots,
          resolution,
          step: 100,
          max_condors: 20,
          roll: true,
          expiry_cadence: cadence,
          strategy,
          // Null, not omitted, when cleared: an omitted field takes the
          // engine's default of 15, which is the opposite of "off".
          max_entry_vix: maxVix === "" ? null : Number(maxVix),
          take_profit: takeProfit !== "" ? Number(takeProfit) : undefined,
          stop_loss: stopLoss !== "" ? Number(stopLoss) : undefined,
          trailing_sl: trailingSl !== "" ? Number(trailingSl) : undefined,
          trailing_sl_trigger: trailingTrigger !== "" ? Number(trailingTrigger) : undefined,
          // HIC derives direction and both caps from its band and spreads, and
          // is symmetric by construction. The ladder's own settings used to be
          // sent to it anyway -- including an untouched Max up of 10 behind a
          // hidden box, which cut its call side short of its put side.
          ...(strategy === "hic"
            ? {
                full_band_steps: bandSteps,
                half_mode: halfMode,
                debit_shift: halfMode === "sell" ? 0 : debitShift,
              }
            : {
                direction,
                anchor_mode: anchorMode,
                max_down: maxDown !== "" ? Number(maxDown) : undefined,
                max_up: maxUp !== "" ? Number(maxUp) : undefined,
                min_entry_dte: minDte !== "" ? Number(minDte) : undefined,
                min_credit_ratio: minCredit !== "" ? Number(minCredit) : undefined,
              }),
        }),
      });
      const body = await res.json();
      if (!res.ok) {
        setError(body.error ?? `Could not start the run (${res.status}).`);
      } else {
        setJob(body.job);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setStarting(false);
    }
  }

  return (
    <section className="card" style={{ overflow: "hidden" }}>
      <div
        style={{
          padding: "12px 16px",
          borderBottom: open || active ? "1px solid var(--border)" : "none",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <div>
          <h2 style={{ margin: 0, fontSize: 14, fontWeight: 700 }}>
            {hasData ? "Backtest settings" : "Run a backtest"}
          </h2>
          <p style={{ margin: "3px 0 0", fontSize: 12.5, color: "var(--ink-muted)", maxWidth: "80ch", lineHeight: 1.6 }}>
            {hasData && !open
              ? currentLabel
                ? `Showing ${currentLabel}. Change the range or bar size and run it again.`
                : "Change the range or bar size and run it again."
              : "Replays the ladder over your own Choice history. Premiums are fetched per leg, so a longer range takes proportionally longer."}
          </p>
        </div>
        {hasData && !active && (
          <button onClick={() => setOpen((v) => !v)} className="btn-quiet">
            {open ? "Hide" : "Run another backtest"}
          </button>
        )}
      </div>

      <div style={{ padding: open || active ? 16 : 0 }}>
        {error && (
          <div className="auth-alert auth-alert-error" role="alert" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}

        {active ? (
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 6 }}>
              <strong>{job?.message || job?.stage}</strong>
              <span className="tnum" style={{ color: "var(--ink-muted)" }}>
                {Math.round((job?.progress ?? 0) * 100)}%
              </span>
            </div>
            <div className="progress-track">
              <div className="progress-bar" style={{ width: `${Math.max(2, (job?.progress ?? 0) * 100)}%` }} />
            </div>
            <p style={{ fontSize: 11.5, color: "var(--ink-muted)", margin: "8px 0 0" }}>
              Stage: <code className="mono">{job?.stage}</code>. You can leave this page; the run
              continues on the server.
            </p>
          </div>
        ) : !open ? null : (
          <>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Range
                  <InfoTooltip field="range" />
                </span>
                <select
                  value={days}
                  onChange={(e) => setDays(Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 120 }}
                >
                  {RANGES.map((r) => (
                    <option key={r.days} value={r.days}>{r.label}</option>
                  ))}
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Bar size
                  <InfoTooltip field="resolution" />
                </span>
                <select
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 100 }}
                >
                  <option value="D">Daily</option>
                  <option value="60">Hourly</option>
                  <option value="15">15 min</option>
                  <option value="5">5 min</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Expiry
                  <InfoTooltip field="cadence" />
                </span>
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
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Strategy
                  <InfoTooltip field="strategy" />
                </span>
                <select
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value as "ladder" | "hic")}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 180 }}
                >
                  <option value="ladder">Condor ladder</option>
                  <option value="hic">Hybrid iron condor</option>
                </select>
              </label>

              {strategy === "hic" && (
                <>
                  <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                    <span style={{ display: "inline-flex", alignItems: "center" }}>
                      Core band
                      <InfoTooltip field="hic_band" />
                    </span>
                    <select
                      value={bandSteps}
                      onChange={(e) => setBandSteps(Number(e.target.value))}
                      className="auth-input"
                      style={{ marginTop: 5, minWidth: 150 }}
                    >
                      <option value={0}>Anchor only</option>
                      <option value={1}>Anchor ± 1 step</option>
                      <option value={2}>Anchor ± 2 steps</option>
                    </select>
                  </label>

                  <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                    <span style={{ display: "inline-flex", alignItems: "center" }}>
                      Beyond the band
                      <InfoTooltip field="hic_half_mode" />
                    </span>
                    <select
                      value={halfMode}
                      onChange={(e) => setHalfMode(e.target.value as "buy" | "sell")}
                      className="auth-input"
                      style={{ marginTop: 5, minWidth: 165 }}
                    >
                      <option value="buy">Buy a spread</option>
                      <option value="sell">Sell one (comparison)</option>
                    </select>
                  </label>

                  {halfMode === "buy" && (
                    <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                      <span style={{ display: "inline-flex", alignItems: "center" }}>
                        Spread strikes
                        <InfoTooltip field="hic_debit_shift" />
                      </span>
                      <select
                        value={debitShift}
                        onChange={(e) => setDebitShift(Number(e.target.value) as 0 | 200)}
                        className="auth-input"
                        style={{ marginTop: 5, minWidth: 175 }}
                      >
                        <option value={0}>Reverse the condor&rsquo;s</option>
                        <option value={200}>Bought at the level</option>
                      </select>
                    </label>
                  )}
                </>
              )}

              {strategy === "hic" ? null : (
              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Direction (v2)
                  <InfoTooltip field="direction" />
                </span>
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
              )}

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Anchor Mode
                  <InfoTooltip field="anchor_mode" />
                </span>
                <select
                  value={anchorMode}
                  onChange={(e) => setAnchorMode(e.target.value as "floor" | "nearest" | "round")}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 120 }}
                >
                  <option value="floor">Floor</option>
                  <option value="nearest">Nearest</option>
                  <option value="round">Round</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Lots
                  <InfoTooltip field="lots" />
                </span>
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

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Pause above VIX
                  <InfoTooltip field="max_vix" />
                </span>
                <input
                  type="number"
                  min={1}
                  max={100}
                  step={0.5}
                  value={maxVix}
                  placeholder="Off"
                  onChange={(e) => setMaxVix(e.target.value === "" ? "" : Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, width: 90 }}
                />
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Trailing SL (×)
                  <InfoTooltip field="trailing_sl" />
                </span>
                <input
                  type="number"
                  min={0.1}
                  max={10}
                  step={0.1}
                  value={trailingSl}
                  placeholder="Off"
                  onChange={(e) => setTrailingSl(e.target.value === "" ? "" : Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, width: 85 }}
                />
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Trail trigger
                  <InfoTooltip field="trailing_trigger" />
                </span>
                <select
                  value={trailingTrigger}
                  onChange={(e) => setTrailingTrigger(e.target.value === "" ? "" : Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 120 }}
                >
                  <option value="">Auto (breakeven)</option>
                  <option value={0.1}>At 10% profit</option>
                  <option value={0.2}>At 20% profit</option>
                  <option value={0.3}>At 30% profit</option>
                  <option value={0.5}>At 50% profit</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Take profit
                  <InfoTooltip field="take_profit" />
                </span>
                <select
                  value={takeProfit}
                  onChange={(e) => setTakeProfit(e.target.value === "" ? "" : Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 110 }}
                >
                  <option value="">Hold</option>
                  <option value={0.4}>40% of credit</option>
                  <option value={0.5}>50% of credit</option>
                  <option value={0.6}>60% of credit</option>
                  <option value={0.75}>75% of credit</option>
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                <span style={{ display: "inline-flex", alignItems: "center" }}>
                  Stop loss
                  <InfoTooltip field="stop_loss" />
                </span>
                <select
                  value={stopLoss}
                  onChange={(e) => setStopLoss(e.target.value === "" ? "" : Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 100 }}
                >
                  <option value="">Hold</option>
                  <option value={1.0}>1.0× credit</option>
                  <option value={1.5}>1.5× credit</option>
                  <option value={2.0}>2.0× credit</option>
                  <option value={3.0}>3.0× credit</option>
                </select>
              </label>

              {strategy !== "hic" && (
                <>
                  <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                    <span style={{ display: "inline-flex", alignItems: "center" }}>
                      Skip late entries
                      <InfoTooltip field="min_dte" />
                    </span>
                    <select
                      value={minDte}
                      onChange={(e) => setMinDte(e.target.value === "" ? "" : Number(e.target.value))}
                      className="auth-input"
                      style={{ marginTop: 5, minWidth: 150 }}
                    >
                      <option value="">Off</option>
                      <option value={3}>Under 3 days left</option>
                      <option value={5}>Under 5 days left</option>
                      <option value={8}>Under 8 days left</option>
                      <option value={12}>Under 12 days left</option>
                    </select>
                  </label>
                  <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                    <span style={{ display: "inline-flex", alignItems: "center" }}>
                      Minimum credit
                      <InfoTooltip field="min_credit" />
                    </span>
                    <select
                      value={minCredit}
                      onChange={(e) => setMinCredit(e.target.value === "" ? "" : Number(e.target.value))}
                      className="auth-input"
                      style={{ marginTop: 5, minWidth: 150 }}
                    >
                      <option value="">Off</option>
                      <option value={0.45}>45% of the wing</option>
                      <option value={0.5}>50% (loss ≤ credit)</option>
                      <option value={0.55}>55% of the wing</option>
                    </select>
                  </label>
                </>
              )}

              {strategy !== "hic" && direction !== "up" && (
                <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                  <span style={{ display: "inline-flex", alignItems: "center" }}>
                    Max down
                    <InfoTooltip field="max_down" />
                  </span>
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

              {strategy !== "hic" && direction !== "down" && (
                <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                  <span style={{ display: "inline-flex", alignItems: "center" }}>
                    Max up
                    <InfoTooltip field="max_up" />
                  </span>
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

              <button onClick={start} disabled={starting} className="auth-submit" style={{ marginTop: 0, minWidth: 140 }}>
                {starting ? "Starting…" : hasData ? "Run again" : "Run backtest"}
              </button>
            </div>

            {job?.status === "error" && (
              <div className="auth-alert auth-alert-error" style={{ marginTop: 14 }}>
                <strong>The last run failed.</strong> {job.error}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
