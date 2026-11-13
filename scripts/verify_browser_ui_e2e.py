"""Headless Chromium audit of the live KRONOS dashboard (GitHub Pages)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PAGES = "https://vetukurisuresh.github.io/kronos-runner-public/"
OUT = Path("reports/browser_ui_e2e.json")


def main() -> int:
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            errors.append(f"{name}: {detail}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

        page.goto(PAGES, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#kfBtn", timeout=30000)
        page.wait_for_timeout(2500)

        # Launcher + panel.
        if page.is_visible("#kfWrap"):
            pass
        else:
            page.click("#kfBtn")
        page.wait_for_timeout(1200)
        check("panel opens", page.is_visible("#kfWrap"), "kfWrap visible")

        svgs = page.locator("svg.kf-flow")
        check("5-node canvas present", svgs.count() >= 3, f"svg count={svgs.count()}")
        box = svgs.first.bounding_box() if svgs.count() else None
        check("canvas has real size", bool(box) and box["width"] > 300 and box["height"] > 80,
              json.dumps(box))

        body = page.inner_text("#kfBody")
        pending = body.count("feed pending")
        check("live numbers, not only placeholders", pending < body.count("KRONOS-"),
              f"pending_nodes={pending}")

        # Tabs.
        for tab in ("directional_100k", "scalping_100k", "zero_to_hero_100k", "all"):
            page.click(f' #kfTabs [data-tab="{tab}"]')
            page.wait_for_timeout(400)
            n = svgs.count()
            check(f"tab {tab} renders", n >= 1, f"svg={n}")

        # Hover the hub node.
        hub = page.locator('#kfWrap [data-node="hub"]').first
        hub.dispatch_event("mouseenter")
        hub.hover(force=True)
        page.wait_for_timeout(500)
        tip = page.locator("#kfTip")
        tip_text = tip.inner_text() if tip.is_visible() else ""
        check("hub hover telemetry", tip.is_visible() and "UPSTOX" in tip_text.upper(), tip_text[:120])

        # Chart button (may not exist with zero trades).
        btns = page.locator("[data-chart]")
        if btns.count():
            btns.first.click()
            page.wait_for_timeout(600)
            check("chart modal opens", page.is_visible("#kfModal"), "kfModal")
            cbox = page.locator("#kfCanvas").bounding_box()
            check("canvas size", bool(cbox) and cbox["width"] > 400 and cbox["height"] > 220, json.dumps(cbox))
        else:
            check("chart button present", False, "no trades yet -> no chart button rendered")

        browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"checks": checks, "errors": errors}, indent=1), encoding="utf-8")
    print(json.dumps({"checks": checks, "errors": errors}, indent=1))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
