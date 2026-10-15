"""Screenshots of the homepage's agent-teams animation for review against marketing's frames/ (demo-teams v3). Not
collected by default (no test_ prefix). Run it explicitly:

    RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_demo_teams.py

Time is Playwright's fake clock, started when the section is on screen. Its requestAnimationFrame runs the
animation's own clock ahead of real time, for the original as much as for the port, so a frame "at t" is not
marketing's ?t frame. With ``RAINCLI_DEMO_TEAMS_ORIGINAL=<path to demo-teams-v3.html>`` the original is rendered
through the same clock at the strip's moments and compared pixel for pixel with the port (``port-vs-original.txt``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures
from .test_demo_teams_gui import NO_FILTER, open_page, settle, to_section

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "demo-teams-screenshots")
STRIP = (1.6, 4.6, 8.6, 10.1, 11.15, 12.75, 14.6, 15.4, 16.45, 18.3, 20.05, 22.75)


def at(page, seconds, done):
    """Run the clock on to `seconds` of the loop (`done` already elapsed)."""
    page.clock.run_for(max(0, int(round((seconds - done) * 1000))))
    return seconds


def test_screenshots(browser, live):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    heights = []
    for size in ("desktop", "phone"):
        for scheme in ("light", "dark"):
            context, page, _ = open_page(browser, live, size=size, scheme=scheme)
            heights.append(f"{size}-{scheme}: page height {page.evaluate('document.documentElement.scrollHeight')} px")
            page.screenshot(path=str(OUT / f"page-fold-{size}-{scheme}.png"))
            to_section(page)
            done = 0.1
            for t in (STRIP if scheme == "light" else (11.15,)):  # light: one frame per scene, as marketing's strips
                done = at(page, t, done)
                page.locator("#adm").screenshot(path=str(OUT / f"strip-{size}-{scheme}-t{t:05.2f}.png"))
                if t == 11.15:  # marketing's single frames are at 11.15 s
                    page.locator("#adm").screenshot(path=str(OUT / f"frame-{size}-{scheme}-t11.15.png"))
            page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
            page.screenshot(path=str(OUT / f"page-full-{size}-{scheme}.png"), full_page=True)
            context.close()
        for scheme in ("light", "dark"):
            context, page, _ = open_page(browser, live, size=size, scheme=scheme, motion="reduce")
            to_section(page)
            page.locator("#adm").screenshot(path=str(OUT / f"reduced-motion-{size}-{scheme}.png"))
            context.close()
    # Safari (no Canvas 2D filter): the title mid blur-in, with and without the fallback.
    for name, init in (("with-blur", None), ("safari-fallback", NO_FILTER)):
        context, page, _ = open_page(browser, live, init=init)
        to_section(page)
        at(page, 1.0, 0.1)
        page.locator("#adm").screenshot(path=str(OUT / f"title-t1.00-{name}.png"))
        at(page, 2.6, 1.0)
        page.locator("#adm").screenshot(path=str(OUT / f"title-t2.60-{name}.png"))
        context.close()
    # The pause: hovered with a real mouse (desktop), and after a tap (touch phone).
    context, page, _ = open_page(browser, live)
    to_section(page)
    at(page, 8.6, 0.1)
    box = page.locator("#adm").bounding_box()
    page.mouse.move(box["x"] + box["width"] * .6, box["y"] + box["height"] * .6, steps=6)
    settle(page)
    page.locator("#adm").screenshot(path=str(OUT / "pause-hover-desktop-light.png"))
    context.close()
    context, page, _ = open_page(browser, live, size="phone", touch=True, scheme="dark")
    to_section(page)
    at(page, 8.6, 0.1)
    box = page.locator("#adm").bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + 200)
    settle(page)
    page.locator("#adm").screenshot(path=str(OUT / "pause-tapped-phone-dark.png"))
    context.close()
    (OUT / "heights.txt").write_text("\n".join(heights) + "\n", "utf-8")
    original = os.environ.get("RAINCLI_DEMO_TEAMS_ORIGINAL")
    if original:
        compare_with_original(browser, live, Path(original))


def _canvas_frames(browser, url, sizes, prepare):  # noqa: F811
    out = {}
    for size in sizes:
        width, height = {"desktop": (1440, 900), "phone": (390, 844)}[size]
        context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
        page = context.new_page()
        page.clock.install()
        page.goto(url, wait_until="load")
        page.wait_for_function("() => document.documentElement.dataset.ready === '1'", timeout=20000)
        prepare(page)
        done = 0.1
        for t in STRIP:
            page.clock.run_for(int(round((t - done) * 1000)))
            done = t
            out[(size, t)] = page.locator("#adm canvas").screenshot()
        context.close()
    return out


DIFF = """async ([a, b]) => {
    const load = (src) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = src; });
    const [ia, ib] = await Promise.all([load(a), load(b)]);
    if (ia.width !== ib.width || ia.height !== ib.height) return {size: [ia.width, ia.height, ib.width, ib.height]};
    const c = document.createElement('canvas'); c.width = ia.width; c.height = ia.height;
    const x = c.getContext('2d');
    x.drawImage(ia, 0, 0); const da = x.getImageData(0, 0, c.width, c.height).data;
    x.clearRect(0, 0, c.width, c.height); x.drawImage(ib, 0, 0); const db = x.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let k = 0; k < da.length; k += 4)
        if (Math.max(Math.abs(da[k] - db[k]), Math.abs(da[k + 1] - db[k + 1]), Math.abs(da[k + 2] - db[k + 2])) > 24) n++;
    return {share: n / (c.width * c.height)};
}"""


def compare_with_original(browser, live, original):  # noqa: F811
    """The port against marketing's own file, through the same fake clock: the share of pixels that differ by more
    than 24/255 in any channel, and side-by-side images (original left, port right)."""
    import base64

    def section(page):
        page.evaluate("document.getElementById('adm').scrollIntoView({block: 'center', behavior: 'instant'})")
        page.wait_for_timeout(350)
        page.clock.run_for(100)

    port = _canvas_frames(browser, live + "/", ("desktop", "phone"), section)
    orig = _canvas_frames(browser, original.as_uri(), ("desktop", "phone"), section)
    context = browser.new_context()
    page = context.new_page()
    lines = []
    for key in port:
        urls = ["data:image/png;base64," + base64.b64encode(x[key]).decode() for x in (orig, port)]
        result = page.evaluate(DIFF, urls)
        tag = f"{key[0]}-t{key[1]:05.2f}"
        lines.append(f"{key[0]} t={key[1]:5.2f}: " + (f"{result['share'] * 100:.3f}% of pixels differ (> 24/255)"
                                                      if "share" in result else f"sizes differ {result['size']}"))
        (OUT / f"port-vs-original-{tag}-original.png").write_bytes(orig[key])
        (OUT / f"port-vs-original-{tag}-port.png").write_bytes(port[key])
    context.close()
    (OUT / "port-vs-original.txt").write_text("\n".join(lines) + "\n", "utf-8")
