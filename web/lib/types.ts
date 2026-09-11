/** Choice is the only external data source. "modeled" is Black-76, not a vendor. */
export type PriceSource = "choice" | "modeled";

export interface CoverageFailure {
  token: string;
  resolution: string;
  range: string;
  status: string;
  error: string;
}

export interface Provenance {
  spot_source: string;
  vol_source: string;
  premium_source: string;
  expiry_source?: string;
  verified: boolean;
  /** True when Choice has not been connected and there is nothing to show. */
  awaiting_connection?: boolean;
  note: string;
  resolution: string;
  option_resolution?: string;
  generated_at: string;
  provider: { real_quotes: number; modeled_quotes: number; total_quotes: number; real_fraction: number };
  bars: number;
  range: [string, string];
  legs_requested?: number;
  legs_with_choice_data?: number;
  coverage?: Record<string, number>;
  failures?: CoverageFailure[];
  lot_size?: number;
}

export interface Params {
  step: number;
  short_offset: number;
  long_offset: number;
  lots: number;
  lot_size: number;
  qty: number;
  max_condors: number;
  fill_gaps: boolean;
  take_profit_pct: number | null;
  stop_loss_mult: number | null;
  direction?: "down" | "up" | "both";
  max_down?: number | null;
  max_up?: number | null;
  anchor_mode: string;
  roll_to_next_expiry?: boolean;
  label: string;
}

export interface Attribution {
  down_pnl: number;
  up_pnl: number;
  down_condors: number;
  up_condors: number;
  down_credit: number;
  up_credit: number;
}

export interface Metrics {
  net_pnl: number; gross_pnl: number; total_costs: number; total_credit: number;
  condors: number; wins: number; losses: number; win_rate: number;
  profit_factor: number; expectancy: number; avg_win: number; avg_loss: number;
  best: number; worst: number;
  max_drawdown: number; max_drawdown_pct: number;
  sharpe: number; sortino: number; calmar: number; cagr: number;
  max_concurrent: number; avg_days_held: number; capital_at_risk: number;
  real_price_fraction: number; modeled_quotes: number;
}

export interface Leg {
  right: "CE" | "PE";
  side: "BUY" | "SELL";
  strike: number;
  qty: number;
  signed_qty: number;
  entry_price: number;
  exit_price: number | null;
  source: PriceSource;
}

export interface Condor {
  index: number;
  level: number;
  side?: "anchor" | "down" | "up";
  entry_time: string;
  expiry: string;
  status: "OPEN" | "CLOSED_TARGET" | "CLOSED_STOP" | "EXPIRED";
  exit_time: string | null;
  exit_reason: string | null;
  credit: number;
  entry_costs: number;
  exit_costs: number;
  max_profit: number;
  max_loss: number;
  breakevens: [number, number];
  pnl: number;
  modeled: boolean;
  legs: Leg[];
}

export interface EquityPoint {
  ts: string; equity: number; drawdown: number; open_condors: number; spot: number | null;
}

export interface MatrixRow {
  expiry: string;
  right: "CE" | "PE";
  strike: number;
  net_qty: number;
  gross_long: number;
  gross_short: number;
  is_flat: boolean;
  by_condor: Record<string, number>;
}

export interface Netting {
  strikes_touched: number;
  strikes_fully_offset: number;
  gross_qty: number;
  net_qty: number;
  offset_qty: number;
  offset_ratio: number;
}

export interface Trigger {
  level: number;
  time: string;
  spot: number;
  reason: string;
  side?: "anchor" | "down" | "up";
}

export interface Roll {
  when: string;
  from: string;
  to: string;
}

export interface Dataset {
  provenance: Provenance;
  params: Params;
  metrics: Metrics;
  attribution?: Attribution;
  netting: Netting;
  campaigns?: number;
  rolls?: Roll[];
  condors: Condor[];
  equity: EquityPoint[];
  payoff: { spot: number; pnl: number }[];
  strike_matrix: MatrixRow[];
  triggers: Trigger[];
  warnings: string[];
  skipped: [string, number, string][];
}
