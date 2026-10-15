"""Before-and-after screenshots for homepage pass 2a: the hero fold, the demo, the blue band and /login, in light and
dark, on a desktop (1366x768) and a 390 px phone, plus reduced motion. Not collected by default (no test_
prefix). Run it explicitly:

    RAINCLI_SHOTS_DIR=<dir> RAINCLI_SHOTS_PREFIX=before|after pytest tests/gui/shots_homepage_2a.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "homepage-2a-screenshots")
PREFIX = os.environ.get("RAINCLI_SHOTS_PREFIX", "after")
SIZES = {"desktop": (1366, 768), "phone": (390, 844)}


def _open(browser, live, path, size, scheme, motion="no-preference"):  # noqa: F811
    width, height = SIZES[size]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                  reduced_motion=motion, device_scale_factor=1)
    page = context.new_page()
    page.goto(live + path, wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    return context, page


def _settle(page):
    """Sections revealed and the demo held on its finished frame (13 s)."""
    page.evaluate("""() => { document.querySelectorAll('.rv').forEach((e) => e.classList.add('in'));
        const d = document.querySelector('[data-demo]');
        if (d) d.getAnimations({subtree: true}).forEach((a) => { a.pause(); a.currentTime = 13000; }); }""")
    page.wait_for_timeout(900)


def test_screenshots(browser, live):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    saved = []

    def shot(page, name, locator=None):
        path = OUT / f"{PREFIX}-{name}.png"
        if locator:  # from the full page at scroll 0, so the sticky header never covers the section
            page.evaluate("window.scrollTo(0, 0)")
            box = page.evaluate(f"(() => {{ const r = document.querySelector({locator!r}).getBoundingClientRect();"
                                f" return {{x: r.left + scrollX, y: r.top + scrollY, width: r.width, height: r.height}}; }})()")
            page.screenshot(path=str(path), full_page=True, clip=box)
        else:
            page.screenshot(path=str(path))
        saved.append(path.name)

    for size in SIZES:
        for scheme in ("light", "dark"):
            context, page = _open(browser, live, "/", size, scheme)
            _settle(page)
            shot(page, f"hero-fold-{size}-{scheme}")
            shot(page, f"demo-{size}-{scheme}", "[data-demo]")
            shot(page, f"band-{size}-{scheme}", "#how")
            context.close()
            context, page = _open(browser, live, "/login", size, scheme)
            page.evaluate("document.activeElement && document.activeElement.blur()")
            shot(page, f"login-{size}-{scheme}")
            context.close()
    for scheme in ("light", "dark"):
        context, page = _open(browser, live, "/", "desktop", scheme, motion="reduce")
        shot(page, f"hero-fold-desktop-{scheme}-reduced-motion")
        context.close()
    with open(OUT / f"{PREFIX}-files.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(saved) + "\n")
