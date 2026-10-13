"""Screenshots of the homepage (concept C "Paper") and its link-preview image, in headless Chromium. Not collected
by default (no test_ prefix). Run it explicitly:

    RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_landing_c.py

With ``RAINCLI_OG_OUT=<path>`` it also writes the link-preview image (1200x630) there; the page's own copy is
``raincli_server/web/static/og.png``, rendered from ``og_landing_c.html`` with the site's own fonts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "landing-c-screenshots")
OG_HTML = Path(__file__).with_name("og_landing_c.html")


def _page(browser, live, size, scheme, motion="no-preference"):  # noqa: F811
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, color_scheme=scheme,
                                  reduced_motion=motion, device_scale_factor=1)
    page = context.new_page()
    page.goto(live + "/", wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    return context, page


def _settle(page):
    """Every section revealed and the demo held on its finished frame (13 s), so a full page shows everything."""
    page.evaluate("""() => { document.querySelectorAll('.rv').forEach((e) => e.classList.add('in'));
        const d = document.querySelector('[data-demo]');
        d.getAnimations({subtree: true}).forEach((a) => { a.pause(); a.currentTime = 13000; }); }""")
    page.wait_for_timeout(900)  # the reveal transition


def test_screenshots(browser, live):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    saved = []
    for scheme in ("light", "dark"):
        context, page = _page(browser, live, (1366, 768), scheme)
        _settle(page)
        page.screenshot(path=str(OUT / f"desktop-fold-{scheme}-1366x768.png"))
        page.screenshot(path=str(OUT / f"desktop-full-{scheme}-1366.png"), full_page=True)
        saved += [f"desktop-fold-{scheme}-1366x768.png", f"desktop-full-{scheme}-1366.png"]
        context.close()
        context, page = _page(browser, live, (390, 844), scheme)
        _settle(page)
        page.screenshot(path=str(OUT / f"phone-fold-{scheme}-390x844.png"))
        page.screenshot(path=str(OUT / f"phone-full-{scheme}-390.png"), full_page=True)
        saved += [f"phone-fold-{scheme}-390x844.png", f"phone-full-{scheme}-390.png"]
        context.close()
    for scheme in ("light", "dark"):
        context, page = _page(browser, live, (1366, 768), scheme, motion="reduce")
        page.screenshot(path=str(OUT / f"desktop-reduced-motion-{scheme}-1366x768.png"))
        saved.append(f"desktop-reduced-motion-{scheme}-1366x768.png")
        context.close()
    for scheme in ("light", "dark"):  # the sign-in page, restyled to match (second commit)
        context = browser.new_context(viewport={"width": 1366, "height": 768}, color_scheme=scheme)
        page = context.new_page()
        page.goto(live + "/login", wait_until="networkidle")
        page.evaluate("document.activeElement && document.activeElement.blur()")
        page.screenshot(path=str(OUT / f"login-{scheme}-1366x768.png"))
        saved.append(f"login-{scheme}-1366x768.png")
        context.close()
    # The link-preview image, rendered with the site's fonts (served by the live server).
    # bypass_csp: the preview's own page has an inline style; it is never served, only rendered here.
    context = browser.new_context(viewport={"width": 1200, "height": 630}, device_scale_factor=1, bypass_csp=True)
    page = context.new_page()
    page.goto(live + "/", wait_until="load")  # the page's origin, so /static/... resolves
    page.set_content(OG_HTML.read_text("utf-8"), wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(300)
    page.screenshot(path=str(OUT / "og.png"))
    saved.append("og.png")
    if os.environ.get("RAINCLI_OG_OUT"):
        page.screenshot(path=os.environ["RAINCLI_OG_OUT"])
    context.close()
    (OUT / "files.txt").write_text("\n".join(saved) + "\n", "utf-8")
