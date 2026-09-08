"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { BacktestJob } from "@/lib/engine";

const RANGES = [
  { days: 30, label: "1 month" },
  { days: 90, label: "3 months" },
  { days: 180, label: "6 months" },
  { days: 365, label: "1 year" },
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
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  // Collapsed once there is something to look at, so the controls stay
  // available without pushing the results down the page.
  const [open, setOpen] = useState(!hasData);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const active = job?.status === "queued" || job?.status === "running";

  const poll = useCallback(async () => {
    try {
      const res = await fetch("/api/backtest/status", { cache: "no-store" });
      const body = await res.json();
      if (res.ok) setJob(body.job ?? null);
    } catch {
      /* transient; the next tick retries */
    }
  }, []);

  useEffect(() => {
    if (!active) {
      if (timer.current) clearTimeout(timer.current);
      // A finished run means the server-rendered pages now have data.
      if (job?.status === "done") window.location.reload();
      return;
    }
    timer.current = setTimeout(poll, 2000);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [active, job, poll]);

  async function start() {
    setError(null);
    setStarting(true);
    try {
      const res = await fetch("/api/backtest/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days, lots, resolution, step: 100, max_condors: 20, roll: true }),
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
                Range
                <select
                  value={days}
                  onChange={(e) => setDays(Number(e.target.value))}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 130 }}
                >
                  {RANGES.map((r) => (
                    <option key={r.days} value={r.days}>{r.label}</option>
                  ))}
                </select>
              </label>

              <label style={{ fontSize: 11.5, fontWeight: 600, color: "var(--ink-2)" }}>
                Bar size
                <select
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                  className="auth-input"
                  style={{ marginTop: 5, minWidth: 120 }}
                >
                  <option value="D">Daily</option>
                  <option value="60">Hourly</option>
                  <option value="15">15 min</option>
                  <option value="5">5 min</option>
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
                  style={{ marginTop: 5, width: 90 }}
                />
              </label>

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
