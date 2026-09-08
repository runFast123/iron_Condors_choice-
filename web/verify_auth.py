"""End-to-end browser tests for the login system.

Runs against a live Next server plus a live engine, so the flow under test is
the real one: middleware redirect, server route, httpOnly cookie, engine
session, and sign-out.

    python web/verify_auth.py --base http://127.0.0.1:3010
"""

from __future__ import annotations

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from playwright.sync_api import sync_playwright

GOOD, BAD, WRONG_IP = "good-key", "bad-key", "wrong-ip"
ALICE, BOB = "9000000001", "9000000002"


def sign_in(page, base: str, mobile: str, key: str, vendor: str = "V1") -> None:
    page.goto(f"{base}/login", wait_until="networkidle")
    page.fill("#vendor_id", vendor)
    page.fill("#mobile", mobile)
    page.fill("#api_key", key)
    page.click("button[type=submit]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3010")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    results: list[tuple[bool, str, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((ok, name, detail))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)

        # --------------------------------------------------------- gate
        ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.goto(f"{base}/ladder", wait_until="networkidle")
        record("gate: protected route redirects to /login", "/login" in page.url, page.url)
        record("gate: original destination preserved", "next=%2Fladder" in page.url or "next=/ladder" in page.url, page.url)

        # ------------------------------------------------------- engine IP
        # Only meaningful when the engine reports one (ENGINE_PUBLIC_IP set).
        page.goto(f"{base}/login", wait_until="networkidle")
        page.wait_for_timeout(400)
        has_panel = page.locator(".ip-panel").count() == 1
        if has_panel:
            shown = page.locator(".ip-value").inner_text().strip()
            record("ip: address shown before any attempt", bool(shown), shown)
            record("ip: panel starts calm", page.locator(".ip-panel.ip-panel-alert").count() == 0, "not alert")
            page.click(".ip-copy")
            page.wait_for_timeout(400)
            try:
                copied = page.evaluate("() => navigator.clipboard.readText()")
                record("ip: copy button copies the address", copied.strip() == shown, copied)
            except Exception:
                # Clipboard read blocked by browser policy; fall back to the
                # button's own confirmation, which is what a user actually sees.
                label = page.locator(".ip-copy").inner_text().strip()
                record("ip: copy button confirms", label.lower() == "copied", label)

        # -------------------------------------------------- bad credentials
        sign_in(page, base, ALICE, BAD)
        page.wait_for_selector(".auth-alert-error", timeout=15_000)
        err = page.inner_text(".auth-alert-error")
        record("reject: bad API key shows an error", "401" in err or "Invalid" in err, err[:70])
        record("reject: stays on the login page", "/login" in page.url, page.url)
        record("reject: no session cookie issued",
               not any(c["name"] == "ic_session" for c in ctx.cookies()), "no cookie")

        # ------------------------------------------------ static-IP rejection
        sign_in(page, base, ALICE, WRONG_IP)
        page.wait_for_selector(".auth-alert-error", timeout=15_000)
        ip_err = page.inner_text(".auth-alert-error")
        record("reject: static-IP failure is explained, not generic",
               "IP" in ip_err, ip_err[:80].replace("\n", " "))

        if has_panel:
            record("ip: rejection escalates the IP panel",
                   page.locator(".ip-panel.ip-panel-alert").count() == 1, "alert state")

        # A wrong key must not blame the address: different problem, different fix.
        sign_in(page, base, ALICE, BAD)
        page.wait_for_selector(".auth-alert-error", timeout=15_000)
        page.wait_for_timeout(300)
        if has_panel:
            record("ip: a bad key does NOT blame the IP",
                   page.locator(".ip-panel.ip-panel-alert").count() == 0, "stays calm")

        # ------------------------------------------------------- happy path
        sign_in(page, base, ALICE, GOOD)
        page.wait_for_url(lambda u: "/login" not in u, timeout=20_000)
        page.wait_for_load_state("networkidle")
        record("login: lands on the app", "/login" not in page.url, page.url)
        body = page.inner_text("body")
        record("login: signed-in user is shown", "Test Trader 01" in body, "sidebar identity")
        record("login: mobile is masked, never raw", ALICE not in body and "**01" in body, "masked")

        cookie = next((c for c in ctx.cookies() if c["name"] == "ic_session"), None)
        record("cookie: session cookie set", cookie is not None, cookie["name"] if cookie else "-")
        record("cookie: httpOnly", bool(cookie and cookie.get("httpOnly")), str(cookie and cookie.get("httpOnly")))
        record("cookie: sameSite set", bool(cookie and cookie.get("sameSite")), str(cookie and cookie.get("sameSite")))
        js_cookie = page.evaluate("() => document.cookie")
        record("cookie: unreadable from JavaScript", "ic_session" not in js_cookie, repr(js_cookie)[:40])
        record("cookie: token absent from page HTML",
               (cookie["value"] not in page.content()) if cookie else False, "not leaked")

        # ------------------------------------------------ navigation while in
        page.goto(f"{base}/forward", wait_until="networkidle")
        record("session: protected pages now load", "Forward Test" in page.inner_text("h1"), page.url)

        # --------------------------------------------------------- isolation
        ctx2 = browser.new_context()
        page2 = ctx2.new_page()
        sign_in(page2, base, BOB, GOOD)
        page2.wait_for_url(lambda u: "/login" not in u, timeout=20_000)
        page2.wait_for_load_state("networkidle")
        body2 = page2.inner_text("body")
        record("isolation: second user sees their own identity", "Test Trader 02" in body2, "user 2")

        page.reload(wait_until="networkidle")
        record("isolation: first user unaffected by second login",
               "Test Trader 01" in page.inner_text("body"), "user 1 intact")

        c1 = next(c for c in ctx.cookies() if c["name"] == "ic_session")
        c2 = next(c for c in ctx2.cookies() if c["name"] == "ic_session")
        record("isolation: distinct session tokens", c1["value"] != c2["value"], "differ")

        # ---------------------------------------------------------- sign out
        page.click(".signout")
        page.wait_for_url("**/login**", timeout=20_000)
        record("signout: returns to login", "/login" in page.url, page.url)
        record("signout: cookie cleared",
               not any(c["name"] == "ic_session" and c["value"] for c in ctx.cookies()), "cleared")

        page.goto(f"{base}/ladder", wait_until="networkidle")
        record("signout: protected route blocked again", "/login" in page.url, page.url)

        page2.reload(wait_until="networkidle")
        record("signout: other user still signed in",
               "Test Trader 02" in page2.inner_text("body"), "user 2 intact")

        browser.close()

    for ok, name, detail in results:
        print(f"{'[PASS]' if ok else '[FAIL]'} {name:<52} {detail}")
    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} auth checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
