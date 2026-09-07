import seed from "@/data/seed.json";
import type { Dataset } from "./types";

/**
 * The dashboard's single data source.
 *
 * Today this is a JSON bundle produced by `python -m engine.tools.seed`, which
 * is what lets the deployed site render without Choice credentials (which are
 * bound to a static IP and can never be exercised from Vercel). When the
 * engine is wired to Postgres, swap this one function for a DB query — every
 * page reads through here and nothing else touches the shape.
 */
export function getDataset(): Dataset {
  return seed as unknown as Dataset;
}
