"""Browser verification of the dashboard, driven by Playwright.

Loads every page in real Chrome, in both light and dark themes, and checks that
each one actually rendered its data rather than an empty shell. Collects console
errors, failed network requests, and screenshots.

    python web/verify.py                      # against the local static build
    python web/verify.py --base <url>         # against a deployment

Uses the Chrome already installed on this machine (channel="chrome") because
the Playwright browser CDN is blocked on this network.
"""

from __future__ import annotations

import argparse
import http.server
import os
import re
import socketserver
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parent
OUT = WEB / "out"

# Text that must appear regardless of whether Choice is connected.
PAGES: dict[str, list[str]] = {
    "/": ["Overview", "iron condor"],
    "/ladder/": ["Strike Ladder Matrix", "Why the offsetting happens"],
    "/payoff/": ["Payoff at Expiry"],
    "/chart/": ["Price & Trigger Levels"],
    "/trades/": ["Trades"],
    "/data/": ["Data Health", "Sources", "Option premiums"],
    "/about/": ["The Strategy", "Why the legs cancel", "Where the risk is"],
    "/forward/": ["Forward Test", "Safety", "Paper by default"],
    "/forward/log/": ["Activity Log", "Trade history", "Run log"],
}

# Additional text required only once Choice data is present.
WHEN_CONNECTED: dict[str, list[str]] = {
    "/": ["Net P&L", "Win rate", "Ladder rungs"],
    "/chart/": ["Trigger log", "NIFTY with ladder levels"],
    "/payoff/": ["Combined expiry payoff", "Per-rung structure"],
}

# Text that must appear while Choice is NOT connected, so the empty state can
# never silently become a blank page.
WHEN_AWAITING = ["Awaiting Choice FinX connection", "engine.tools.doctor"]

# Console noise that is not a defect.
IGNORE_CONSOLE = (
    "Download the React DevTools",
    "favicon",
    "net::ERR_ABORTED",
)


@dataclass
class PageResult:
    path: str
    ok: bool = True
    status: int = 0
    missing: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    text_len: int = 0
    notes: list[str] = field(default_factory=list)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the static export without logging every request to stdout."""

    def log_message(self, *args) -> None:  # noqa: D102
        pass


def serve(directory: Path, port: int = 8899) -> socketserver.TCPServer:
    handler = lambda *a, **k: _QuietHandler(*a, directory=str(directory), **k)  # noqa: E731
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def awaiting_state(page) -> bool:
    """True when the page is showing the 'no Choice connection' placeholder."""
    return "Awaiting Choice FinX connection" in page.inner_text("body")


def check(base: str, shots: Path, headed: bool = False) -> list[PageResult]:
    results: list[PageResult] = []
    shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed)
        for theme in ("dark", "light"):
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                color_scheme=theme,
                device_scale_factor=1,
            )
            page = context.new_page()
            # The site defaults to light regardless of OS preference, so dark
            # must be selected the way a user would.
            page.add_init_script(
                f"try{{localStorage.setItem('ic-theme','{theme}')}}catch(e){{}}"
            )

            for path, expected in PAGES.items():
                result = PageResult(path=f"{path} [{theme}]")
                console: list[str] = []
                failed: list[str] = []

                page.on(
                    "console",
                    lambda m, c=console: c.append(f"{m.type}: {m.text}")
                    if m.type == "error" and not any(s in m.text for s in IGNORE_CONSOLE)
                    else None,
                )
                page.on(
                    "requestfailed",
                    lambda r, f=failed: f.append(f"{r.url} ({r.failure})")
                    if not any(s in (r.url or "") for s in IGNORE_CONSOLE)
                    else None,
                )

                try:
                    response = page.goto(base.rstrip("/") + path, wait_until="networkidle", timeout=45_000)
                    result.status = response.status if response else 0
                except Exception as exc:  # noqa: BLE001
                    result.ok = False
                    result.notes.append(f"navigation failed: {exc}")
                    results.append(result)
                    continue

                page.wait_for_timeout(900)  # let charts mount

                text = page.inner_text("body")
                result.text_len = len(text)
                normalised = re.sub(r"\s+", " ", text)
                awaiting = "Awaiting Choice FinX connection" in normalised
                required = list(expected)
                if awaiting:
                    if path not in ("/about/",):
                        required += WHEN_AWAITING
                else:
                    required += WHEN_CONNECTED.get(path, [])
                result.missing = [e for e in required if e not in normalised]
                result.notes.append("awaiting" if awaiting else "connected")

                result.console_errors = console[:]
                result.failed_requests = failed[:]

                # Nothing should overflow the viewport horizontally.
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                if overflow > 2:
                    result.notes.append(f"horizontal overflow: {overflow}px")

                # The body must be painted, not transparent.
                bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
                if bg in ("rgba(0, 0, 0, 0)", "transparent"):
                    result.notes.append("body background is transparent")
                result.notes.append(f"bg={bg}")

                # Charts should have rendered actual geometry.
                svg_paths = page.evaluate(
                    "() => Array.from(document.querySelectorAll('svg path')).filter(p => (p.getAttribute('d')||'').length > 40).length"
                )
                canvases = page.evaluate("() => document.querySelectorAll('canvas').length")
                if path in ("/", "/payoff/") and not awaiting_state(page) and svg_paths == 0:
                    result.ok = False
                    result.notes.append("expected a chart path, found none")
                if path == "/chart/" and not awaiting_state(page) and canvases == 0:
                    result.ok = False
                    result.notes.append("lightweight-charts canvas did not render")
                result.notes.append(f"svgPaths={svg_paths} canvases={canvases}")

                name = (path.strip("/") or "overview").replace("/", "_")
                page.screenshot(path=str(shots / f"{name}-{theme}.png"), full_page=True)

                if result.missing or result.console_errors or result.failed_requests:
                    result.ok = False
                results.append(result)

            context.close()
        browser.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=None, help="Base URL. Defaults to serving web/out locally.")
    parser.add_argument("--shots", default=str(WEB / "screenshots"))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    httpd = None
    base = args.base
    if not base:
        if not OUT.exists():
            print("web/out not found - run `npm run build` first.")
            return 2
        httpd = serve(OUT)
        base = "http://127.0.0.1:8899"
        print(f"Serving {OUT} at {base}")

    print(f"Verifying {base} in Chrome...\n")
    try:
        results = check(base, Path(args.shots), args.headed)
    finally:
        if httpd:
            httpd.shutdown()

    failures = [r for r in results if not r.ok]
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {r.path:<24} HTTP {r.status}  {r.text_len:>5} chars  {' '.join(r.notes)}")
        for m in r.missing:
            print(f"         missing text: {m!r}")
        for c in r.console_errors:
            print(f"         console: {c}")
        for f in r.failed_requests:
            print(f"         request failed: {f}")

    print(f"\n{len(results) - len(failures)}/{len(results)} checks passed.")
    print(f"Screenshots: {args.shots}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
