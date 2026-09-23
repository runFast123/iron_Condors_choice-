import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { SESSION_COOKIE } from "@/lib/session";

/** Routes reachable without a session. Everything else requires login. */
const PUBLIC = ["/login", "/api/auth/login", "/api/auth/logout"];

/**
 * The one hostname the app is served from.
 *
 * Cookies belong to a hostname, and Vercel answers on many: every deploy gets
 * its own `web-<hash>-….vercel.app` URL, and the tunnel script redeploys on
 * every engine restart and prints that URL to its log. A user who opened one
 * of those arrived with no cookie and was sent to /login -- and signing in
 * there replaced their session on the engine, which signed out the tab they
 * had open on the real hostname. On 23 Sep a user was bounced this way from
 * `web-jsp0x4x2w-…`, the URL the 09:08 redeploy had just produced.
 *
 * Only `.vercel.app` hosts are redirected, so local development and a future
 * custom domain are left alone.
 */
const CANONICAL_HOST =
  process.env.CANONICAL_HOST || process.env.VERCEL_PROJECT_PRODUCTION_URL || "";

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  const host = request.headers.get("host") ?? "";
  if (CANONICAL_HOST && host !== CANONICAL_HOST && host.endsWith(".vercel.app")) {
    const url = request.nextUrl.clone();
    url.protocol = "https:";
    url.host = CANONICAL_HOST;
    url.port = "";
    // 308, not 307: a POST (a sign-in, a start button) keeps its method and
    // body across the hop instead of turning into a GET.
    return NextResponse.redirect(url, 308);
  }

  if (PUBLIC.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return NextResponse.next();
  }

  const token = request.cookies.get(SESSION_COOKIE)?.value;
  if (!token) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    // Send the user back where they were heading, once signed in.
    if (pathname !== "/") url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Everything except Next internals and static assets.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"],
};
