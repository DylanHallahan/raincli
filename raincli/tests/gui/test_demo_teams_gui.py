"""The homepage's agent-teams animation (demo-teams v3 "The Gap") in headless Chromium: CSP-clean, self-hosted fonts
only, right below the hero; it plays only on screen and while the tab is visible; the draft's query-string hooks do
nothing; the pause shows on a real mouse hover, on keyboard focus and after a tap on a touch screen, never at rest;
reduced motion holds the composed end state; and without Canvas 2D filter (Safari) it still draws legibly.

Time is Playwright's fake clock (it drives requestAnimationFrame), so the canvas is compared frame for frame.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

SIZES = {"desktop": (1440, 900), "phone": (390, 844)}
CANVAS = "() => document.querySelector('#adm canvas').toDataURL()"
OPACITY = "() => getComputedStyle(document.querySelector('#adm .adm-pp')).opacity"
# Canvas 2D without filter, as in Safari: assignments are ignored and it reads back undefined.
NO_FILTER = """Object.defineProperty(CanvasRenderingContext2D.prototype, 'filter',
    {configurable: true, get() { return undefined; }, set(v) {}});"""


def open_page(browser, live, *, size="desktop", scheme="light", motion="no-preference", path="/", touch=False,  # noqa: F811
              init=None):
    width, height = SIZES[size]
    options = dict(viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion=motion,
                   device_scale_factor=1)
    if touch:
        options.update(has_touch=True, is_mobile=True)
    context = browser.new_context(**options)
    if init:
        context.add_init_script(init)
    page = context.new_page()
    log = {"console": [], "errors": [], "requests": []}
    page.on("console", lambda m: log["console"].append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: log["errors"].append(str(e)))
    page.on("request", lambda r: log["requests"].append(r.url))
    page.clock.install()
    page.goto(live + path, wait_until="load")
    page.wait_for_function("() => document.documentElement.dataset.ready === '1'")
    return context, page, log


def to_section(page):
    page.evaluate("document.getElementById('adm').scrollIntoView({block: 'center', behavior: 'instant'})")
    settle(page)
    page.clock.run_for(100)


def settle(page):
    """Real time for what the fake clock doesn't drive: IntersectionObserver delivery and CSS opacity transitions."""
    page.wait_for_timeout(350)


def frame(page) -> str:
    return hashlib.sha256(page.evaluate(CANVAS).encode()).hexdigest()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_csp_clean_self_hosted_and_right_below_the_hero(browser, live, size, scheme):  # noqa: F811
    context, page, log = open_page(browser, live, size=size, scheme=scheme)
    try:
        to_section(page)
        page.clock.run_for(3000)
        assert log["console"] == [] and log["errors"] == [], log  # a CSP violation is a console error
        origin = urlparse(live).netloc
        assert all(urlparse(u).netloc == origin for u in log["requests"]), log["requests"]  # no Google Fonts
        fonts = {urlparse(u).path.rsplit("/", 1)[-1] for u in log["requests"] if "/fonts/" in u}
        assert {"jetbrains-mono-medium-v2304.woff2", "jetbrains-mono-bold-v2304.woff2",
                "noto-serif-display-light-v2009.woff2"} <= fonts, fonts
        layout = page.evaluate("""() => {
            const s = document.getElementById('adm'), r = s.getBoundingClientRect();
            return {after: s.previousElementSibling.id, width: r.width, inner: innerWidth,
                    scroll: document.documentElement.scrollWidth <= innerWidth};
        }""")
        assert layout["after"] == "hero" and layout["width"] == layout["inner"] and layout["scroll"], layout
        assert page.evaluate(OPACITY) == "0"  # nothing visible at rest
    finally:
        context.close()


def test_it_plays_only_on_screen_and_while_the_tab_is_visible(browser, live):  # noqa: F811
    context, page, _ = open_page(browser, live, size="phone")
    try:
        page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        settle(page)
        held = frame(page)
        page.clock.run_for(1500)
        assert frame(page) == held  # below the fold: not playing
        to_section(page)
        page.clock.run_for(1500)
        playing = frame(page)
        assert playing != held
        page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                                 Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
                                 document.dispatchEvent(new Event('visibilitychange')); }""")
        page.clock.run_for(1500)
        assert frame(page) == playing  # the tab hidden: held
        page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                                 Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
                                 document.dispatchEvent(new Event('visibilitychange')); }""")
        page.clock.run_for(1500)
        assert frame(page) != playing
    finally:
        context.close()


def test_the_drafts_query_hooks_do_nothing(browser, live):  # noqa: F811
    """?t (hold a frame), ?theme (force a theme) and ?rec are gone: with them the animation still plays, in the
    browser's own theme (light here, even with ?theme=dark), and exposes no debug globals."""
    for path in ("/", "/?t=5&theme=dark&rec=1"):
        context, page, log = open_page(browser, live, path=path)
        try:
            to_section(page)
            hashes = []
            for _ in range(3):
                page.clock.run_for(2000)
                hashes.append(frame(page))
            assert len(set(hashes)) == 3, path  # playing, not held on a frame
            assert page.evaluate("() => [document.documentElement.dataset.theme, typeof window.__frame,"
                                 " typeof window.__probe, typeof window.__labels]") == [None, "undefined",
                                                                                        "undefined", "undefined"]
            paper = page.evaluate("""() => { const c = document.querySelector('#adm canvas');
                return Array.from(c.getContext('2d').getImageData(4, 4, 1, 1).data.slice(0, 3)); }""")
            assert paper == [243, 238, 228], (path, paper)  # light paper, not the forced dark
            assert log["console"] == [] and log["errors"] == []
        finally:
            context.close()
    from pathlib import Path

    import raincli_server

    js = (Path(raincli_server.__file__).parent / "web" / "static" / "demo-teams.js").read_text("utf-8")
    for hook in ("URLSearchParams", "location.search", "__frame", "__probe", "__labels", "dataset.theme"):
        assert hook not in js, hook


def test_the_pause_with_a_real_mouse_and_the_keyboard(browser, live):  # noqa: F811
    context, page, _ = open_page(browser, live)
    try:
        to_section(page)
        box = page.locator("#adm").bounding_box()
        page.mouse.move(5, 5)
        settle(page)
        assert page.evaluate(OPACITY) == "0"
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)  # a real pointer move
        settle(page)
        assert page.evaluate(OPACITY) == "1"  # shown while hovered
        toggle = page.locator("#adm .adm-pp")
        b = toggle.bounding_box()
        assert b["width"] >= 44 and b["height"] >= 44
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause animation", "false")
        page.mouse.click(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause animation", "true")
        held = frame(page)
        page.clock.run_for(1500)
        assert frame(page) == held  # paused
        page.mouse.move(5, 5, steps=4)
        settle(page)
        assert page.evaluate(OPACITY) == "0"  # gone again once the pointer leaves
        # By keyboard: Tab reaches it and shows it; Enter resumes.
        for _ in range(60):
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.classList.contains('adm-pp')"):
                break
        else:
            pytest.fail("the animation's pause is not reachable by Tab")
        settle(page)
        assert page.evaluate(OPACITY) == "1"
        page.keyboard.press("Enter")
        assert toggle.get_attribute("aria-pressed") == "false"
        page.clock.run_for(1500)
        assert frame(page) != held
    finally:
        context.close()


def test_the_pause_on_a_touch_screen(browser, live):  # noqa: F811
    """As the homepage demo (review F1): with no hover, a tap on the animation shows the pause until a tap elsewhere;
    nothing at rest; the pause works."""
    context, page, _ = open_page(browser, live, size="phone", touch=True)
    try:
        assert page.evaluate("matchMedia('(hover: none)').matches")
        to_section(page)
        assert page.evaluate(OPACITY) == "0"
        box = page.locator("#adm").bounding_box()
        page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + 200)
        settle(page)
        page.clock.run_for(1200)
        page.wait_for_timeout(900)  # longer than a hover flash, in real time
        assert page.evaluate(OPACITY) == "1"  # shown, and it stays
        b = page.locator("#adm .adm-pp").bounding_box()
        page.touchscreen.tap(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        assert page.get_attribute("#adm .adm-pp", "aria-pressed") == "true"
        held = frame(page)
        page.clock.run_for(1500)
        assert frame(page) == held
        page.evaluate("document.getElementById('hero-title').scrollIntoView({block: 'center', behavior: 'instant'})")
        hero = page.locator("#hero-title").bounding_box()
        page.touchscreen.tap(hero["x"] + 20, hero["y"] + 10)  # elsewhere on the page
        settle(page)
        assert page.evaluate(OPACITY) == "0"
        assert page.get_attribute("#adm .adm-pp", "aria-pressed") == "true"  # hiding it doesn't resume
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_reduced_motion_holds_the_composed_end_state(browser, live, scheme):  # noqa: F811
    context, page, log = open_page(browser, live, motion="reduce", scheme=scheme)
    try:
        to_section(page)
        still = frame(page)
        page.clock.run_for(3000)
        assert frame(page) == still  # no motion
        assert page.evaluate("document.getElementById('adm').classList.contains('is-still')")
        assert not page.is_visible("#adm .adm-pp")  # no pause needed
        assert log["console"] == [] and log["errors"] == []
    finally:
        context.close()


def test_without_javascript_there_is_no_dead_control(browser, live):  # noqa: F811
    context = browser.new_context(viewport={"width": 1440, "height": 900}, java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(live + "/", wait_until="load")
        assert page.evaluate("getComputedStyle(document.querySelector('#adm .adm-pp')).display") == "none"
        assert "Maya and Jon" in page.text_content("#adm .adm-sr")
    finally:
        context.close()


def test_without_canvas_filter_safari_it_stays_legible(browser, live):  # noqa: F811
    """Safari has no Canvas 2D filter: the blur-ins fall back to their fade and rise. Forced off here, it plays with no
    error, and once each blur has settled the frame is the same as with blur."""
    shots = []
    for init in (None, NO_FILTER):
        context, page, log = open_page(browser, live, init=init)
        try:
            assert page.evaluate("document.getElementById('adm').classList.contains('adm-no-blur')") is (init is not None)
            to_section(page)
            page.clock.run_for(600)  # mid blur-in of the title's first words
            mid = page.evaluate(CANVAS)
            page.clock.run_for(1900)  # the title has settled (no blur left)
            shots.append((mid, frame(page)))
            assert log["console"] == [] and log["errors"] == [], log
        finally:
            context.close()
    (blur_mid, blur_done), (plain_mid, plain_done) = shots
    assert blur_done == plain_done  # legible and identical once settled
    assert blur_mid != plain_mid  # the fallback really drew without blur mid-reveal


@pytest.mark.parametrize("width", [390, 700, 960, 1180, 1440, 1920])
def test_the_stage_height_matches_the_drafts_formula_without_inline_style(browser, live, width):  # noqa: F811
    """The draft set the stage height from script; it is CSS here (no style attribute), to the same formula."""
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    try:
        page.goto(live + "/", wait_until="load")
        page.wait_for_function("() => document.documentElement.dataset.ready === '1'")
        h = page.evaluate("document.querySelector('#adm .adm-stage').getBoundingClientRect().height")
        w = page.evaluate("document.getElementById('adm').clientWidth")
        want = min(max(w * 720 / 1180, 520), 860) if w >= 960 else 800 * min(w / 390, 1.5)
        assert abs(h - want) <= 1, (width, h, want)
        assert page.locator("[style]").count() == 0
    finally:
        context.close()
