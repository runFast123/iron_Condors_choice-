/**
 * Where an option premium came from. "choice" is a real Choice candle;
 * "backup" a real candle from the backtest's backup source, for a contract
 * Choice has no history for (backtests only); "modeled" is Black-76, not a
 * source at all. The dashboard never names the backup source.
 */
export type PriceSource = "choice" | "backup" | "modeled";

/** What a backtest took from its backup source because Choice had nothing. */
export interface BackupUse {
  provider: "backup";
  /** Whether the engine has a backup source configured at all. */
  configured?: boolean;
  used: boolean;
  nifty_days: string[];
  /** Intraday VIX bars, for the entry rule and the model. */
  vix_bar_days: string[];
  /** Daily VIX closes, for the model's fallback. */
  vix_close_days: string[];
  /** Official closes an expiry settled against. */
  settlement_days?: string[];
  option_legs_asked?: number;
  option_legs_found?: number;
  option_legs_used?: number;
  /** What neither Choice nor the backup could cover. */
  notes: string[];
}

/** What the VIX rule did over a backtest. */
export interface VixGate {
  limit: number;
  bars: number;
  paused_bars: number;
  paused_fraction: number;
  spells: number;
  levels_passed: number;
  bars_without_vix: number;
  /** Bars per VIX source: "choice", "backup", "choice:close", "none", ... */
  readings: Record<string, number>;
  resolution: string;
}

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
  /** `real_quotes` counts every real traded price, Choice's and the backup's;
   *  the split is absent on results from before the backup existed. */
  provider: {
    real_quotes: number; modeled_quotes: number; total_quotes: number; real_fraction: number;
    choice_quotes?: number; backup_quotes?: number; backup_fraction?: number;
  };
  bars: number;
  range: [string, string];
  legs_requested?: number;
  legs_with_choice_data?: number;
  /**
   * Why a leg ended up modelled, split by cause. The three are different
   * problems: `legs_empty` is Choice resolving the contract and returning no
   * candles at all, which is what it does for a settled option and cannot be
   * fixed from here; `legs_unresolved` is a contract that could not be found;
   * the rest priced from real candles.
   */
  legs_total?: number;
  legs_real?: number;
  legs_empty?: number;
  legs_unresolved?: number;
  empty_expiries?: string[];
  /** Bars came back for these legs, but none near a moment they were needed. */
  legs_unused?: number;
  unused_legs?: {
    expiry: string; strike: number; right: string; bars: number;
    first_bar: string | null; last_bar: string | null; first_needed: string;
  }[];
  coverage?: Record<string, number>;
  failures?: CoverageFailure[];
  lot_size?: number;
  /** Absent on runs from before the backup source existed. */
  backup?: BackupUse;
  /** Legs priced from the backup source at least once. */
  legs_backup?: number;
  /** How many expiries settled against NIFTY's official close, and which
   *  fell back to their last bar. */
  settlement?: { official_close: number; last_bar: string[] };
  /** Null or absent when the run had no VIX limit. */
  vix_gate?: VixGate | null;
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
  /** Ladder entry filters. Null when off. */
  min_entry_dte?: number | null;
  min_credit_ratio?: number | null;
  /** No new positions while India VIX is above this. Null (or absent, on
   *  results from before the rule) when off. Both strategies. */
  max_entry_vix?: number | null;
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
  /**
   * Gross wins over gross losses, or null when that is not a ratio: every
   * closed trade won (nothing to divide by), or nothing has closed at all.
   * The engine emits infinity for the first, which becomes null on the wire.
   */
  profit_factor: number | null; expectancy: number; avg_win: number; avg_loss: number;
  best: number; worst: number;
  max_drawdown: number; max_drawdown_pct: number;
  /** Null when the run was too short, or too flat, to compute them.
   *  Zero is a real answer to a different question and must not stand in. */
  sharpe: number | null; sortino: number | null;
  calmar: number | null; cagr: number | null;
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

/** What shape a position is. Drives the badge, not the arithmetic. */
export type UnitKind =
  | "condor"
  | "put_debit_spread"
  | "call_debit_spread"
  | "put_credit_spread"
  | "call_credit_spread";

export interface Condor {
  /** Absent on datasets written before HIC, where everything was a condor. */
  kind?: UnitKind;
  /** Steps from the anchor. Null for a ladder rung, which has no band. */
  k?: number | null;
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
  /**
   * Where the structure breaks even at expiry.
   *
   * A list, not a pair. A condor has two; a vertical spread has one, and none
   * at all when its payoff never crosses zero. The fixed tuple this replaced
   * indexed [1] on a spread and rendered NaN.
   */
  breakevens: number[];
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
  /**
   * Which book the payoff curve describes.
   *
   * One campaign, not all of them summed. A rolling backtest re-anchors at
   * each expiry, so its positions belong to books that were never held at the
   * same time; stacking them made the trough deeper the longer the run was.
   * Absent on datasets written before that was fixed.
   */
  payoff_campaign?: {
    expiry: string | null;
    campaigns: number;
    units: number;
    max_loss: number;
    credit: number;
    debit: number;
  };
  strike_matrix: MatrixRow[];
  triggers: Trigger[];
  warnings: string[];
  skipped: [string, number, string][];
}
