import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });

  let body: Record<string, unknown> = {};
  try {
    body = await request.json();
  } catch {
    /* defaults are fine */
  }

  try {
    return NextResponse.json(await engine.backtestRun(token, body));
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
