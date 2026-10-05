import { NextResponse } from "next/server";
import { engine, EngineError } from "@/lib/engine";
import { getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });

  let body: { run_id?: string; run_ids?: string[] } = {};
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body." }, { status: 400 });
  }

  const runIds: string[] = [];
  if (body.run_id) runIds.push(body.run_id);
  if (Array.isArray(body.run_ids)) runIds.push(...body.run_ids);

  const unique = Array.from(new Set(runIds.filter(Boolean)));
  if (unique.length === 0) {
    return NextResponse.json({ error: "No run_id or run_ids provided." }, { status: 400 });
  }

  try {
    if (unique.length === 1 && body.run_id && !body.run_ids) {
      const res = await engine.backtestDelete(token, unique[0]);
      return NextResponse.json(res);
    }
    const res = await engine.backtestDeleteMany(token, unique);
    return NextResponse.json(res);
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}

export async function DELETE(request: Request) {
  const token = await getSessionToken();
  if (!token) return NextResponse.json({ error: "Not signed in." }, { status: 401 });

  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id") || searchParams.get("id");

  if (!runId) {
    return NextResponse.json({ error: "Missing run_id query parameter." }, { status: 400 });
  }

  try {
    const res = await engine.backtestDelete(token, runId);
    return NextResponse.json(res);
  } catch (err) {
    const e = err as EngineError;
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
