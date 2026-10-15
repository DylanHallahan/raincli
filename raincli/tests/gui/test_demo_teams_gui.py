"""The homepage's first-visit intro (demo-teams v3 "The Gap", round 4) in headless Chromium: a full-viewport dialog
over the page on a first visit only, never under reduced motion; it plays once and fades into the hero; Skip, Esc,
a scroll, a wheel, a touch or a click dismisses it at once and focus returns to the page's top; "Watch the intro"
reopens it (the still under reduced motion). CSP-clean, self-hosted fonts only, first paint not delayed, the draft's
query hooks inert, the Safari no-blur fallback, no line tracing a message.

Time is Playwright's fake clock (it drives requestAnimationFrame and setTimeout), so canvas frames compare exactly.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

SIZES = {"desktop": (1440, 900), "phone": (390, 844)}
OVERLAY = ".adm-intro"
CANVAS = "() => document.querySelector('.adm-intro canvas').toDataURL()"
OPACITY = "() => getComputedStyle(document.querySelector('.adm-intro .adm-pp')).opacity"
STATE = "() => document.querySelector('.adm-intro .adm').dataset.admState"
KEY = "raincli-intro-seen"
# Canvas 2D without filter, as in Safari: assignments are ignored and it reads back undefined.
NO_FILTER = """Object.defineProperty(CanvasRenderingContext2D.prototype, 'filter',
    {configurable: true, get() { return undefined; }, set(v) {}});"""
NO_LOCAL = "Object.defineProperty(window, 'localStorage', {configurable: true, get() { throw new Error('blocked'); }});"
NO_SESSION = "Object.defineProperty(window, 'sessionStorage', {configurable: true, get() { throw new Error('blocked'); }});"


def new_context(browser, *, size="desktop", scheme="light", motion="no-preference", touch=False, init=(), js=True):  # noqa: F811
    width, height = SIZES[size]
    options = dict(viewport={"width": width, "height": height}, color_scheme=scheme, reduced_motion=motion,
                   device_scale_factor=1, java_script_enabled=js)
    if touch:
        options.update(has_touch=True, is_mobile=True)
    context = browser.new_context(**options)
    for script in init:
        context.add_init_script(script)
    return context


def visit(context, live, path="/", *, clock=True):
    page = context.new_page()
    log = {"console": [], "errors": [], "requests": []}
    page.on("console", lambda m: log["console"].append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: log["errors"].append(str(e)))
    page.on("request", lambda r: log["requests"].append(r.url))
    if clock:
        page.clock.install()
    page.goto(live + path, wait_until="load")
    page.wait_for_timeout(250)  # the deferred script has run
    if clock:  # the intro waits for the page's paint, then a frame or two (requestAnimationFrame: the fake clock)
        for _ in range(16):
            page.clock.run_for(100)
            page.wait_for_timeout(20)
            if overlay_count(page):
                break
    return page, log


def ready(page):
    page.wait_for_function("() => document.querySelector('.adm-intro .adm')?.dataset.admReady === '1'")
    page.wait_for_timeout(250)  # IntersectionObserver delivery (real time, not the fake clock)
    page.clock.run_for(100)


def frame(page) -> str:
    return hashlib.sha256(page.evaluate(CANVAS).encode()).hexdigest()


def settle(page):
    """Real time for what the fake clock doesn't drive: CSS opacity transitions."""
    page.wait_for_timeout(350)


def overlay_count(page) -> int:
    return page.locator(OVERLAY).count()


def focus_on_top(page) -> bool:
    return page.evaluate("document.activeElement === document.getElementById('hero-title')")


INERT_CLEARED = "Array.from(document.body.children).every((e) => !e.inert && !e.hasAttribute('aria-hidden'))"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_a_first_visit_opens_the_intro_as_an_accessible_dialog(browser, live, size, scheme):  # noqa: F811
    context = new_context(browser, size=size, scheme=scheme)
    try:
        page, log = visit(context, live)
        ready(page)
        page.clock.run_for(3000)
        dialog = page.locator(OVERLAY)
        assert dialog.count() == 1
        assert (dialog.get_attribute("role"), dialog.get_attribute("aria-modal")) == ("dialog", "true")
        assert dialog.get_attribute("aria-label") == "Intro: Agent Direct Messaging for your team"
        described = page.evaluate("document.getElementById(document.querySelector('.adm-intro')"
                                  ".getAttribute('aria-describedby')).textContent")
        assert "Maya and Jon, two people on the same team" in described  # the screen-reader description
        assert page.evaluate("document.activeElement.matches('[data-intro-skip]')")  # Skip has the initial focus
        skip = page.locator("[data-intro-skip]").bounding_box()
        assert skip["width"] >= 44 and skip["height"] >= 44 and page.is_visible("[data-intro-skip]")
        # The page underneath is inert and hidden from assistive technology.
        assert page.evaluate("""() => Array.from(document.body.children).filter((e) => !e.matches('.adm-intro'))
            .every((e) => e.inert && e.getAttribute('aria-hidden') === 'true')""")
        # Full viewport, and the animation fits inside it.
        box = page.evaluate("""() => { const o = document.querySelector('.adm-intro').getBoundingClientRect(),
            s = document.querySelector('.adm-intro .adm-stage').getBoundingClientRect();
            return [o.width, o.height, innerWidth, innerHeight,
                    s.top >= -1 && s.bottom <= innerHeight + 1 && s.left >= -1 && s.right <= innerWidth + 1]; }""")
        assert box[:2] == box[2:4] and box[4], box
        assert page.evaluate(OPACITY) == "0"  # the pause: nothing visible at rest
        assert log["console"] == [] and log["errors"] == [], log  # a CSP violation is a console error
        origin = urlparse(live).netloc
        assert all(urlparse(u).netloc == origin for u in log["requests"]), log["requests"]  # no Google Fonts
        fonts = {urlparse(u).path.rsplit("/", 1)[-1] for u in log["requests"] if "/fonts/" in u}
        assert {"jetbrains-mono-medium-v2304.woff2", "jetbrains-mono-bold-v2304.woff2"} <= fonts, fonts
        assert page.locator("[style]").count() == 0
    finally:
        context.close()


def test_it_plays_once_fades_into_the_hero_and_is_remembered(browser, live):  # noqa: F811
    context = new_context(browser)
    try:
        page, _ = visit(context, live)
        ready(page)
        assert page.evaluate(f"localStorage.getItem('{KEY}')") == "1"  # remembered as soon as it shows
        hashes = []
        for _ in range(4):
            page.clock.run_for(5000)
            hashes.append(frame(page))
        assert len(set(hashes)) == 4 and page.evaluate(STATE) == "playing"  # it plays
        states = []
        for _ in range(40):  # to the end of the 24 s sequence: it stops there (no loop) and fades
            page.clock.run_for(250)
            state = page.evaluate("document.querySelector('.adm-intro')?.dataset.introState || 'gone'")
            states.append(state)
            if state == "fading":
                break
        assert states[-1] == "fading", states
        assert page.evaluate("document.querySelector('.adm-intro').classList.contains('adm-intro-out')")
        assert page.evaluate(STATE) == "ended"
        assert focus_on_top(page)
        page.clock.run_for(800)
        assert overlay_count(page) == 0  # faded out and removed
        assert page.evaluate("document.documentElement.dataset.introClosed") == "end"
        assert page.evaluate(INERT_CLEARED)
        # A returning visitor lands straight on the page.
        page2, _ = visit(context, live)
        page2.clock.run_for(2000)
        assert overlay_count(page2) == 0
    finally:
        context.close()


def test_without_localstorage_session_storage_remembers_it(browser, live):  # noqa: F811
    context = new_context(browser, init=(NO_LOCAL,))
    try:
        page, log = visit(context, live)
        assert overlay_count(page) == 1  # plays
        assert page.evaluate(f"sessionStorage.getItem('{KEY}')") == "1"
        page.reload(wait_until="load")  # the same tab: the same session
        page.wait_for_timeout(250)
        assert overlay_count(page) == 0  # not again this session
        assert log["errors"] == []
    finally:
        context.close()


def test_with_all_storage_throwing_it_plays_once_and_nothing_breaks(browser, live):  # noqa: F811
    context = new_context(browser, init=(NO_LOCAL, NO_SESSION))
    try:
        page, log = visit(context, live)
        ready(page)
        assert overlay_count(page) == 1
        page.keyboard.press("Escape")
        assert overlay_count(page) == 0
        page.clock.run_for(2000)
        assert overlay_count(page) == 0  # seen for the rest of this page
        page.click("[data-intro-open]")
        assert overlay_count(page) == 1  # and it can still be reopened
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_reduced_motion_has_no_intro_and_the_link_shows_the_still(browser, live, scheme):  # noqa: F811
    context = new_context(browser, motion="reduce", scheme=scheme)
    try:
        page, log = visit(context, live)
        page.clock.run_for(2000)
        assert overlay_count(page) == 0
        assert page.evaluate(f"localStorage.getItem('{KEY}')") is None
        page.click("[data-intro-open]")
        ready(page)
        assert page.evaluate(STATE) == "still"
        assert page.text_content("[data-intro-skip]").strip() == "Close"
        still = frame(page)
        page.clock.run_for(3000)
        assert frame(page) == still  # no motion
        assert not page.is_visible(".adm-intro .adm-pp")
        page.keyboard.press("Escape")
        assert overlay_count(page) == 0 and focus_on_top(page)
        assert log["console"] == [] and log["errors"] == []
    finally:
        context.close()


def _dismiss(page, how):
    if how == "skip":
        page.keyboard.press("Enter")  # Skip has the focus
    elif how == "escape":
        page.keyboard.press("Escape")
    elif how == "scroll":
        page.evaluate("window.scrollBy(0, 200)")
        page.wait_for_timeout(100)
    elif how == "wheel":
        page.mouse.move(600, 400)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(100)
    elif how == "touch":
        page.touchscreen.tap(195, 420)
    elif how == "click":
        page.mouse.click(400, 300)


@pytest.mark.parametrize("how", ["skip", "escape", "scroll", "wheel", "touch", "click"])
def test_each_way_of_dismissing_closes_it_at_once_and_returns_focus(browser, live, how):  # noqa: F811
    context = new_context(browser, size="phone" if how == "touch" else "desktop", touch=how == "touch")
    try:
        page, log = visit(context, live)
        ready(page)
        page.clock.run_for(4000)
        _dismiss(page, how)
        assert overlay_count(page) == 0, how  # instantly: no fade on a dismissal
        assert page.evaluate("document.documentElement.dataset.introClosed") == how
        assert focus_on_top(page), how
        assert page.evaluate(INERT_CLEARED)
        assert log["errors"] == []
    finally:
        context.close()


def test_watch_the_intro_reopens_it(browser, live):  # noqa: F811
    context = new_context(browser)
    try:
        page, _ = visit(context, live)
        ready(page)
        page.keyboard.press("Escape")
        link = page.locator("[data-intro-open]")
        assert link.is_visible() and link.text_content() == "Watch the intro"
        hero = page.locator("#hero").bounding_box()
        box = link.bounding_box()
        assert hero["y"] <= box["y"] <= hero["y"] + hero["height"]  # near the hero
        link.click()
        ready(page)
        assert overlay_count(page) == 1 and page.evaluate(STATE) == "playing"
        assert page.evaluate("document.activeElement.matches('[data-intro-skip]')")
        held = frame(page)
        page.clock.run_for(3000)
        assert frame(page) != held  # it plays again, from the start
    finally:
        context.close()


def test_the_focus_stays_in_the_dialog(browser, live):  # noqa: F811
    context = new_context(browser)
    try:
        page, _ = visit(context, live)
        ready(page)
        seen = set()
        for _ in range(6):
            page.keyboard.press("Tab")
            seen.add(page.evaluate("document.activeElement.matches('[data-intro-skip]') ? 'skip' :"
                                   " document.activeElement.matches('.adm-pp') ? 'pause' : 'other'"))
        assert seen == {"skip", "pause"}
        assert overlay_count(page) == 1  # Tab doesn't dismiss
    finally:
        context.close()


def test_the_pause_with_a_real_mouse_and_the_keyboard(browser, live):  # noqa: F811
    context = new_context(browser)
    try:
        page, _ = visit(context, live)
        ready(page)
        page.mouse.move(5, 890)
        settle(page)
        assert page.evaluate(OPACITY) == "0"
        stage = page.locator(".adm-intro .adm").bounding_box()
        page.mouse.move(stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2, steps=8)  # a real move
        settle(page)
        assert page.evaluate(OPACITY) == "1"  # shown while hovered
        toggle = page.locator(".adm-intro .adm-pp")
        b = toggle.bounding_box()
        assert b["width"] >= 44 and b["height"] >= 44
        page.mouse.click(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        assert overlay_count(page) == 1  # a click on the pause pauses; it doesn't dismiss
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause animation", "true")
        assert page.evaluate(STATE) == "paused"
        held = frame(page)
        page.clock.run_for(1500)
        assert frame(page) == held
        page.keyboard.press("Enter")  # the pause has the focus after the click
        assert toggle.get_attribute("aria-pressed") == "false"
        page.clock.run_for(1500)
        assert frame(page) != held
    finally:
        context.close()


LCP_LOG = """window.__lcp = []; window.__lcpEls = [];
    new PerformanceObserver((l) => l.getEntries().forEach((e) => { if (e.element) window.__lcpEls.push(e.element);
        window.__lcp.push([e.startTime, e.element && e.element.tagName, e.element && (e.element.id || e.element.className)]); }))
        .observe({type: 'largest-contentful-paint', buffered: true});"""


def test_first_paint_and_lcp_are_not_changed_by_the_intro(browser, live):  # noqa: F811
    """The page is all in the HTML and the intro is a deferred script's template, so nothing blocks the first paint;
    the largest contentful paint is the page's own element at about the same time as a returning visit without
    the intro (the canvas is never a candidate)."""
    measured = {}
    returning = f"try {{ localStorage.setItem('{KEY}', '1'); }} catch (e) {{}}"
    for visit_kind, init in (("first", (LCP_LOG,)), ("returning", (LCP_LOG, returning))):
        context = new_context(browser, init=init)  # both with a cold cache
        try:
            page, _ = visit(context, live, clock=False)
            page.wait_for_timeout(3000)
            assert overlay_count(page) == (1 if visit_kind == "first" else 0), visit_kind
            lcp = page.evaluate("window.__lcp")
            fcp = page.evaluate("performance.getEntriesByName('first-contentful-paint')[0].startTime")
            assert lcp and all(tag != "CANVAS" for _, tag, _ in lcp), lcp
            assert page.evaluate("""() => window.__lcpEls.every((e) => !e.closest('.adm-intro'))"""), lcp
            assert page.evaluate("getComputedStyle(document.getElementById('hero-title')).display") != "none"
            measured[visit_kind] = (lcp[-1], fcp)
            if visit_kind == "first":
                shown = page.evaluate("performance.getEntriesByName('intro-shown')[0].startTime")
                assert shown >= lcp[-1][0], (shown, lcp)  # it showed after the page's largest paint
        finally:
            context.close()
    (first_lcp, first_fcp), (back_lcp, back_fcp) = measured["first"], measured["returning"]
    # Which of the page's elements is largest varies run to run with font timing (the hero's headline, the
    # header button, the CLI heading), with or without the intro; it is always the page's own, painted before the
    # intro shows, and no later than a returning visit's.
    assert first_lcp[0] < back_lcp[0] + 500 and first_fcp < back_fcp + 500, measured
    from urllib.request import urlopen
    with urlopen(live + "/") as r:
        html = r.read().decode()
    assert '<h1 class="intro" id="hero-title">' in html and '<template id="adm-intro">' in html
    assert re.search(r'<script src="[^"]*demo-teams\.js[^"]*" defer></script>', html)  # never render-blocking


def test_the_drafts_query_hooks_do_nothing(browser, live):  # noqa: F811
    context = new_context(browser)
    try:
        page, log = visit(context, live, "/?t=5&theme=dark&rec=1")
        ready(page)
        hashes = []
        for _ in range(3):
            page.clock.run_for(2000)
            hashes.append(frame(page))
        assert len(set(hashes)) == 3  # playing, not held on a frame
        assert page.evaluate("() => [document.documentElement.dataset.theme, typeof window.__frame,"
                             " typeof window.__probe, typeof window.__labels]") == [None, "undefined", "undefined",
                                                                                    "undefined"]
        paper = page.evaluate("""() => Array.from(document.querySelector('.adm-intro canvas').getContext('2d')
            .getImageData(4, 4, 1, 1).data.slice(0, 3))""")
        assert paper == [243, 238, 228]  # light, not the forced dark
        assert log["errors"] == []
    finally:
        context.close()
    from pathlib import Path

    import raincli_server

    js = (Path(raincli_server.__file__).parent / "web" / "static" / "demo-teams.js").read_text("utf-8")
    for hook in ("URLSearchParams", "location.search", "__frame", "__probe", "__labels", "dataset.theme"):
        assert hook not in js, hook


def test_without_canvas_filter_safari_it_stays_legible(browser, live):  # noqa: F811
    shots = []
    for init in ((), (NO_FILTER,)):
        context = new_context(browser, init=init)
        try:
            page, log = visit(context, live)
            ready(page)
            no_blur = page.evaluate("document.querySelector('.adm-intro .adm').classList.contains('adm-no-blur')")
            assert no_blur is bool(init)
            page.clock.run_for(600)  # mid blur-in of the title's first words
            mid = page.evaluate(CANVAS)
            page.clock.run_for(1600)  # the title has settled
            shots.append((mid, page.evaluate(CANVAS)))
            assert log["console"] == [] and log["errors"] == [], log
        finally:
            context.close()
    (blur_mid, blur_done), (plain_mid, plain_done) = shots
    assert blur_mid != plain_mid  # mid reveal, the fallback really drew without blur
    context = browser.new_context()
    try:
        page = context.new_page()
        share = page.evaluate(PIXEL_DIFF, [blur_done, plain_done])
        assert share < .002, share  # once the title has settled the two are the same: legible
    finally:
        context.close()


PIXEL_DIFF = """async ([a, b]) => {
    const load = (src) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = src; });
    const [ia, ib] = await Promise.all([load(a), load(b)]);
    const c = document.createElement('canvas'); c.width = ia.width; c.height = ia.height;
    const x = c.getContext('2d');
    x.drawImage(ia, 0, 0); const da = x.getImageData(0, 0, c.width, c.height).data;
    x.clearRect(0, 0, c.width, c.height); x.drawImage(ib, 0, 0); const db = x.getImageData(0, 0, c.width, c.height).data;
    let n = 0;
    for (let k = 0; k < da.length; k += 4)
        if (Math.max(Math.abs(da[k] - db[k]), Math.abs(da[k + 1] - db[k + 1]), Math.abs(da[k + 2] - db[k + 2])) > 24) n++;
    return n / (c.width * c.height);
}"""


RECORD_STROKES = """(() => {
    const strokes = window.__strokes = [], orig = CanvasRenderingContext2D.prototype.stroke;
    CanvasRenderingContext2D.prototype.stroke = function (...a) {
        strokes.push([String(this.strokeStyle), this.lineWidth, this.getLineDash().join(',')]);
        return orig.apply(this, a);
    };
})();"""


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_no_line_traces_a_message_route(browser, live, size):  # noqa: F811
    """Round 4: over the whole play, no blue route stroke (brand blue, 2.4) and no grey dotted trace (dash 0.1/5)."""
    for scheme, blue in (("light", "#1f5fd1"), ("dark", "#7aa7ff")):
        context = new_context(browser, size=size, scheme=scheme, init=(RECORD_STROKES,))
        try:
            page, log = visit(context, live)
            ready(page)
            for _ in range(23):
                page.clock.run_for(1000)
            strokes = page.evaluate("window.__strokes")
            assert len(strokes) > 500
            assert [s for s in strokes if s[0] == blue and abs(s[1] - 2.4) < 1e-6] == []
            assert [s for s in strokes if s[2] in ("0.1,5", "0.10000000149011612,5")] == []
            assert log["errors"] == []
        finally:
            context.close()


def test_without_javascript_nothing_of_the_intro_shows(browser, live):  # noqa: F811
    context = new_context(browser, js=False)
    try:
        page, _ = visit(context, live, clock=False)
        assert overlay_count(page) == 0
        assert not page.is_visible("[data-intro-open]")
        assert page.is_visible("#hero-title")
    finally:
        context.close()


def test_someone_already_using_the_page_is_not_interrupted(browser, live):  # noqa: F811
    """If the visitor scrolls before the intro would show (it waits for the page's paint), it doesn't open."""
    context = new_context(browser)
    try:
        page = context.new_page()
        page.clock.install()
        page.goto(live + "/", wait_until="domcontentloaded")
        page.evaluate("window.scrollBy(0, 300)")
        for _ in range(16):
            page.clock.run_for(100)
            page.wait_for_timeout(20)
        assert overlay_count(page) == 0
        assert page.evaluate(f"localStorage.getItem('{KEY}')") == "1"  # still counted as seen
        assert page.is_visible("[data-intro-open]")
    finally:
        context.close()
