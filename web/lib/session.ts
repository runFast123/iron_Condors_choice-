import { cookies } from "next/headers";

export const SESSION_COOKIE = "ic_session";

/**
 * The engine's opaque session token, kept in an httpOnly cookie.
 *
 * httpOnly means no script on the page can read it, so an XSS bug cannot
 * exfiltrate a live trading session. The token itself carries no user data —
 * it is a random string the engine resolves — so there is nothing to decode
 * or tamper with offline either.
 */
export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

/** Seconds until end of day IST, matching Choice's day-scoped session. */
export function secondsUntilEndOfDayIST(): number {
  const now = new Date();
  const ist = new Date(now.getTime() + (330 + now.getTimezoneOffset()) * 60_000);
  const end = new Date(ist);
  end.setHours(23, 59, 0, 0);
  return Math.max(60, Math.floor((end.getTime() - ist.getTime()) / 1000));
}

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    maxAge: secondsUntilEndOfDayIST(),
  };
}
