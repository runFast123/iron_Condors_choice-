import seed from "@/data/seed.json";
import type { BacktestHistoryRun, Dataset } from "./types";
import { engine, engineConfigured } from "./engine";
import { getSessionToken } from "./session";

/**
 * The signed-in user's own backtest result, or a specific past run by runId.
 */
export async function getDataset(runId?: string | null): Promise<Dataset> {
  const fallback = seed as unknown as Dataset;
  if (!engineConfigured()) return fallback;

  const token = await getSessionToken();
  if (!token) return fallback;

  try {
    const data = await engine.backtestDataset(token, runId);
    return data as unknown as Dataset;
  } catch {
    // A dead engine must not take the page down with it.
    return fallback;
  }
}

/**
 * Historical backtest runs for the signed-in user.
 */
export async function getBacktestHistory(limit = 30): Promise<BacktestHistoryRun[]> {
  if (!engineConfigured()) return [];

  const token = await getSessionToken();
  if (!token) return [];

  try {
    const res = await engine.backtestHistory(token, limit);
    return res.runs || [];
  } catch {
    return [];
  }
}
