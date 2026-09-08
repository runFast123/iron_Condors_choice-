
export interface LiveSession {
  mode: "paper" | "live";
  armed: boolean;
  status: "running" | "stopped" | "disconnected";
  stopped_reason: string | null;
  started_at: string | null;
  last_tick: string | null;
  market_open: boolean;
  connected: boolean;
  expiry: string | null;
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
  order_id: string | null;
  action: "OPEN" | "CLOSE";
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

export interface LiveState {
  session: LiveSession;
  market: { spot: number | null; ts: string | null };
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
    open_rungs: number; total_rungs: number;
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
