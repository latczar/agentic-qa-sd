"""Capture the README screenshots.

Scripted rather than hand-cropped so they can be regenerated after a UI change
instead of slowly going stale. Needs the stack running. The shots show whatever
is on the board, so raise a sample ticket or two first if it is empty.

    python docs/capture_screenshots.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
OUT = Path(__file__).parent / "media"


def shot(page, name: str, full_page: bool = False) -> None:
    page.screenshot(path=OUT / name, full_page=full_page)
    print(name)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Reduced motion, so a still never catches a pulse or the drawer halfway
        # through sliding in. The overseer honours it the same way it would for
        # a person who asked their system for less motion.
        context = browser.new_context(
            viewport={"width": 1440, "height": 1080},
            device_scale_factor=2,
            reduced_motion="reduce",
            color_scheme="light",
        )
        page = context.new_page()

        # The live stream never goes quiet, so wait for content, not the network.
        page.goto(f"{BASE}/overseer", wait_until="domcontentloaded")
        page.wait_for_selector("#pet-level:not([hidden])", timeout=15_000)
        page.wait_for_selector("#decide .ticket-card, #decide .empty", timeout=15_000)
        # Idle workers report in every five seconds; wait for one to show on the track.
        page.wait_for_selector("#track .token", timeout=15_000)
        page.wait_for_timeout(1000)
        shot(page, "05-overseer-floor.png")

        # Full width for the other two tabs: the drawer is the floor's.
        if page.locator("#drawer.open").count():
            page.click("#drawer-close")
        page.click('[data-tab="team"]')
        page.wait_for_selector("#org-tree .member", timeout=15_000)
        page.wait_for_timeout(800)
        shot(page, "06-overseer-team.png", full_page=True)

        page.click('[data-tab="routines"]')
        page.wait_for_selector("#routines .routine", timeout=15_000)
        page.wait_for_timeout(500)
        shot(page, "07-overseer-routines.png")

        page.goto(BASE, wait_until="domcontentloaded")
        page.locator("nav button", has_text="Pipeline").click()
        page.wait_for_timeout(2500)
        shot(page, "08-console-pipeline.png")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
