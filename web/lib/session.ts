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

/**
 * How long a sign-in lasts, in days. Mirrors SESSION_DAYS in
 * engine/auth/sessions.py: if the cookie outlived the engine's session the
 * page would bounce to /login, and if it died first a still-valid session
 * would be thrown away.
 */
export const SESSION_DAYS = 7;

/** Seconds until the end of the sign-in's last day, IST. */
export function secondsUntilSessionEnds(): number {
  return secondsUntilEndOfDayIST() + (SESSION_DAYS - 1) * 86_400;
}

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    // Was the end of the day, matching Choice's day-scoped session. The engine
    // renews that session on its own now, so ending the sign-in at midnight
    // only forced a login every morning.
    maxAge: secondsUntilSessionEnds(),
  };
}
