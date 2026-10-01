import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });
  const id = new URL(request.url).searchParams.get("id");
  if (!id) return NextResponse.json({ error: "Which job?" }, { status: 400 });
  try {
    return NextResponse.json(await engine.playgroundJob(token, id));
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
