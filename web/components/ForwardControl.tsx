"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { LiveState } from "@/lib/live";
import type { ForwardRunSummary } from "@/lib/engine";
import { inr, num, pct, dateTime, istClock, istDay } from "@/lib/format";
import { Badge } from "@/components/ui";
import { UnitKindBadge } from "@/components/UnitKindBadge";
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

/**
 * One field in the start form: label, control, hint, stacked.
 *
 * A column, because a `<label>` whose text and control are inline siblings
 * puts them side by side -- and only wraps when something *else* inside forces
 * the box narrow. That is why the selects, which carry a 190px hint below
 * them, had their label above while the bare number inputs had theirs beside:
 * the same markup, laid out two different ways, in one row.
 */
const FIELD: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "flex-start",
  gap: 4,
  fontSize: 11.5,
  fontWeight: 600,
  color: "var(--ink-2)",
};

/** The explanatory line under a field. */
const HINT: CSSProperties = {
  fontSize: 10.5,
  fontWeight: 400,
  lineHeight: 1.45,
  color: "var(--ink-muted)",
};

/** The run a page with no selection is about. Named this since before runs
 *  had names, which is why it is the default everywhere. */
const DEFAULT_RUN = "ladder";

export function ForwardControl({
  initial,
  runKey: initialRunKey,
}: {
  initial: LiveState | null;
  /** Which run the page was rendered for, from its query string. */
  runKey?: string;
}) {
  const [rawState, setRawState] = useState<LiveState | null>(initial);
  // Which run this panel is showing, and every run the user has. A forward
  // test keeps ticking in the engine whether or not it is the one on screen.
  const [runKey, setRunKey] = useState<string>(initialRunKey ?? DEFAULT_RUN);
  // Which run the state in hand actually belongs to. Switching tabs changes
  // `runKey` immediately but the fetch takes a moment, and until it landed the
  // panel rendered the *previous* run's P&L, positions and ladder under the
  // new run's name -- the one thing per-run isolation is supposed to prevent.
  const [stateRun, setStateRun] = useState<string>(initialRunKey ?? DEFAULT_RUN);
  const state = stateRun === runKey ? rawState : null;
  const [runs, setRuns] = useState<ForwardRunSummary[]>([]);
  const [maxRuns, setMaxRuns] = useState(5);
  const [runName, setRunName] = useState("");
  // Whether the start form is on screen. It used to be shown only when
  // nothing was running, which meant that once a test was going there was no
  // way to start a second one -- the feature existed in the engine and was
  // unreachable from the page.
  const [starting, setStarting] = useState(false);
  const [lots, setLots] = useState(1);
  // Which strategy a new run trades. Without this the form could only ever
  // start a ladder, which made HIC unreachable from the page entirely.
  const [strategy, setStrategy] = useState<"ladder" | "hic">("ladder");
  // HIC only. The ladder has no band and buys no spreads.
  const [bandSteps, setBandSteps] = useState(1);
  const [putSpreads, setPutSpreads] = useState(10);
  const [callSpreads, setCallSpreads] = useState(10);
  const [debitShift, setDebitShift] = useState<0 | 200>(0);
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

  // What the next rung on each side will actually be. A HIC level inside the
  // band opens a full four-leg condor and one beyond it opens a two-leg
  // bought spread, and the level alone does not say which -- so a rung that
  // looked like it should be a spread arrived as a condor with no explanation.
  const SHAPE_WORD: Record<string, string> = {
    condor: "condor",
    put_debit_spread: "put spread",
    call_debit_spread: "call spread",
    put_credit_spread: "put credit spread",
    call_credit_spread: "call credit spread",
  };
  const shapeHint = (kind: string | null | undefined) =>
    kind ? SHAPE_WORD[kind] ?? kind : undefined;

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`/api/forward/state?run=${encodeURIComponent(runKey)}`, {
        cache: "no-store",
      });
      if (res.status === 401) {
        window.location.href = "/login?reason=expired";
        return;
      }
      const body = await res.json();
      // The roll-call rides along with the state now, so one poll does what
      // two used to. Each request is a browser -> Vercel -> tunnel -> India
      // round trip, and there were two of them every ten seconds.
      if (Array.isArray(body.runs)) {
        setRuns(body.runs as ForwardRunSummary[]);
        if (typeof body.max_runs === "number") setMaxRuns(body.max_runs);
      }
      if (res.ok && body.state) {
        setRawState(body.state as LiveState);
        setStateRun(runKey);
        recordTick(body.state as LiveState);
        setError(null);
      } else if (res.ok) {
        // A successful response carrying no state means the engine no longer
        // has this run. Keeping the last one on screen left a stopped or
        // retired run looking live, ticking P&L and all, with nothing said.
        setRawState(null);
        setStateRun(runKey);
        setError(`The engine has no run called "${runKey}" any more.`);
      } else {
        setError(body.error ?? `Lost contact with the engine (${res.status}).`);
      }
    } catch (err) {
      // Swallowed, this froze the panel on "RUNNING" with a stale P&L and no
      // explanation: one blip meant no setState, so the effect never re-ran
      // and no further poll was ever scheduled.
      setError(`Lost contact with the engine (${(err as Error).message}).`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey]);

  /** The roll-call of runs, which is separate from any one run's state. */
  const refreshRuns = useCallback(async () => {
    try {
      const res = await fetch("/api/forward/runs", { cache: "no-store" });
      if (!res.ok) return;
      const body = await res.json();
      if (Array.isArray(body.runs)) {
        setRuns(body.runs as ForwardRunSummary[]);
        if (typeof body.max_runs === "number") setMaxRuns(body.max_runs);
      }
    } catch {
      /* the panel still works on the single run it is showing */
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
    let cancelled = false;
    // The stored series first, so the chart is never briefly empty on a run
    // that has been going for hours.
    (async () => {
      try {
        const res = await fetch(`/api/forward/ticks?run=${encodeURIComponent(runKey)}`, {
          cache: "no-store",
        });
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
    setTicks([]);
    void refresh();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey]);

  // Keyed only on `running`, so a failed poll cannot end the loop, and paused
  // while the tab is hidden -- there is nobody watching a chart they cannot
  // see, and each response carries the whole session.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void refresh();          // carries the roll-call with it
    }, POLL_MS);
    return () => clearInterval(id);
  }, [running, refresh]);

  // The list has to keep refreshing even when the run on screen has stopped,
  // or a user watching a finished run would never see the others move.
  useEffect(() => {
    if (running) return;
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void refreshRuns();
    }, POLL_MS);
    return () => clearInterval(id);
  }, [running, refreshRuns]);

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
      else {
        if (payload.run_key) selectRun(payload.run_key as string);
        if (payload.state) {
          setRawState(payload.state as LiveState);
          setStateRun((payload.run_key as string) ?? runKey);
        }
        void refreshRuns();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const start = async () => {
    await post(
      "/api/forward/start",
      {
        strategy,
        name: runName.trim() || undefined,
        lots,
        step: 100,
        poll_seconds: 10,
        expiry_cadence: cadence,
        ...(strategy === "hic"
          ? {
              full_band_steps: bandSteps,
              max_put_spreads: putSpreads,
              max_call_spreads: callSpreads,
              debit_shift: debitShift,
              // No direction, anchor or per-side caps. HIC derives all four
              // from the band and the spread counts, and it is symmetric by
              // construction. Sending the ladder's defaults anyway is what
              // produced a live run with max_down 12 and max_up 10: the
              // Direction control is hidden for HIC so `direction` stayed
              // "down", which hid the Max up box -- while the form kept
              // sending its untouched default of 10, quietly cutting the call
              // side two rungs shorter than the put side.
            }
          : {
              direction,
              anchor_mode: anchorMode,
              max_down: maxDown !== "" ? Number(maxDown) : undefined,
              max_up: maxUp !== "" ? Number(maxUp) : undefined,
            }),
      },
      "starting",
    );
    // Close the form and clear the name, so the next "New test" starts from a
    // blank one rather than silently reusing the last run's name.
    setStarting(false);
    setRunName("");
  };

  const stop = async (key: string = runKey) => {
    await post(`/api/forward/stop?run=${encodeURIComponent(key)}`, undefined, "stopping");
    // A stopped run drops out of the listing, so staying pointed at it would
    // leave the panel on a run that no longer exists. Move to another live
    // one if there is one.
    if (key === runKey) {
      const next = runs.find((r) => r.run_key !== key && r.running);
      selectRun(next ? next.run_key : DEFAULT_RUN);
    }
  };

  /** Switch the panel, and put the run in the address bar with it.
   *
   *  Without this the selection lives only in component state: a reload, a
   *  bookmark, or the link to the activity log would all quietly go back to
   *  whichever run the server picked, which is exactly the confusion this is
   *  meant to remove. replaceState rather than a router push, because moving
   *  between runs is not history worth a back button. */
  const selectRun = useCallback((key: string) => {
    setRunKey(key);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("run", key);
      window.history.replaceState(null, "", url.toString());
    }
  }, []);

  /**
   * Caveats that apply to the figures on screen right now.
   *
   * Built as data rather than written inline so they can be grouped, counted
   * and ordered. Warnings first: a partial P&L is a different kind of fact
   * from a fill-quality statistic, and burying the first among the second is
   * how a number gets trusted more than it deserves.
   */
  const notes: { key: string; tone: "warn" | "info"; body: ReactNode }[] = [];
  if (unmarked > 0) {
    const allUnmarked = unmarked === (state?.pnl.open_condors ?? 0);
    notes.push({
      key: "unmarked",
      tone: "warn",
      body: (
        <>
          {unmarked} open {unmarked === 1 ? "condor has" : "condors have"} no live mark yet, so
          {allUnmarked ? " no" : " the"} unrealised P&amp;L
          {allUnmarked ? " can be shown" : " above is partial"}. Marks are computed on each tick;
          the run resumes marking when the market reopens.
        </>
      ),
    });
  }
  // HIC only. The band is the rule that decides whether a rung is a condor or
  // a bought spread, and without it on screen a level inside the band looks
  // like a spread that arrived as a condor.
  const band = state?.ladder.band;
  const anchor = state?.ladder.anchor;
  if (band != null && anchor != null) {
    const step = state?.ladder.step ?? 100;
    notes.push({
      key: "hic-band",
      tone: "info",
      body: (
        <>
          <strong>Core band {band}.</strong> Anchored at {num(anchor)}, so every level from{" "}
          {num(anchor - band * step)} to {num(anchor + band * step)} opens a full four-leg
          condor. The first bought spread is at {num(anchor - (band + 1) * step)} on the way
          down and {num(anchor + (band + 1) * step)} on the way up.
        </>
      ),
    });
  }
  if (state?.market.stale) {
    notes.push({
      key: "stale-spot",
      tone: "warn",
      body: (
        <>
          Choice&rsquo;s live-quote endpoint does not serve index tokens, so NIFTY is being read
          from the most recent traded candle. A real price, one bar behind the touch.
        </>
      ),
    });
  }
  if (
    state?.fill_quality &&
    state.fill_quality.legs_on_real_depth + state.fill_quality.legs_on_modelled_spread > 0
  ) {
    notes.push({
      key: "fill-quality",
      tone: "info",
      body: (
        <>
          <strong style={{ color: "var(--ink)" }}>Fill quality:</strong>{" "}
          {pct(state.fill_quality.real_depth_fraction)} of legs filled against a real order book;
          the rest were charged a modelled spread. Total slippage paid{" "}
          {inr(state.fill_quality.total_slippage)} per share across all legs.
        </>
      ),
    });
  }

  const liveRuns = runs.filter((r) => r.running);
  const atCap = liveRuns.length >= maxRuns;

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

      {/* Every forward test this user is driving. They keep ticking in the
          engine regardless of which one is on screen, so this is a view of
          them rather than a switch that starts and stops anything. */}
      {runs.length > 0 && (
        <div
          style={{
            display: "flex", gap: 8, flexWrap: "wrap", alignItems: "stretch",
            padding: "10px 16px", borderBottom: "1px solid var(--border)",
            background: "var(--surface-3)",
          }}
        >
          {runs.map((r) => {
            const selected = r.run_key === runKey;
            const pnl = r.pnl?.total ?? 0;
            return (
              <button
                key={r.run_key}
                type="button"
                onClick={() => selectRun(r.run_key)}
                aria-current={selected ? "true" : undefined}
                title={
                  r.running
                    ? `${r.strategy}, ${r.lots} lot(s)${r.direction ? `, ${r.direction}` : ""}`
                    : (r.stopped_reason ?? "stopped")
                }
                style={{
                  textAlign: "left", cursor: "pointer", borderRadius: "var(--radius)",
                  padding: "7px 11px", fontSize: 12, lineHeight: 1.35,
                  border: `1px solid ${selected ? "var(--brand)" : "var(--border)"}`,
                  background: selected ? "var(--brand-soft)" : "var(--surface)",
                  color: "var(--ink)",
                }}
              >
                <span style={{ display: "flex", gap: 7, alignItems: "center" }}>
                  <span style={{ fontWeight: 700 }}>{r.label}</span>
                  {!r.running ? (
                    <span style={{ fontSize: 10.5, color: "var(--ink-muted)" }}>stopped</span>
                  ) : !r.ticking ? (
                    // Marked running but no worker behind it. The watchdog
                    // restarts these; saying so beats a silent frozen number.
                    <span style={{ fontSize: 10.5, color: "var(--warn)" }}>restarting</span>
                  ) : null}
                </span>
                <span
                  className="tnum"
                  style={{
                    display: "block", fontWeight: 700, fontSize: 12.5,
                    color: pnl > 0 ? "var(--pos)" : pnl < 0 ? "var(--neg)" : "var(--ink-2)",
                  }}
                >
                  {inr(pnl, { sign: true })}
                  <span
                    style={{ fontWeight: 500, color: "var(--ink-muted)", marginLeft: 6, fontSize: 11 }}
                  >
                    {r.open_condors} open
                  </span>
                </span>
              </button>
            );
          })}
          <span
            style={{
              marginLeft: "auto", alignSelf: "center", display: "flex",
              gap: 10, alignItems: "center", fontSize: 11.5, color: "var(--ink-muted)",
            }}
          >
            {liveRuns.length} of {maxRuns} running
            <button
              type="button"
              onClick={() => setStarting((v) => !v)}
              disabled={atCap && !starting}
              className="btn-quiet"
              style={{ fontSize: 11.5, padding: "4px 10px" }}
              title={
                atCap
                  ? `${maxRuns} tests are already running. Stop one to start another.`
                  : "Start another forward test, with its own settings"
              }
            >
              {starting ? "Cancel" : "+ New test"}
            </button>
          </span>
        </div>
      )}

      <div style={{ padding: 16 }}>
        {error && (
          <div className="auth-alert auth-alert-error" role="alert" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}

        {!running || starting ? (
          <>
            {starting && running && (
              <p style={{ margin: "0 0 14px", fontSize: 12, color: "var(--ink-2)" }}>
                Starting a second test. It runs alongside the others on the same
                live ticks, which is what makes the two comparable.
              </p>
            )}
            {atCap && (
              <div className="auth-alert auth-alert-info" style={{ marginBottom: 14 }}>
                {maxRuns} forward tests are already running. Stop one to start another.
              </div>
            )}
            {/* Tops aligned, not bottoms. Some fields carry a two-line hint
                and some carry none, so `flex-end` stepped every short field
                down to the tallest one's baseline and left the row looking
                broken. The action moved out of the row entirely -- wrapped
                between two number boxes is no place for the primary button. */}
            <div style={{ display: "flex", gap: "14px 16px", flexWrap: "wrap", alignItems: "flex-start" }}>
              <label style={FIELD}>
                Strategy
                <select
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value as "ladder" | "hic")}
                  className="auth-input"
                  style={{ minWidth: 190 }}
                >
                  <option value="ladder">Condor ladder</option>
                  <option value="hic">Hybrid iron condor</option>
                </select>
                <span
                  style={{ ...HINT, maxWidth: 190 }}
                >
                  {strategy === "ladder"
                    ? "A condor at every step. Earns when the market stalls."
                    : "Condors near the anchor, bought spreads beyond it. Earns when a move keeps going."}
                </span>
              </label>

              <label style={FIELD}>
                Name
                <input
                  className="auth-input"
                  value={runName}
                  onChange={(e) => setRunName(e.target.value)}
                  placeholder={strategy}
                  maxLength={40}
                  style={{ width: 170 }}
                />
                <span
                  style={{ ...HINT, maxWidth: 200 }}
                >
                  Name two runs differently to compare them on the same ticks.
                </span>
              </label>
              <label style={FIELD}>
                Expiry
                <select
                  value={cadence}
                  onChange={(e) => setCadence(e.target.value as "weekly" | "monthly")}
                  className="auth-input"
                  style={{ minWidth: 110 }}
                >
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                </select>
              </label>

              {strategy === "hic" ? (
                <>
                  <label style={FIELD}>
                    Core band
                    <select
                      value={bandSteps}
                      onChange={(e) => setBandSteps(Number(e.target.value))}
                      className="auth-input"
                      style={{ minWidth: 190 }}
                    >
                      <option value={0}>Anchor only</option>
                      <option value={1}>Anchor and one step either way</option>
                      <option value={2}>Anchor and two steps either way</option>
                    </select>
                    <span
                      style={{ ...HINT, maxWidth: 190 }}
                    >
                      How many levels open a full condor. Everything beyond
                      buys a spread.
                    </span>
                  </label>

                  <label style={FIELD}>
                    Spread strikes
                    <select
                      value={debitShift}
                      onChange={(e) => setDebitShift(Number(e.target.value) as 0 | 200)}
                      className="auth-input"
                      style={{ minWidth: 175 }}
                    >
                      <option value={0}>Reverse the condor&rsquo;s</option>
                      <option value={200}>Bought at the level</option>
                    </select>
                    <span
                      style={{ ...HINT, maxWidth: 175 }}
                    >
                      Buying at the level costs more and starts paying sooner.
                    </span>
                  </label>

                  <label style={FIELD}>
                    Put spreads
                    <input
                      type="number" min={0} max={40} value={putSpreads}
                      onChange={(e) => setPutSpreads(Number(e.target.value))}
                      className="auth-input" style={{ width: 90 }}
                    />
                  </label>

                  <label style={FIELD}>
                    Call spreads
                    <input
                      type="number" min={0} max={40} value={callSpreads}
                      onChange={(e) => setCallSpreads(Number(e.target.value))}
                      className="auth-input" style={{ width: 90 }}
                    />
                  </label>
                </>
              ) : (
              <label style={FIELD}>
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
                  style={{ minWidth: 140 }}
                >
                  <option value="down">Down-only (v1)</option>
                  <option value="both">Two-way / Both (v2)</option>
                  <option value="up">Up-only (v2)</option>
                </select>
              </label>
              )}

              {strategy === "hic" ? null : (
              <label style={FIELD}>
                Anchor mode
                <select
                  value={anchorMode}
                  onChange={(e) => setAnchorMode(e.target.value as "floor" | "nearest" | "round")}
                  className="auth-input"
                  style={{ minWidth: 110 }}
                >
                  <option value="floor">Floor</option>
                  <option value="nearest">Nearest</option>
                  <option value="round">Round</option>
                </select>
              </label>
              )}

              <label style={FIELD}>
                Lots
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={lots}
                  onChange={(e) => setLots(Math.max(1, Number(e.target.value)))}
                  className="auth-input"
                  style={{ width: 70 }}
                />
              </label>

              {strategy !== "hic" && direction !== "up" && (
                <label style={FIELD}>
                  Max down
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={maxDown}
                    onChange={(e) => setMaxDown(e.target.value === "" ? "" : Number(e.target.value))}
                    className="auth-input"
                    style={{ width: 80 }}
                  />
                </label>
              )}

              {strategy !== "hic" && direction !== "down" && (
                <label style={FIELD}>
                  Max up
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={maxUp}
                    onChange={(e) => setMaxUp(e.target.value === "" ? "" : Number(e.target.value))}
                    className="auth-input"
                    style={{ width: 80 }}
                  />
                </label>
              )}

            </div>

            <div
              style={{
                display: "flex",
                gap: 14,
                alignItems: "center",
                flexWrap: "wrap",
                marginTop: 18,
                paddingTop: 14,
                borderTop: "1px solid var(--border)",
              }}
            >
              <button
                onClick={start}
                disabled={busy !== null}
                className="auth-submit"
                style={{ marginTop: 0, minWidth: 150 }}
              >
                {busy === "starting" ? "Starting…" : "Start paper run"}
              </button>
              <p style={{ fontSize: 12, color: "var(--ink-muted)", margin: 0, lineHeight: 1.6, maxWidth: "62ch" }}>
                Runs against live Choice quotes and records simulated fills. This platform places
                no orders — there is no live-trading path to switch into.
              </p>
            </div>
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
                  {/* The span, not just the count. The series holds the last
                      900 ticks, which at a ten-second poll reaches back into
                      yesterday's session — so "900 ticks" gave no hint that
                      the left of the chart was a different day. */}
                  NIFTY live &middot; {ticks.length} tick{ticks.length === 1 ? "" : "s"}
                  {ticks.length > 1 && (
                    <>
                      {" "}&middot;{" "}
                      {istDay(ticks[0].t) === istDay(ticks[ticks.length - 1].t)
                        ? `${istDay(ticks[0].t)}, ${istClock(ticks[0].t)}–${istClock(ticks[ticks.length - 1].t)}`
                        : `${istDay(ticks[0].t)} ${istClock(ticks[0].t)} – ${istDay(ticks[ticks.length - 1].t)} ${istClock(ticks[ticks.length - 1].t)}`}
                    </>
                  )}
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
                // The fallback to `next_trigger` is for an engine old enough
                // not to send `next_down` -- which is also too old to run a
                // two-way ladder. On a two-way run `next_trigger` reports the
                // *up* rung once the down side is capped, so the tile labelled
                // "Next down" showed a level above the market.
                value={
                  state?.ladder.next_down != null
                    ? num(state.ladder.next_down)
                    : state?.ladder.direction !== "both" && state?.ladder.next_trigger != null
                      ? num(state.ladder.next_trigger)
                      : "—"
                }
                hint={shapeHint(state?.ladder.next_down_kind)}
              />
              {state?.ladder.direction === "both" && (
                <Metric
                  label="Next up"
                  value={state?.ladder.next_up != null ? num(state.ladder.next_up) : "—"}
                  hint={shapeHint(state?.ladder.next_up_kind)}
                />
              )}
              <Metric label="Self-hedged" value={pct(state?.netting.offset_ratio ?? 0)} />
              <Metric label="Last tick" value={session?.last_tick ? dateTime(session.last_tick).split(", ")[1] ?? "—" : "—"} />
            </div>

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {/* Arrow, not a bare reference: onClick hands the handler a
                  MouseEvent, which would arrive as the run name. */}
              <button onClick={() => stop()} disabled={busy !== null} className="btn-danger">
                {busy === "stopping" ? "Stopping…" : `Stop ${runs.length > 1 ? runKey : "run"}`}
              </button>

              <button onClick={refresh} className="btn-quiet">
                Refresh now
              </button>
            </div>

            {/* Caveats on the numbers above, in one place.
                These were three loose paragraphs between the metrics and the
                buttons -- easy to read as page furniture and skip, which is
                the opposite of what a caveat is for. Gathered into a bordered
                panel, each on its own row, so it is clear they qualify the
                figures rather than describe the product. */}
            {notes.length > 0 && (
              <div
                style={{
                  marginTop: 14, border: "1px solid var(--border)",
                  borderRadius: "var(--radius)", background: "var(--surface-3)",
                  overflow: "hidden",
                }}
              >
                <p
                  style={{
                    margin: 0, padding: "7px 12px", fontSize: 11,
                    fontWeight: 700, letterSpacing: ".04em", textTransform: "uppercase",
                    color: "var(--ink-muted)", borderBottom: "1px solid var(--border)",
                  }}
                >
                  About these numbers
                </p>
                {notes.map((note) => (
                  <p
                    key={note.key}
                    style={{
                      margin: 0, padding: "9px 12px", fontSize: 11.5, lineHeight: 1.6,
                      color: "var(--ink-2)", display: "flex", gap: 9,
                      borderTop: "1px solid var(--border)",
                    }}
                  >
                    <span
                      aria-hidden="true"
                      style={{
                        flex: "0 0 3px", borderRadius: 2, alignSelf: "stretch",
                        background: note.tone === "warn" ? "var(--warn)" : "var(--border-strong)",
                      }}
                    />
                    <span>{note.body}</span>
                  </p>
                ))}
              </div>
            )}

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
                        <th>Unit</th><th>Level</th><th>Expiry</th>
                        <th style={{ textAlign: "right" }}>Credit</th>
                        <th style={{ textAlign: "right" }}>Live P&amp;L</th>
                        <th style={{ textAlign: "right" }}>Max loss</th>
                        <th>Legs</th>
                      </tr>
                    </thead>
                    <tbody>
                      {openPositions.map((p) => (
                        <tr key={p.index}>
                          {/* Without this the only way to tell a four-leg
                              condor from a two-leg spread was to count the
                              badges in the Legs column. */}
                          <td><UnitKindBadge kind={p.kind} k={p.k} /></td>
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

function Metric({
  label, value, tone, hint,
}: { label: string; value: string; tone?: "pos" | "neg"; hint?: string }) {
  const color = tone === "pos" ? "var(--pos)" : tone === "neg" ? "var(--neg)" : "var(--ink)";
  return (
    <div style={{ background: "var(--surface-3)", borderRadius: 8, padding: "9px 11px" }}>
      <div style={{ fontSize: 10.5, color: "var(--ink-muted)", fontWeight: 600 }}>{label}</div>
      <div className="tnum" style={{ fontSize: 17, fontWeight: 700, color, marginTop: 2 }}>
        {value}
      </div>
      {hint && (
        <div style={{ fontSize: 10, color: "var(--ink-muted)", marginTop: 1 }}>{hint}</div>
      )}
    </div>
  );
}
