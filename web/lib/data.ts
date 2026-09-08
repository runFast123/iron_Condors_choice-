import seed from "@/data/seed.json";
import type { Dataset } from "./types";
import { engine, engineConfigured } from "./engine";
import { getSessionToken } from "./session";

/**
 * The signed-in user's own backtest result.
 *
 * Every page reads through here. It asks the engine for *this user's* dataset
 * rather than a file baked in at build time, because a build-time bundle is
 * both stale and shared -- two users would see the same numbers, which is
 * exactly wrong once each brings their own Choice account.
 *
 * The static bundle survives only as the shape-correct empty state for when
 * the engine cannot be reached, so pages render an explanation instead of
 * throwing.
 */
export async function getDataset(): Promise<Dataset> {
  const fallback = seed as unknown as Dataset;
  if (!engineConfigured()) return fallback;

  const token = await getSessionToken();
  if (!token) return fallback;

  try {
    const data = await engine.backtestDataset(token);
    return data as unknown as Dataset;
  } catch {
    // A dead engine must not take the page down with it.
    return fallback;
  }
}
