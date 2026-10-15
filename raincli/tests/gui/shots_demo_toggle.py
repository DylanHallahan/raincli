"""Screenshots of the demo's pause (WCAG 2.2.2): at rest (invisible), hovered, focused by keyboard and paused, in
light and dark, desktop and phone, a touch screen (rest and after a tap), and reduced motion (no toggle). Not collected by default. Run it explicitly:

    RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_demo_toggle.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "demo-toggle-screenshots")


def test_screenshots(browser, live):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    for size in ((1366, 768), (390, 844)):
        for scheme in ("light", "dark"):
            context = browser.new_context(viewport={"width": size[0], "height": size[1]}, color_scheme=scheme)
            page = context.new_page()
            page.goto(live + "/", wait_until="networkidle")
            page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
            win = page.locator(".hero .win-wrap")
            tag = f"{size[0]}-{scheme}"
            page.mouse.move(2, size[1] - 2)
            win.screenshot(path=str(OUT / f"toggle-rest-{tag}.png"))
            page.hover(".hero .win")
            page.wait_for_timeout(250)
            win.screenshot(path=str(OUT / f"toggle-hover-{tag}.png"))
            page.mouse.move(2, size[1] - 2)
            page.locator("[data-demo-toggle]").focus()
            page.evaluate("document.querySelector('[data-demo-toggle]').matches(':focus-visible')")
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            page.wait_for_timeout(250)
            win.screenshot(path=str(OUT / f"toggle-focus-{tag}.png"))
            page.keyboard.press("Enter")
            page.wait_for_timeout(250)
            win.screenshot(path=str(OUT / f"toggle-paused-{tag}.png"))
            context.close()
    for scheme in ("light", "dark"):  # a touch screen: nothing at rest; a tap on the window shows the pause
        context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True,
                                      color_scheme=scheme)
        page = context.new_page()
        page.goto(live + "/", wait_until="load")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        win = page.locator(".hero .win-wrap")
        win.screenshot(path=str(OUT / f"toggle-touch-rest-390-{scheme}.png"))
        page.tap(".hero .win", position={"x": 150, "y": 220})
        page.wait_for_timeout(600)
        win.screenshot(path=str(OUT / f"toggle-touch-tapped-390-{scheme}.png"))
        context.close()
    context = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    page = context.new_page()
    page.goto(live + "/", wait_until="networkidle")
    page.hover(".hero .win")
    page.locator(".hero .win-wrap").screenshot(path=str(OUT / "toggle-reduced-motion-1366-light.png"))
    context.close()
