
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
  expiry: string;
  entry_time: string;
  status: string;
  credit: number;
  max_loss: number;
  pnl: number | null;
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
  market: {
    spot: number | null;
    ts: string | null;
    /** True when spot came from the last traded candle rather than the live
     *  book — MultipleTouchline does not serve index tokens. */
    stale?: boolean;
  };
  ladder: {
    anchor: number | null;
    last_level: number | null;
    next_trigger: number | null;
    distance: number | null;
    fired: number[];
    step: number;
  };
  pnl: {
    realised: number; unrealised: number; total: number;
    open_condors: number; total_condors: number;
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

import { engine, engineConfigured } from "./engine";
import { getSessionToken } from "./session";

/**
 * The signed-in user's own forward run.
 *
 * Returns null when no run has been started, which the UI renders as controls
 * to start one rather than as an error. A dead engine is reported separately
 * so the page can say which of the two is wrong.
 */
export async function getLiveState(): Promise<{ state: LiveState | null; engineError: string | null }> {
  if (!engineConfigured()) {
    return { state: null, engineError: "ENGINE_URL is not configured." };
  }
  const token = await getSessionToken();
  if (!token) return { state: null, engineError: null };

  try {
    const body = await engine.forwardState(token);
    return { state: (body.state as LiveState | null) ?? null, engineError: null };
  } catch (err) {
    return { state: null, engineError: (err as Error).message };
  }
}
