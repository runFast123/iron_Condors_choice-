import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** NIFTY over one of a run's past campaigns, for its chart. */
export async function GET(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  const params = new URL(request.url).searchParams;
  const run = params.get("run") ?? undefined;
  const expiry = params.get("expiry");
  if (!expiry) return NextResponse.json({ error: "Which campaign?" }, { status: 400 });

  try {
    return NextResponse.json(await engine.forwardCampaign(token, expiry, run));
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
