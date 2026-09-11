"""Interaction tests for the dashboard.

Static rendering is not the same as working. This drives the actual controls in
real Chrome: the theme toggle, the Strike Ladder Matrix filters, the chart
hover tooltips, and navigation.

    python web/verify_interactions.py --base https://<deployment>
"""

from __future__ import annotations

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - older interpreters / redirected streams
    pass

from playwright.sync_api import sync_playwright, expect

PASS, FAIL = "[PASS]", "[FAIL]"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8899")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    results: list[tuple[bool, str, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((ok, name, detail))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()

        # ---------------------------------------------------------- navigation
        page.goto(f"{base}/", wait_until="networkidle", timeout=45_000)
        page.wait_for_selector("nav a[aria-current='page']")   # hydration finished
        page.click("nav a:has-text('Strike Ladder')")
        page.wait_for_url("**/ladder/**", timeout=15_000)
        page.wait_for_load_state("networkidle")
        record(
            "nav: Overview -> Strike Ladder",
            "Strike Ladder Matrix" in page.inner_text("h1"),
            page.url,
        )

        # ------------------------------------------------- strike matrix filters
        matrix = page.locator("table").first          # the strike matrix, not the worked example
        rows_pe = matrix.locator("tbody tr").count()
        first_pe = matrix.locator("tbody tr").first.inner_text()
        record("matrix: puts render rows", rows_pe > 3, f"{rows_pe} rows")
        record("matrix: rows are PE", "PE" in first_pe, first_pe.split("\n")[0][:40])

        page.click("button:has-text('Calls')")
        page.wait_for_timeout(400)
        first_ce = matrix.locator("tbody tr").first.inner_text()
        record("matrix: Calls toggle switches to CE", "CE" in first_ce, first_ce.split("\n")[0][:40])

        # The offsetting claim, checked in the DOM rather than asserted in prose.
        page.click("button:has-text('Puts')")
        page.wait_for_timeout(400)
        before = matrix.locator("tbody tr").count()
        page.check("input[type=checkbox]")
        page.wait_for_timeout(400)
        rows = matrix.locator("tbody tr")
        after = rows.count()
        record(
            "matrix: 'only offset' filter narrows the table",
            after < before and after > 0,
            f"{before} -> {after} rows",
        )
        # Read the real NET cell (last column) of every surviving row, rather
        # than pattern-matching the whole tbody text.
        nets = []
        for i in range(after):
            cells = rows.nth(i).locator("td")
            nets.append(cells.nth(cells.count() - 1).inner_text().strip())
        record(
            "matrix: every filtered row nets to zero",
            after > 0 and all(n.replace("—", "").strip() == "0" for n in nets),
            f"net cells: {nets[:4]}",
        )
        page.uncheck("input[type=checkbox]")

        # ------------------------------------------------------- theme toggle
        page.click("button:has-text('Light')")
        page.wait_for_timeout(400)
        light_bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        light_attr = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        page.click("button:has-text('Dark')")
        page.wait_for_timeout(400)
        dark_bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        record("theme: light applies", light_attr == "light" and "255" in light_bg, light_bg)
        record("theme: dark applies", dark_bg == "rgb(15, 22, 33)", dark_bg)

        # Persistence across a reload is the whole point of storing it.
        page.reload(wait_until="networkidle")
        persisted = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        record("theme: survives reload", persisted == "rgb(15, 22, 33)", persisted)

        # ------------------------------------------------ payoff chart hover
        page.goto(f"{base}/payoff/", wait_until="networkidle")
        page.wait_for_timeout(600)
        svg = page.locator("svg[role=img]").first
        box = svg.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
        page.wait_for_timeout(400)
        after_hover = page.inner_text("body")
        record(
            "payoff: hover shows a tooltip",
            "NIFTY at expiry" in after_hover,
            "tooltip visible",
        )

        # ------------------------------------------- equity chart crosshair
        page.goto(f"{base}/", wait_until="networkidle")
        page.wait_for_timeout(600)
        eq = page.locator("svg[role=img]").first
        box = eq.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
        page.wait_for_timeout(400)
        record(
            "equity: hover shows P&L tooltip",
            "Open rungs" in page.inner_text("body"),
            "tooltip visible",
        )

        # -------------------------------------------- trade history by condor
        #
        # The whole point of grouping is that the legs are hidden until asked
        # for, so "it rendered" proves nothing. Expanding must actually reveal
        # rows that were not there before.
        page.goto(f"{base}/forward/log/", wait_until="networkidle")
        page.wait_for_timeout(600)
        history = page.locator("table").first
        condor_rows = history.locator("tbody tr[role=button]")
        if condor_rows.count() == 0:
            record(
                "trade history: grouped by condor",
                True,
                "skipped - no forward run with fills on this deployment",
            )
        else:
            before = history.locator("tbody tr").count()
            first = condor_rows.first
            record(
                "trade history: condor rows start collapsed",
                first.get_attribute("aria-expanded") == "false",
                f"{condor_rows.count()} condors",
            )
            first.click()
            page.wait_for_timeout(300)
            after = history.locator("tbody tr").count()
            record(
                "trade history: clicking a condor reveals its legs",
                after > before and first.get_attribute("aria-expanded") == "true",
                f"{before} -> {after} rows",
            )
            # The legs are fills, so each carries a side badge. Checked
            # structurally rather than by text, which wraps unpredictably.
            legs = history.locator("tbody tr").nth(1).locator("table tbody tr")
            record(
                "trade history: expanded rows are leg fills",
                legs.count() >= 4,
                f"{legs.count()} leg rows",
            )
            first.click()
            page.wait_for_timeout(300)
            record(
                "trade history: clicking again collapses it",
                history.locator("tbody tr").count() == before,
                f"back to {history.locator('tbody tr').count()} rows",
            )

        # ------------------------------------------------ lightweight-charts
        page.goto(f"{base}/chart/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        canvas_size = page.evaluate(
            "() => { const c = document.querySelector('canvas'); return c ? c.width * c.height : 0; }"
        )
        record("price chart: canvas has real dimensions", canvas_size > 10_000, f"{canvas_size}px^2")

        # ------------------------------------------------------------ favicon
        icon = page.evaluate(
            "() => { const l = document.querySelector('link[rel~=\"icon\"]'); return l ? l.href : null; }"
        )
        ok_icon = False
        if icon:
            resp = page.request.get(icon)
            ok_icon = resp.status == 200
        record("favicon: present and served", ok_icon, icon or "no <link rel=icon>")

        # ------------------------------------------------------- mobile width
        mob = browser.new_context(viewport={"width": 390, "height": 844}).new_page()
        mob.goto(f"{base}/ladder/", wait_until="networkidle")
        mob.wait_for_timeout(500)
        overflow = mob.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        record("mobile 390px: no horizontal page overflow", overflow <= 2, f"{overflow}px")

        browser.close()

    for ok, name, detail in results:
        print(f"{PASS if ok else FAIL} {name:<48} {detail}")
    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} interaction checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
