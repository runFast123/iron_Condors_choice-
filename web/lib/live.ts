
import type { UnitKind } from "./types";

export interface LiveSession {
  /** Paper is the only mode. There is no order-placing path to switch into. */
  mode: "paper";
  status: "running" | "stopped" | "disconnected";
  stopped_reason: string | null;
  started_at: string | null;
  last_tick: string | null;
  market_open: boolean;
  connected: boolean;
  expiry: string | null;
  /** Why the last tick produced no quotes, if it didn't. */
  last_error?: string | null;
  /** Which MultipleTouchline payload shape Choice accepted. */
  quote_format?: string | null;
}

export interface LiveEvent {
  ts: string;
  level: "info" | "trade" | "warn" | "error";
  message: string;
  detail: Record<string, unknown>;
}

export interface LiveFill {
  ts: string;
  condor_index: number;
  condor_level: number;
  expiry: string;
  right: "CE" | "PE";
  side: "BUY" | "SELL";
  strike: number;
  qty: number;
  price: number;
  source: string;
  mode: string;
  token: number | null;
  action: "OPEN" | "CLOSE";
  /**
   * When the price actually traded, when that differs from `ts`.
   *
   * `ts` is the engine's clock at the moment it recorded the fill. A leg
   * filled off a candle close traded when that candle closed, which can be
   * minutes earlier. Null means the two are the same.
   */
  market_ts?: string | null;
  /** Fair value at the time, what crossing the spread cost, and whether that
   *  spread came from a real book or had to be modelled. */
  reference: number | null;
  slippage: number;
  spread_modelled: boolean;
}

export interface LivePositionLeg {
  right: "CE" | "PE";
  side: "BUY" | "SELL";
  strike: number;
  qty: number;
  entry_price: number;
  exit_price: number | null;
  token: number | null;
  source: string;
}

export interface LivePosition {
  index: number;
  level: number;
  /** Absent on runs started before HIC, where every unit was a condor. */
  kind?: UnitKind;
  k?: number | null;
  max_profit?: number;
  breakevens?: number[];
  side?: "anchor" | "down" | "up";
  expiry: string;
  entry_time: string;
  status: string;
  credit: number;
  max_loss: number;
  /**
   * Live mark while open, realised once closed, and null when an open condor
   * has never been marked -- which is not the same as zero.
   */
  pnl: number | null;
  is_open: boolean;
  exit_reason: string | null;
  legs: LivePositionLeg[];
}

/** How much of a run was priced on a real order book rather than a modelled spread. */
export interface FillQuality {
  legs_on_real_depth: number;
  legs_on_modelled_spread: number;
  real_depth_fraction: number;
  total_slippage: number;
}

export interface LiveState {
  session: LiveSession;
  fill_quality?: FillQuality;
  /** Latest mid per option token, as of `session.last_tick`. */
  marks?: Record<string, number>;
  market: {
    spot: number | null;
    ts: string | null;
    /** True when spot came from the last traded candle rather than the live
     *  book — MultipleTouchline does not serve index tokens. */
    stale?: boolean;
    /** When that spot printed, as against when the engine read it. */
    as_of?: string | null;
  };
  ladder: {
    anchor: number | null;
    last_level: number | null;
    next_trigger: number | null;
    distance: number | null;
    fired: number[];
    step: number;
    direction?: "down" | "up" | "both";
    high_level?: number | null;
    next_down?: number | null;
    next_up?: number | null;
    distance_up?: number | null;
    down_count?: number;
    up_count?: number;
  };
  pnl: {
    realised: number; unrealised: number; total: number;
    open_condors: number; total_condors: number;
    down_pnl?: number;
    up_pnl?: number;
    /** Open condors the total does NOT include, because they have no mark yet. */
    unmarked_condors?: number;
  };
  netting: {
    strikes_touched: number; strikes_fully_offset: number; gross_qty: number;
    net_qty: number; offset_qty: number; offset_ratio: number;
  };
  positions: LivePosition[];
  fills: LiveFill[];
  events: LiveEvent[];
  net_positions: {
    expiry: string; right: "CE" | "PE"; strike: number;
    net_qty: number; gross_long: number; gross_short: number; is_flat: boolean;
  }[];
  generated_at: string;
}

import { engine, engineConfigured, type ForwardRunSummary } from "./engine";
import { getSessionToken } from "./session";

/**
 * Every forward test the signed-in user is driving.
 *
 * Returns an empty list rather than throwing when the engine is unreachable or
 * nobody is signed in: a page that cannot list runs should still render its
 * own content, and the state fetch beside it reports the failure once.
 */
export async function getForwardRuns(): Promise<ForwardRunSummary[]> {
  if (!engineConfigured()) return [];
  const token = await getSessionToken();
  if (!token) return [];
  try {
    const body = await engine.forwardRuns(token);
    return body.runs ?? [];
  } catch {
    return [];
  }
}

/**
 * The signed-in user's own forward run.
 *
 * Returns null when no run has been started, which the UI renders as controls
 * to start one rather than as an error. A dead engine is reported separately
 * so the page can say which of the two is wrong.
 */
export async function getLiveState(
  run?: string,
): Promise<{
  state: LiveState | null;
  /** Every run this user has, so a page need not ask a second time. */
  runs: ForwardRunSummary[];
  /** Which run the engine actually served. Authoritative over any guess. */
  activeRun: string;
  maxRuns: number;
  engineError: string | null;
}> {
  const empty = { state: null, runs: [], activeRun: run ?? "ladder", maxRuns: 5 };
  if (!engineConfigured()) {
    return { ...empty, engineError: "ENGINE_URL is not configured." };
  }
  const token = await getSessionToken();
  if (!token) return { ...empty, engineError: null };

  try {
    // One call, not two. The engine answers from India behind a tunnel while
    // this renders on Vercel, so asking separately for the roll-call and the
    // state cost two ocean round trips before the page could draw anything.
    const body = await engine.forwardState(token, run);
    return {
      state: (body.state as LiveState | null) ?? null,
      // Array-checked, not just null-checked. An engine older than this build
      // answers `runs` as a dict keyed by run, which is truthy -- so `?? []`
      // would hand the page an object where it expects a list. The two deploy
      // separately, so that window is real.
      runs: Array.isArray(body.runs) ? body.runs : [],
      activeRun: body.run_key ?? run ?? "ladder",
      maxRuns: body.max_runs ?? 5,
      engineError: null,
    };
  } catch (err) {
    return { ...empty, engineError: (err as Error).message };
  }
}
