import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Start a stopped run again: the same run, positions and all. */
export async function POST(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  const run = new URL(request.url).searchParams.get("run") ?? undefined;

  try {
    return NextResponse.json(await engine.forwardResume(token, run));
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
