import { NextResponse } from "next/server";
import { engine, EngineError, engineConfigured } from "@/lib/engine";
import { SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Exchange Choice credentials for a session.
 *
 * The credentials pass straight through to the engine and are never stored,
 * logged, or written to the response. What comes back to the browser is an
 * httpOnly cookie holding an opaque token, plus a redacted user summary.
 */
export async function POST(request: Request) {
  if (!engineConfigured()) {
    return NextResponse.json(
      {
        error:
          "The engine is not configured. Set ENGINE_URL to the machine whose static IP is declared against your Choice API key.",
      },
      { status: 503 },
    );
  }

  let body: { vendor_id?: string; api_key?: string; mobile?: string };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }

  const vendorId = (body.vendor_id ?? "").trim();
  const apiKey = (body.api_key ?? "").trim();
  const mobile = (body.mobile ?? "").trim();

  if (!vendorId || !apiKey || !mobile) {
    return NextResponse.json(
      { error: "Vendor ID, API key and mobile number are all required." },
      { status: 400 },
    );
  }

  try {
    const { token, user } = await engine.login(vendorId, apiKey, mobile);
    const res = NextResponse.json({ user });
    res.cookies.set(SESSION_COOKIE, token, sessionCookieOptions());
    return res;
  } catch (err) {
    const e = err as EngineError;
    // Pass the broker's own message through: "static IP rejected" and "bad
    // credentials" need very different fixes, and collapsing them into a
    // generic failure would leave the user guessing.
    return NextResponse.json({ error: e.message }, { status: e.status ?? 502 });
  }
}
