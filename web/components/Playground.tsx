"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { LiveCampaign } from "@/lib/live";
import type { PlanResult, PlaygroundJob, PlaygroundRun, ReplayResult } from "@/lib/playground";
import { dateTime, inr, num, pct, shortDate } from "@/lib/format";
import { useCountUp } from "@/lib/useCountUp";
import { CampaignStrip } from "@/components/CampaignStrip";
import { PayoffChart } from "@/components/charts/PayoffChart";
import { SettingsEditor, type Edits } from "@/components/playground/SettingsEditor";
import { CompareLines, OutcomeHistogram, sharedBins } from "@/components/playground/Charts";

/** Follow a playground job until it finishes. */
function useJob<R>(id: string | null): PlaygroundJob<R> | null {
  const [job, setJob] = useState<PlaygroundJob<R> | null>(null);
  useEffect(() => {
    setJob(null);
    if (!id) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const res = await fetch(`/api/playground/job?id=${encodeURIComponent(id)}`, { cache: "no-store" });
        const body = await res.json();
        if (!live) return;
        if (!res.ok) {
          setJob({ job_id: id, kind: "replay", status: "error", progress: 0, message: "", error: body.error ?? "Lost the job.", params: {} });
          return;
        }
        setJob(body.job as PlaygroundJob<R>);
        if (body.job.status === "running") timer = setTimeout(poll, 1200);
      } catch (err) {
        if (live) timer = setTimeout(poll, 2500);
        void err;
      }
    };
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [id]);
  return job;
}

export function Playground() {
  const [runs, setRuns] = useState<PlaygroundRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runKey, setRunKey] = useState<string | null>(null);
  const [mode, setMode] = useState<"back" | "ahead">("back");

  useEffect(() => {
    fetch("/api/playground/campaigns", { cache: "no-store" })
      .then(async (res) => {
        const body = await res.json();
        if (!res.ok) throw new Error(body.error ?? `The engine did not answer (${res.status}).`);
        const list = body.runs as PlaygroundRun[];
        setRuns(list);
        setRunKey((k) => k ?? list.find((r) => r.campaigns.some((c) => c.status !== "active"))?.run_key ?? list[0]?.run_key ?? null);
      })
      .catch((err) => setError((err as Error).message));
  }, []);

  const run = runs?.find((r) => r.run_key === runKey) ?? null;

  if (error) return <div className="auth-alert auth-alert-error">{error}</div>;
  if (!runs) return <div className="skeleton" style={{ height: 320, borderRadius: 12 }} aria-label="Loading" />;
  if (!runs.length) {
    return <div className="card" style={{ padding: 18, fontSize: 13 }}>Start a forward test first; the playground works on its campaigns.</div>;
  }

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="card" style={{ padding: 14, display: "grid", gap: 12 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <span style={{ fontSize: 11.5, color: "var(--ink-muted)", fontWeight: 600, marginRight: 4 }}>Test</span>
          {runs.map((r) => (
            <button key={r.run_key} type="button" onClick={() => setRunKey(r.run_key)}
                    className={`pg-chip${r.run_key === runKey ? " is-on" : ""}`}>
              {r.label}
              <span style={{ color: "var(--ink-muted)", fontWeight: 500 }}>
                {" "}· {r.campaigns.length} campaign{r.campaigns.length === 1 ? "" : "s"}{r.running ? "" : " · stopped"}
              </span>
            </button>
          ))}
        </div>
        <div className="pg-tabs" role="tablist">
          <button role="tab" aria-selected={mode === "back"} className={mode === "back" ? "is-on" : ""} onClick={() => setMode("back")}>
            <strong>Look back</strong>
            <span>Replay a settled campaign with other settings</span>
          </button>
          <button role="tab" aria-selected={mode === "ahead"} className={mode === "ahead" ? "is-on" : ""} onClick={() => setMode("ahead")}>
            <strong>Look ahead</strong>
            <span>Simulate the campaign trading now</span>
          </button>
        </div>
      </div>

      {run && (mode === "back"
        ? <LookBack key={`back-${run.run_key}`} run={run} />
        : <LookAhead key={`ahead-${run.run_key}`} run={run} />)}
    </div>
  );
}

// ------------------------------------------------------------- look back

function LookBack({ run }: { run: PlaygroundRun }) {
  const settled = run.campaigns.filter((c) => c.status !== "active");
  // Open on the last campaign held to settlement; one closed early is still a click away.
  const [expiry, setExpiry] = useState((settled.find((c) => c.status === "settled") ?? settled[0])?.expiry ?? "");
  const [edits, setEdits] = useState<Edits>({});
  const [ids, setIds] = useState<{ baseline: string; edited: string } | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const baseline = useJob<ReplayResult>(ids?.baseline ?? null);
  const edited = useJob<ReplayResult>(ids?.edited ?? null);
  const campaign = settled.find((c) => c.expiry === expiry) ?? null;

  if (!run.settings) return <Note>This test&rsquo;s settings could not be read, so it cannot be replayed.</Note>;
  if (!settled.length) {
    return <Note>No campaign of {run.label} has settled yet. Once one has, replay it here with other settings.</Note>;
  }

  const replay = async () => {
    setBusy(true);
    setStartError(null);
    try {
      const res = await fetch("/api/playground/replay", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run: run.run_key, expiry, settings: edits }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error ?? `Could not start (${res.status}).`);
      setIds({ baseline: body.baseline.job_id, edited: body.edited.job_id });
    } catch (err) {
      setStartError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }} className="fade-up">
      <div className="card" style={{ padding: 16 }}>
        <SectionTitle n={1} title="Pick a settled campaign" />
        <CampaignStrip campaigns={settled} selected={expiry} onSelect={(e) => { setExpiry(e); setIds(null); }} />
        {campaign && (
          <p style={{ margin: 0, fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.6 }}>
            What happened: <strong>{campaign.positions} positions</strong> from {shortDate(campaign.started_at)}, anchored at{" "}
            {num(campaign.anchor)}, {campaign.status === "settled" ? "settled" : "ended"}
            {campaign.settlement_spot != null ? ` against an official close of ${num(Math.round(campaign.settlement_spot))}` : ""} for{" "}
            <PnlText v={campaign.pnl} />.
            {campaign.status === "closed" && (
              <> It was closed early, on {shortDate(campaign.ended_at ?? campaign.started_at)}; the replay holds it to
                its {shortDate(campaign.expiry)} expiry, so expect it to differ from the live figure.</>
            )}
          </p>
        )}
      </div>

      <div className="card" style={{ padding: 16 }}>
        <SectionTitle n={2} title="Change what you would have done" hint="Edits are highlighted. The replay uses the same month's real prices, fills and charges." />
        <SettingsEditor base={run.settings} edits={edits} onChange={setEdits} mode="replay" />
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14, flexWrap: "wrap" }}>
          <button className="auth-submit" style={{ marginTop: 0, minWidth: 170 }} disabled={busy || !campaign}
                  onClick={() => void replay()}>
            {busy ? "Starting…" : Object.keys(edits).length ? "Replay with these changes" : "Replay as traded"}
          </button>
          {Object.keys(edits).length > 0 && (
            <button className="btn-quiet" onClick={() => setEdits({})}>Reset to the run&rsquo;s settings</button>
          )}
          {startError && <span style={{ color: "var(--neg)", fontSize: 12.5 }}>{startError}</span>}
        </div>
      </div>

      {ids && campaign && (
        <ReplayResults campaign={campaign} baseline={baseline} edited={edited} edits={edits} />
      )}
    </div>
  );
}

function ReplayResults({
  campaign, baseline, edited, edits,
}: {
  campaign: LiveCampaign;
  baseline: PlaygroundJob<ReplayResult> | null;
  edited: PlaygroundJob<ReplayResult> | null;
  edits: Edits;
}) {
  const a = baseline?.status === "done" ? baseline.result ?? null : null;
  const b = edited?.status === "done" ? edited.result ?? null : null;
  const failed = [baseline, edited].find((j) => j?.status === "error");
  const changedAnything = Object.keys(edits).length > 0;
  const deltaTarget = b && a ? b.metrics.total_pnl - a.metrics.total_pnl : null;
  const delta = useCountUp(deltaTarget);

  if (failed) return <div className="auth-alert auth-alert-error">{failed.error}</div>;
  if (!a || !b) {
    const p = Math.min(baseline?.progress ?? 0, edited?.progress ?? 0);
    return (
      <div className="card" style={{ padding: 16 }}>
        <div style={{ fontSize: 12.5, color: "var(--ink-2)", marginBottom: 8 }}>
          Replaying {shortDate(campaign.expiry)} … {edited?.message || baseline?.message || "starting"}
        </div>
        <div className="pg-progress"><span style={{ width: `${Math.round(100 * Math.max(0.04, p))}%` }} /></div>
      </div>
    );
  }

  const rows: { label: string; live: number | null; a: number; b: number; better: "high" | "low"; money?: boolean }[] = [
    { label: "P&L", live: campaign.pnl, a: a.metrics.total_pnl, b: b.metrics.total_pnl, better: "high", money: true },
    { label: "Positions opened", live: campaign.positions, a: a.positions.length, b: b.positions.length, better: "low" },
    { label: "Worst position", live: campaign.worst, a: a.metrics.worst, b: b.metrics.worst, better: "high", money: true },
    { label: "Deepest drawdown", live: null, a: a.metrics.max_drawdown, b: b.metrics.max_drawdown, better: "high", money: true },
    { label: "Premium collected", live: campaign.credit, a: a.metrics.total_credit, b: b.metrics.total_credit, better: "high", money: true },
    { label: "Charges", live: null, a: a.metrics.total_costs, b: b.metrics.total_costs, better: "low", money: true },
  ];
  const levelsA = new Map(a.positions.map((p) => [p.level, p]));
  const levelsB = new Map(b.positions.map((p) => [p.level, p]));
  const levels = [...new Set([...levelsA.keys(), ...levelsB.keys()])].sort((x, y) => y - x);

  return (
    <div className="card fade-up" style={{ padding: 16, display: "grid", gap: 16 }}>
      <SectionTitle n={3} title="What would have happened" />
      {changedAnything ? (
        <div className={`pg-verdict ${deltaTarget! >= 0 ? "is-pos" : "is-neg"}`}>
          <div>
            <div className="pg-verdict-label">Your changes, on the same month</div>
            <div className="pg-verdict-value tnum">{delta == null ? "--" : inr(Math.round(delta), { sign: true })}</div>
          </div>
          <p>
            {inr(Math.round(a.metrics.total_pnl), { sign: true })} as traded becomes{" "}
            <strong>{inr(Math.round(b.metrics.total_pnl), { sign: true })}</strong>,
            with {b.positions.length} position{b.positions.length === 1 ? "" : "s"} instead of {a.positions.length}.
            The difference is the changes&rsquo; alone: both replays use the same prices.
          </p>
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: 12.5, color: "var(--ink-2)" }}>
          Replayed as traded. Change a setting above to see what it would have done.
        </p>
      )}

      <div className="scroll-x">
        <table>
          <thead>
            <tr><th></th><th style={{ textAlign: "right" }}>What happened (live)</th>
              <th style={{ textAlign: "right" }}>Replayed as traded</th>
              <th style={{ textAlign: "right" }}>{changedAnything ? "With your changes" : "Replay"}</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const improved = r.better === "high" ? r.b > r.a : r.b < r.a;
              const fmt = (v: number | null) => (v == null ? "—" : r.money ? inr(Math.round(v), { sign: r.label !== "Premium collected" && r.label !== "Charges" }) : num(v));
              return (
                <tr key={r.label}>
                  <td style={{ fontWeight: 600 }}>{r.label}</td>
                  <td className="tnum" style={{ textAlign: "right", color: "var(--ink-2)" }}>{fmt(r.live)}</td>
                  <td className="tnum" style={{ textAlign: "right" }}>{fmt(r.a)}</td>
                  <td className="tnum" style={{ textAlign: "right", fontWeight: 700,
                    color: !changedAnything || r.a === r.b ? "var(--ink)" : improved ? "var(--pos)" : "var(--neg)" }}>
                    {fmt(r.b)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p style={{ margin: 0, fontSize: 11.5, color: "var(--ink-muted)", lineHeight: 1.6 }}>
        &ldquo;Replayed as traded&rdquo; is this month through the backtest with the run&rsquo;s own settings. Its gap
        from what happened live ({campaign.pnl == null ? "—" : inr(Math.round(a.metrics.total_pnl - campaign.pnl), { sign: true })}) is
        the replay&rsquo;s own error: fills at modelled spreads and minute-close timing. Real prices:{" "}
        {a.real_fraction != null ? pct(a.real_fraction) : "—"}.
      </p>

      <div>
        <div className="pg-legend">
          <span><i style={{ background: "var(--ink-muted)" }} /> as traded</span>
          <span><i style={{ background: "var(--brand)" }} /> {changedAnything ? "with your changes" : "replay"}</span>
        </div>
        <CompareLines a={a.equity} b={b.equity} />
      </div>

      <div className="scroll-x">
        <table>
          <thead>
            <tr><th>Level</th><th>Opened (as traded)</th><th style={{ textAlign: "right" }}>P&amp;L as traded</th>
              <th style={{ textAlign: "right" }}>{changedAnything ? "P&L with changes" : "P&L"}</th></tr>
          </thead>
          <tbody>
            {levels.map((lv, i) => {
              const pa = levelsA.get(lv), pb = levelsB.get(lv);
              return (
                <tr key={lv} className="row-in" style={{ animationDelay: `${Math.min(i, 14) * 25}ms` }}>
                  <td className="tnum" style={{ fontWeight: 700 }}>{num(lv)}</td>
                  <td style={{ color: "var(--ink-2)" }}>{pa ? dateTime(pa.entry_time) : (pb ? dateTime(pb.entry_time) : "")}</td>
                  <td className="tnum" style={{ textAlign: "right" }}>{pa ? <PnlText v={pa.pnl} /> : <span style={{ color: "var(--ink-muted)" }}>not opened</span>}</td>
                  <td className="tnum" style={{ textAlign: "right", fontWeight: 700 }}>
                    {pb ? <PnlText v={pb.pnl} /> : <span style={{ color: "var(--ink-muted)", fontWeight: 400 }}>not opened</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ------------------------------------------------------------- look ahead

function LookAhead({ run }: { run: PlaygroundRun }) {
  const active = run.campaigns.find((c) => c.status === "active") ?? null;
  const [edits, setEdits] = useState<Edits>({});
  const [vix, setVix] = useState<string>(run.vix?.value != null ? String(run.vix.value) : "");
  const [fresh, setFresh] = useState(false);
  const [ids, setIds] = useState<{ current: string; plan: string | null } | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const current = useJob<PlanResult>(ids?.current ?? null);
  const plan = useJob<PlanResult>(ids?.plan ?? null);

  if (!run.running) return <Note>{run.label} is not running. Plan a running test; a stopped one has no campaign trading now.</Note>;
  if (!run.settings) return <Note>This test&rsquo;s settings could not be read.</Note>;

  const start = async () => {
    setBusy(true);
    setStartError(null);
    const body = (settings: Edits) => JSON.stringify({
      run: run.run_key, settings, fresh, vix: vix.trim() ? Number(vix) : undefined, paths: 1000,
    });
    try {
      const post = async (settings: Edits) => {
        const res = await fetch("/api/playground/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: body(settings) });
        const out = await res.json();
        if (!res.ok) throw new Error(out.error ?? `Could not start (${res.status}).`);
        return out.job.job_id as string;
      };
      const currentId = await post({});
      const planId = Object.keys(edits).length ? await post(edits) : null;
      setIds({ current: currentId, plan: planId });
    } catch (err) {
      setStartError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }} className="fade-up">
      <div className="card" style={{ padding: 16 }}>
        <SectionTitle n={1} title="Where the campaign stands"
                      hint="The simulation starts from here: the same anchor and levels already opened, unless you start fresh." />
        {active ? (
          <p style={{ margin: "0 0 10px", fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.6 }}>
            <strong>{shortDate(active.expiry)} campaign</strong>, anchored at {num(active.anchor)} on {shortDate(active.started_at)}:{" "}
            {active.positions} position{active.positions === 1 ? "" : "s"}, {active.open} open, P&amp;L so far <PnlText v={active.pnl} />.
            NIFTY {run.market?.spot != null ? num(run.market.spot) : "—"}.
          </p>
        ) : (
          <p style={{ margin: "0 0 10px", fontSize: 12.5, color: "var(--ink-2)" }}>No campaign is trading yet; the plan starts a fresh one.</p>
        )}
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label className="pg-field">
            <span className="pg-field-label">India VIX to assume</span>
            <input className="auth-input" type="number" step="0.1" value={vix} onChange={(e) => setVix(e.target.value)} placeholder="live" />
            <span className="pg-field-hint">today&rsquo;s reading by default</span>
          </label>
          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>
            <input type="checkbox" checked={fresh} onChange={(e) => setFresh(e.target.checked)} />
            Start a fresh campaign from today&rsquo;s price instead
          </label>
        </div>
      </div>

      <div className="card" style={{ padding: 16 }}>
        <SectionTitle n={2} title="Your plan for the month" hint="Leave as is to see where the current settings can go; change any to compare." />
        <SettingsEditor base={run.settings} edits={edits} onChange={setEdits} mode="plan" />
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 14, flexWrap: "wrap" }}>
          <button className="auth-submit" style={{ marginTop: 0, minWidth: 170 }} disabled={busy} onClick={() => void start()}>
            {busy ? "Starting…" : Object.keys(edits).length ? "Simulate both plans" : "Simulate the month"}
          </button>
          {Object.keys(edits).length > 0 && <button className="btn-quiet" onClick={() => setEdits({})}>Reset</button>}
          {startError && <span style={{ color: "var(--neg)", fontSize: 12.5 }}>{startError}</span>}
        </div>
      </div>

      {ids && <PlanResults current={current} plan={ids.plan ? plan : null} />}
    </div>
  );
}

function PlanResults({ current, plan }: { current: PlaygroundJob<PlanResult> | null; plan: PlaygroundJob<PlanResult> | null }) {
  const jobs = [current, plan].filter(Boolean) as PlaygroundJob<PlanResult>[];
  const failed = jobs.find((j) => j.status === "error");
  const ready = jobs.length > 0 && jobs.every((j) => j.status === "done" && j.result);
  const [show, setShow] = useState<"current" | "plan">("plan");
  if (failed) return <div className="auth-alert auth-alert-error">{failed.error}</div>;
  if (!ready) {
    const p = Math.min(...jobs.map((j) => j.progress), 1);
    return (
      <div className="card" style={{ padding: 16 }}>
        <div style={{ fontSize: 12.5, color: "var(--ink-2)", marginBottom: 8 }}>{jobs[0]?.message || "Starting"}</div>
        <div className="pg-progress"><span style={{ width: `${Math.round(100 * Math.max(0.04, p))}%` }} /></div>
      </div>
    );
  }
  const a = current!.result!;
  const b = plan?.result ?? null;
  const chosen = b && show === "plan" ? b : a;
  // One set of edges for both plans, so the bars compare like for like.
  const paired = Boolean(a.paths_pnl?.length && (!b || b.paths_pnl?.length === a.paths_pnl.length));
  const [binsA, binsB] = paired ? sharedBins(b ? [a.paths_pnl, b.paths_pnl] : [a.paths_pnl]) : [a.histogram, b?.histogram];
  const limit = a.inputs.daily_loss_limit;

  return (
    <div className="card fade-up" style={{ padding: 16, display: "grid", gap: 16 }}>
      <SectionTitle n={3} title="Where the month can go"
                    hint={`${num(a.inputs.paths)} simulated paths from now to the ${shortDate(a.inputs.expiry)} expiry (${a.inputs.sessions_left} session${a.inputs.sessions_left === 1 ? "" : "s"}), built from ${num(a.inputs.history_sessions)} real NIFTY sessions (${shortDate(a.inputs.history_from)} – ${shortDate(a.inputs.history_to)}), starting at India VIX ${a.inputs.vix}. P&L is the campaign's total at expiry, the positions already open included.`} />
      {a.inputs.entries_paused_by_vix && (
        <div className="auth-alert auth-alert-info">At VIX {a.inputs.vix} the run&rsquo;s VIX rule holds new positions back, so the simulation opens none.</div>
      )}
      {a.inputs.halted_today && (
        <div className="auth-alert auth-alert-info">The daily loss limit has already stopped new positions for today; the simulation opens none before tomorrow.</div>
      )}
      <div className={`pg-plan-grid${b ? " has-two" : ""}`}>
        <PlanCard title={b ? "Current settings" : "With the current settings"} r={a} />
        {b && <PlanCard title="Your plan" r={b} highlight compareTo={a} />}
      </div>

      <div>
        <div className="pg-legend">
          <span><i style={{ background: "var(--pos)" }} /> profit</span>
          <span><i style={{ background: "var(--neg)" }} /> loss</span>
          {b && <span><i style={{ background: "transparent", borderTop: "2px dashed var(--ink)" }} /> current settings</span>}
        </div>
        <OutcomeHistogram primary={(b ? binsB : binsA) ?? a.histogram} secondary={b ? binsA : null} />
      </div>

      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>If NIFTY moves steadily to a level by expiry</div>
          {b && (
            <div className="pg-seg">
              <button className={show === "current" ? "is-on" : ""} onClick={() => setShow("current")}>Current</button>
              <button className={show === "plan" ? "is-on" : ""} onClick={() => setShow("plan")}>Your plan</button>
            </div>
          )}
        </div>
        <PayoffChart data={chosen.ends_at} spot={chosen.inputs.spot} height={260} />
        <p style={{ margin: "6px 0 0", fontSize: 11.5, color: "var(--ink-muted)" }}>
          One straight path to each level, with the ladder opening every rung on the way, to see each outcome on its own.
          Real paths wander; the distribution above is what to plan on.
        </p>
      </div>

      <details className="pg-method">
        <summary>How this is worked out</summary>
        <p>
          <strong>Paths.</strong> Filtered historical simulation, the way risk desks project a path-dependent book. Each
          simulated session is a real NIFTY session from the last two years, drawn at random &mdash; its 15-minute bars,
          overnight gap included &mdash; together with that day&rsquo;s change in India VIX. The path&rsquo;s VIX moves by
          the drawn change, so a sell-off lifts it as a real one did, and each session is rescaled from the VIX it
          originally opened at to the path&rsquo;s VIX at that point. NIFTY&rsquo;s fat tails, gaps and volatility
          clustering are kept, which a normal model leaves out. The history&rsquo;s own trend
          ({a.inputs.drift_removed_pct >= 0 ? "+" : ""}{a.inputs.drift_removed_pct}% over the sessions left) is removed:
          no drift is assumed. Realised moves run below implied ones, as they do in the market; that gap is the
          premium seller&rsquo;s edge and is kept.
        </p>
        <p>
          <strong>Trading.</strong> On each path the run&rsquo;s own ladder decides bar by bar: the same anchor and
          opened levels, caps, gap-fills, entry filters, the VIX rule at the path&rsquo;s VIX, and the daily loss limit
          {limit ? ` of ${inr(limit)}` : " (none set)"}, measured as the live run measures it, from the previous
          session&rsquo;s close. Nothing opens on expiry day. New positions are priced with the engine&rsquo;s option
          model at that bar&rsquo;s spot, time to expiry and the path&rsquo;s VIX, filled half the bid-ask spread against
          you, and charged the STT and exchange charges in force. Everything settles against the average of expiry
          day&rsquo;s final half hour &mdash; the exchange&rsquo;s method for NIFTY&rsquo;s closing price &mdash; with STT
          on exercised longs.
        </p>
        <p>
          <strong>Reading it.</strong> Expected P&amp;L carries its 95% sampling margin. &ldquo;Bad month&rdquo; is the 5th
          percentile (95% value-at-risk); &ldquo;average of the worst 5%&rdquo; is the expected shortfall beyond it. Both
          plans run on the same paths, so their difference is measured path by path and is the settings&rsquo;, not
          luck&rsquo;s. Not simulated: take-profit and stop-loss. The loss limit is checked when a level is reached, so a
          dip that recovered before the next level is not seen. The straight-line chart holds VIX where it is now.
        </p>
      </details>
    </div>
  );
}

/** Two plans on the same paths, compared path by path: the mean difference,
 *  its 95% margin, and how often each plan came out ahead. */
function pairedDiff(r: PlanResult, base: PlanResult) {
  const a = base.paths_pnl, b = r.paths_pnl;
  if (!a?.length || a.length !== b?.length) return null;
  const d = b.map((v, i) => v - a[i]);
  const n = d.length;
  const mean = d.reduce((s, v) => s + v, 0) / n;
  const sd = Math.sqrt(d.reduce((s, v) => s + (v - mean) ** 2, 0) / Math.max(1, n - 1));
  // The same comparison in the months that hurt most under the current settings.
  const worst = a.map((v, i) => [v, i] as const).sort((x, y) => x[0] - y[0]).slice(0, Math.max(1, Math.floor(n / 20)));
  const inTail = worst.reduce((s, [, i]) => s + d[i], 0) / worst.length;
  return {
    mean, margin: 1.96 * sd / Math.sqrt(n), inTail,
    better: d.filter((v) => v > 0.5).length / n, worse: d.filter((v) => v < -0.5).length / n,
  };
}

function PlanCard({ title, r, highlight, compareTo }: { title: string; r: PlanResult; highlight?: boolean; compareTo?: PlanResult }) {
  const mean = useCountUp(r.pnl.mean);
  const paired = compareTo ? pairedDiff(r, compareTo) : null;
  const diff = paired ? paired.mean : compareTo ? r.pnl.mean - compareTo.pnl.mean : null;
  return (
    <div className={`pg-plan${highlight ? " is-plan" : ""}`}>
      <div className="pg-plan-title">{title}</div>
      <div className="pg-plan-mean tnum" style={{ color: r.pnl.mean >= 0 ? "var(--pos)" : "var(--neg)" }}>
        {mean == null ? "--" : inr(Math.round(mean), { sign: true })}
      </div>
      <div className="pg-plan-sub">
        expected P&amp;L, ± {inr(Math.round(1.96 * r.pnl.se))}
        {diff != null && (
          <> · <span style={{ color: diff >= 0 ? "var(--pos)" : "var(--neg)", fontWeight: 700 }}>{inr(Math.round(diff), { sign: true })}</span>
            {paired ? ` ± ${inr(Math.round(paired.margin))}` : ""} vs current</>
        )}
      </div>
      {paired && (
        <div className="pg-plan-sub">
          Better in {pct(paired.better)} of the same months, worse in {pct(paired.worse)}
          {Math.abs(paired.mean) <= paired.margin ? " — the expected difference is within the noise" : ""}.
          {" "}In the current settings&rsquo; worst 5% of months it does{" "}
          <span style={{ color: paired.inTail >= 0 ? "var(--pos)" : "var(--neg)", fontWeight: 700, whiteSpace: "nowrap" }}>
            {inr(Math.round(Math.abs(paired.inTail)))} {paired.inTail >= 0 ? "better" : "worse"}
          </span>{" "}on average.
        </div>
      )}
      <div className="pg-plan-stats">
        <Stat label="Chance of a loss" value={pct(r.pnl.p_loss)} tone={r.pnl.p_loss > 0.5 ? "neg" : undefined} />
        <Stat label="Typical month" value={inr(Math.round(r.pnl.median), { sign: true })} />
        <Stat label="Bad month (1 in 20)" value={inr(Math.round(r.pnl.p5), { sign: true })} tone="neg" />
        <Stat label="Average of the worst 5%" value={inr(Math.round(r.pnl.es5), { sign: true })} tone="neg" />
        <Stat label="Good month (1 in 20)" value={inr(Math.round(r.pnl.p95), { sign: true })} tone="pos" />
        <Stat label="Positions it opens" value={r.rungs.mean.toFixed(1)} hint={`up to ${r.rungs.max}`} />
      </div>
      <div className="pg-plan-sub" style={{ marginTop: 8 }}>
        NIFTY at expiry: {num(Math.round(r.nifty_at_expiry.p5))} – {num(Math.round(r.nifty_at_expiry.p95))} in 9 of 10 paths
        (middle {num(Math.round(r.nifty_at_expiry.p50))}).
        {r.vix_at_expiry && <> India VIX by then: {r.vix_at_expiry.p5.toFixed(1)} – {r.vix_at_expiry.p95.toFixed(1)}.</>}
        {r.loss_limit && r.loss_limit.share_of_paths > 0 && (
          <> The daily loss limit held back a level in {pct(r.loss_limit.share_of_paths)} of paths.</>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------- helpers

function Stat({ label, value, tone, hint }: { label: string; value: string; tone?: "pos" | "neg"; hint?: string }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, color: "var(--ink-muted)", fontWeight: 600 }}>{label}</div>
      <div className="tnum" style={{ fontSize: 15, fontWeight: 700, color: tone === "pos" ? "var(--pos)" : tone === "neg" ? "var(--neg)" : "var(--ink)" }}>{value}</div>
      {hint && <div style={{ fontSize: 10, color: "var(--ink-muted)" }}>{hint}</div>}
    </div>
  );
}

function PnlText({ v }: { v: number | null }) {
  if (v == null) return <span style={{ color: "var(--ink-muted)" }}>--</span>;
  return <span style={{ color: v > 0 ? "var(--pos)" : v < 0 ? "var(--neg)" : "var(--ink)", fontWeight: 700, whiteSpace: "nowrap" }}>{inr(Math.round(v), { sign: true })}</span>;
}

function SectionTitle({ n, title, hint }: { n: number; title: string; hint?: string }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", gap: 9, alignItems: "center" }}>
        <span className="pg-step">{n}</span>
        <span style={{ fontSize: 14, fontWeight: 700 }}>{title}</span>
      </div>
      {hint && <div style={{ fontSize: 12, color: "var(--ink-muted)", marginTop: 4, marginLeft: 31, lineHeight: 1.5 }}>{hint}</div>}
    </div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return <div className="card fade-up" style={{ padding: 18, fontSize: 13, color: "var(--ink-2)" }}>{children}</div>;
}

