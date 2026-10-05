import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });

  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id");

  try {
    const data = await engine.backtestDataset(token, runId);
    return NextResponse.json(data);
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
