import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Tick history for the live chart.
 *
 * Served from the engine's database rather than kept in the browser, so a
 * reload redraws the whole session instead of starting from an empty series.
 */
export async function GET() {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });

  try {
    return NextResponse.json(await engine.forwardTicks(token));
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
