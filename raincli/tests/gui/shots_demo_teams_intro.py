"""Screenshots of the homepage's first-visit intro (demo-teams v3, round 4) for review. Not collected by default
(no test_ prefix). Run it explicitly:

    RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_demo_teams_intro.py

Time is Playwright's fake clock: the intro opens over the page, plays (frames at 1.6, 8.6 and 12.75 s of the
sequence), ends and fades into the hero; then the hero with "Watch the intro", and the reopened intro. Sizes: desktop
1440x900, phone 390x844, and a short desktop window 1280x520 (review F1); the landscape phone 844x390 has no autoplay
(review F4), so it is shot reopened from the link.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures
from .test_demo_teams_gui import new_context, ready, visit

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "demo-teams-intro-screenshots")


def test_screenshots(browser, live):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    for size in ("desktop", "phone", "short"):
        for scheme in ("light", "dark"):
            tag = f"{size}-{scheme}"
            context = new_context(browser, size=size, scheme=scheme)
            page, _ = visit(context, live)
            ready(page)
            page.wait_for_timeout(300)  # the fade-in (real time)
            page.screenshot(path=str(OUT / f"open-{tag}.png"))
            done = 0.1
            for t in (1.6, 8.6, 12.75):
                page.clock.run_for(int((t - done) * 1000))
                done = t
                page.screenshot(path=str(OUT / f"playing-t{t:05.2f}-{tag}.png"))
            for _ in range(60):  # to the end: it stops and fades into the hero
                page.clock.run_for(250)
                if page.evaluate("document.querySelector('.adm-intro')?.dataset.introState") == "fading":
                    break
            page.wait_for_timeout(300)  # half way through the 0.6 s fade (real time)
            page.screenshot(path=str(OUT / f"fading-into-the-hero-{tag}.png"))
            page.clock.run_for(800)
            page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / f"after-the-intro-hero-{tag}.png"))  # with "Watch the intro"
            page.click("[data-intro-open]")
            ready(page)
            page.clock.run_for(4000)
            page.screenshot(path=str(OUT / f"reopened-{tag}.png"))
            context.close()
    for scheme in ("light", "dark"):
        context = new_context(browser, size="landscape", scheme=scheme)
        page, _ = visit(context, live)
        page.screenshot(path=str(OUT / f"landscape-phone-no-autoplay-{scheme}.png"))
        page.click("[data-intro-open]")
        ready(page)
        page.clock.run_for(8500)
        page.screenshot(path=str(OUT / f"landscape-phone-reopened-t08.60-{scheme}.png"))
        context.close()
    for scheme in ("light", "dark"):
        context = new_context(browser, scheme=scheme, motion="reduce")
        page, _ = visit(context, live)
        page.screenshot(path=str(OUT / f"reduced-motion-no-intro-desktop-{scheme}.png"))
        page.click("[data-intro-open]")
        ready(page)
        page.screenshot(path=str(OUT / f"reduced-motion-reopened-still-desktop-{scheme}.png"))
        context.close()
