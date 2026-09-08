import { NextResponse } from "next/server";
import { engine } from "@/lib/engine";
import { SESSION_COOKIE, getSessionToken } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST() {
  const token = await getSessionToken();
  if (token) {
    // Best effort: end the Choice session too, so a logout here does not
    // leave a broker session alive on the engine.
    try {
      await engine.logout(token);
    } catch {
      /* the cookie is cleared regardless */
    }
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.set(SESSION_COOKIE, "", { path: "/", maxAge: 0 });
  return res;
}
