"""Capture the README screenshots.

Scripted rather than hand-cropped so they can be regenerated after a UI change
instead of slowly going stale. Needs the stack running and at least one ticket
in each interesting state.

    python docs/capture_screenshots.py
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
OUT = Path(__file__).parent / "media"
VIEWPORT = {"width": 1280, "height": 900}


def expand_analysis_for(page, status: str) -> bool:
    """Open the analysis panel on the first ticket with the given status."""
    cards = page.locator(".ticket").all()
    for card in cards:
        if card.locator(f".b-{status}").count():
            card.locator("button.toggle").click()
            page.wait_for_selector(".analysis", timeout=5000)
            card.scroll_into_view_if_needed()
            return True
    return False


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector(".ticket", timeout=10_000)

        page.screenshot(path=OUT / "01-ticket-list.png")
        print("01-ticket-list.png")

        # A confident, evidenced answer that resolved on its own.
        if expand_analysis_for(page, "RESOLVED"):
            page.screenshot(path=OUT / "02-analysis-resolved.png")
            print("02-analysis-resolved.png")
            page.locator("button.toggle", has_text="Hide").first.click()

        # One the gate held back for a human, with the reason visible.
        page.reload(wait_until="networkidle")
        page.wait_for_selector(".ticket", timeout=10_000)
        if expand_analysis_for(page, "AWAITING_APPROVAL"):
            page.screenshot(path=OUT / "03-awaiting-approval.png")
            print("03-awaiting-approval.png")

        page.goto(f"{BASE}/docs", wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.screenshot(path=OUT / "04-api-docs.png")
        print("04-api-docs.png")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
