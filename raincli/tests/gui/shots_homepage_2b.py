"""Before-and-after screenshots for homepage pass 2b (marketing's tighter page): the fold and the full page in
light and dark, on a desktop (1440x900, as marketing measured, and 1366x768) and a 390 px phone, plus reduced
motion, with each page's height in heights.txt. Not collected by default (no test_ prefix). Run it explicitly:

    RAINCLI_SHOTS_DIR=<dir> RAINCLI_SHOTS_PREFIX=before|after pytest tests/gui/shots_homepage_2b.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "homepage-2b-screenshots")
PREFIX = os.environ.get("RAINCLI_SHOTS_PREFIX", "after")
SIZES = {"desktop": (1440, 900), "laptop": (1366, 768), "phone": (390, 844)}


def test_screenshots(browser, live):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    heights = []
    for size, (width, height) in SIZES.items():
        for scheme in ("light", "dark"):
            for motion in ("no-preference", "reduce"):
                if motion == "reduce" and size == "laptop":
                    continue
                context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                              reduced_motion=motion, device_scale_factor=1)
                page = context.new_page()
                page.goto(live + "/", wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                if motion != "reduce":  # every section revealed, the demo held on its finished frame
                    page.evaluate("""() => { document.querySelectorAll('.rv').forEach((e) => e.classList.add('in'));
                        document.querySelector('[data-demo]').getAnimations({subtree: true})
                          .forEach((a) => { a.pause(); a.currentTime = 13000; }); }""")
                    page.wait_for_timeout(900)
                tag = f"{size}-{scheme}" + ("-reduced-motion" if motion == "reduce" else "")
                page.screenshot(path=str(OUT / f"{PREFIX}-fold-{tag}.png"))
                page.screenshot(path=str(OUT / f"{PREFIX}-full-{tag}.png"), full_page=True)
                h = page.evaluate("document.documentElement.scrollHeight")
                heights.append(f"{PREFIX} {tag} {width}x{height}: page height {h} px")
                context.close()
    with open(OUT / f"{PREFIX}-heights.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(heights) + "\n")
