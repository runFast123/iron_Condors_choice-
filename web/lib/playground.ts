import type { LiveCampaign, RunSettings } from "@/lib/live";

/** A run as the playground lists it. */
export interface PlaygroundRun {
  run_key: string;
  label: string;
  strategy: string;
  running: boolean;
  settings: RunSettings | null;
  campaigns: LiveCampaign[];
  market?: { spot: number | null } | null;
  vix?: { value: number | null } | null;
}

export interface PlaygroundJob<R = unknown> {
  job_id: string;
  kind: "replay" | "plan";
  status: "running" | "done" | "error";
  progress: number;
  message: string;
  error: string | null;
  params: Record<string, unknown>;
  result?: R | null;
}

export interface ReplayPosition {
  level: number;
  side: string | null;
  entry_time: string;
  credit: number;
  max_loss: number;
  pnl: number;
  status: string;
  exit_reason: string | null;
}

export interface ReplayResult {
  settings: RunSettings;
  metrics: {
    net_pnl: number; total_pnl: number; open_positions: number; open_pnl: number;
    condors: number; wins: number; losses: number; max_drawdown: number;
    capital_at_risk: number; total_credit: number; total_costs: number; best: number; worst: number;
  };
  positions: ReplayPosition[];
  equity: { ts: string; equity: number; spot: number | null }[];
  real_fraction: number | null;
  warnings: string[];
  skipped: number;
}

export interface PlanStats {
  mean: number; median: number; std: number; se: number; p_loss: number;
  p5: number; p25: number; p75: number; p95: number; worst: number; best: number; es5: number;
}

export interface Quantiles { p5: number; p25: number; p50: number; p75: number; p95: number }

export interface PlanResult {
  settings: RunSettings;
  inputs: {
    spot: number; vix: number; expiry: string; sessions_left: number; paths: number; seed: number;
    realised_so_far: number; open_positions: number; fresh_campaign: boolean;
    entries_paused_by_vix: boolean; history_sessions: number; history_from: string; history_to: string;
    daily_loss_limit: number | null; day_pnl: number; halted_today: boolean;
    /** The history's own trend over the sessions left, removed from every path. */
    drift_removed_pct: number;
  };
  pnl: PlanStats;
  /** Each path's outcome, in path order: two plans share their paths. */
  paths_pnl: number[];
  histogram: { lo: number; hi: number; count: number }[];
  rungs: { mean: number; max: number };
  loss_limit: { share_of_paths: number; rungs_held_back: number };
  nifty_at_expiry: Quantiles;
  vix_at_expiry: Quantiles;
  ends_at: { spot: number; pnl: number }[];
}
