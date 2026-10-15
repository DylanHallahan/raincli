"""The homepage (concept C "Paper"; it replaced website Phase B) in headless Chromium: no CSP violations or console errors in either theme, no
requests to other origins, the anchors, the demo's autoplay rules (on screen and visible only), Pause/Restart by
keyboard, the reduced-motion final frame, the phone layout, and accessibility checks done in the page (Lighthouse
and axe are not test dependencies): WCAG AA contrast of every visible text, accessible names, landmarks, one h1,
heading order and 44 px tap targets.

Screenshots go to ``RAINCLI_GUI_ARTIFACTS`` or the test's temporary directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from .test_app_mode_gui import browser, live  # noqa: F401 - fixtures

pytest.importorskip("playwright.sync_api")

SIZES = {"desktop": (1366, 768), "phone": (390, 844)}

# The demo's state, from the Web Animations API: whether any animation is running, and the first one's time.
ANIMATIONS = """() => {
    const all = document.querySelector('[data-demo]').getAnimations({subtree: true});
    return {count: all.length, running: all.filter((a) => a.playState === 'running').length,
            time: all.length ? all[0].currentTime : null, state: document.querySelector('[data-demo]').dataset.demoState};
}"""

# Seek the demo to a time and hold it there (null: let it play on).
SEEK = """(ms) => document.querySelector('[data-demo]').getAnimations({subtree: true}).forEach((a) => {
    if (ms === null) { a.play(); } else { a.pause(); a.currentTime = ms; } })"""

# WCAG AA contrast for every element with its own visible text (opacity 1 and not clipped), against the first
# opaque background up the tree.
CONTRAST = """() => {
    const parse = (c) => { const m = c.match(/[\\d.]+/g).map(Number); return {r: m[0], g: m[1], b: m[2], a: m.length > 3 ? m[3] : 1}; };
    const lum = ({r, g, b}) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; })
        .reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
    const bg = (el) => { for (; el; el = el.parentElement) { const c = parse(getComputedStyle(el).backgroundColor); if (c.a >= 0.99) return c; } return {r: 255, g: 255, b: 255, a: 1}; };
    const visible = (el) => { for (let e = el; e; e = e.parentElement) { const s = getComputedStyle(e); if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) < 1) return false; } return true; };
    const out = [];
    for (const el of document.body.querySelectorAll('*')) {
        if (!Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
        if (el.closest('.sr-only') || !visible(el)) continue;
        const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
        const s = getComputedStyle(el), fg = parse(s.color), b = bg(el);
        const a = fg.a, mix = {r: fg.r * a + b.r * (1 - a), g: fg.g * a + b.g * (1 - a), b: fg.b * a + b.b * (1 - a)};
        const [l1, l2] = [lum(mix), lum(b)].sort((x, y) => y - x), ratio = (l1 + 0.05) / (l2 + 0.05);
        const size = parseFloat(s.fontSize), large = size >= 24 || (size >= 18.66 && Number(s.fontWeight) >= 700);
        if (ratio < (large ? 3 : 4.5)) out.push(`${el.tagName}.${el.className}: ${ratio.toFixed(2)} "${el.textContent.trim().slice(0, 40)}"`);
    }
    return out;
}"""

# Names, landmarks, headings and tap targets.
STRUCTURE = """() => {
    const problems = [];
    const name = (el) => (el.getAttribute('aria-label') || el.textContent || '').trim();
    for (const el of document.querySelectorAll('a[href], button, summary')) {
        if (!name(el)) problems.push('no name: ' + el.outerHTML.slice(0, 80));
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        const inline = el.closest('p, li, pre') && s.display === 'inline';  // links inside a sentence are exempt (WCAG 2.5.8)
        if (r.width && r.height && !inline && !el.closest('[aria-hidden="true"]') && !el.matches('.skip-link') && (r.height < 44 || r.width < 44))
            problems.push(`small target ${Math.round(r.width)}x${Math.round(r.height)}: ${name(el).slice(0, 30)}`);
    }
    if (document.documentElement.lang !== 'en') problems.push('lang');
    if (document.querySelectorAll('h1').length !== 1) problems.push('h1 count');
    let last = 1;
    for (const h of document.querySelectorAll('main h1, main h2, main h3, main h4')) {
        const n = Number(h.tagName[1]); if (n > last + 1) problems.push('heading skip at ' + h.textContent); last = n;
    }
    for (const sel of ['header', 'main', 'footer', 'nav']) if (!document.querySelector(sel)) problems.push('no ' + sel);
    for (const s of document.querySelectorAll('section')) if (!s.getAttribute('aria-label') && !s.getAttribute('aria-labelledby')) problems.push('unnamed section ' + s.id);
    for (const svg of document.querySelectorAll('svg')) if (svg.getAttribute('aria-hidden') !== 'true' && !svg.closest('[aria-hidden="true"]')) problems.push('svg not hidden');
    const ids = Array.from(document.querySelectorAll('[id]'), (e) => e.id);
    if (new Set(ids).size !== ids.length) problems.push('duplicate id');
    return problems;
}"""


def reveal_all(page):
    """Every section revealed (they fade in as they reach the screen), so the checks see the whole page."""
    page.evaluate("document.querySelectorAll('.rv').forEach((e) => e.classList.add('in'))")
    page.wait_for_timeout(900)


@pytest.fixture
def artifacts(tmp_path) -> Path:
    path = Path(os.environ.get("RAINCLI_GUI_ARTIFACTS") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_home(browser, live, *, size="desktop", scheme="light", motion="no-preference"):
    width, height = SIZES[size]
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                  reduced_motion=motion, device_scale_factor=1)
    page = context.new_page()
    log = {"console": [], "requests": [], "errors": []}
    page.on("console", lambda m: log["console"].append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: log["errors"].append(str(e)))
    page.on("request", lambda r: log["requests"].append(r.url))
    page.goto(live + "/", wait_until="load")
    return context, page, log


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_no_csp_violations_no_other_origins_and_the_layout(browser, live, artifacts, size, scheme):
    context, page, log = open_home(browser, live, size=size, scheme=scheme)
    try:
        page.wait_for_timeout(300)
        # Screenshots at the spec's moments of the 16 s loop (seeked with the Web Animations API, then held there),
        # and the whole page.
        for name, ms in (("t01.0", 1000), ("t03.5", 3500), ("t06.0", 6000), ("t09.6", 9600), ("t13.0", 13000)):
            page.evaluate(SEEK, ms)
            page.screenshot(path=str(artifacts / f"home-{size}-{scheme}-{name}.png"))
        page.screenshot(path=str(artifacts / f"home-{size}-{scheme}-full.png"), full_page=True)
        page.evaluate(SEEK, None)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(200)
        assert log["console"] == [] and log["errors"] == [], log  # a CSP violation is a console error
        origin = urlparse(live).netloc
        assert log["requests"] and all(urlparse(u).netloc == origin for u in log["requests"]), log["requests"]
        fonts = sorted({urlparse(u).path for u in log["requests"] if "/fonts/" in urlparse(u).path})
        assert fonts == ["/static/fonts/jetbrains-mono-regular-v2304.woff2",
                         "/static/fonts/noto-serif-display-light-italic-v2009.woff2",
                         "/static/fonts/noto-serif-display-light-v2009.woff2"], fonts  # self-hosted only
        assert not any(urlparse(u).path.endswith((".png", ".jpg")) for u in log["requests"])
        # No horizontal scroll, and the theme really is the requested one.
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg == ("rgb(243, 238, 228)" if scheme == "light" else "rgb(19, 18, 15)"), bg  # paper, ink
        page.evaluate("window.scrollTo(0, 0)")
        if size == "desktop":
            # Above the fold at 1366x768: the headline in 3-4 lines, the subline, both buttons and the app window.
            fold = page.evaluate("""() => {
                const h1 = document.querySelector('.hero h1'), r = (s) => document.querySelector(s).getBoundingClientRect();
                return {lines: Math.round(h1.getBoundingClientRect().height / parseFloat(getComputedStyle(h1).lineHeight)),
                        sub: r('.hero .sub').bottom, actions: r('.hero .actions').bottom, win: r('.hero .win').top};
            }""")
            assert 3 <= fold["lines"] <= 4 and fold["sub"] < 768 and fold["actions"] < 768 - 24, fold
            assert fold["win"] < 768 - 200, fold
            assert page.is_visible(".nav") and not page.is_visible(".menu")
        else:
            assert not page.is_visible(".nav") and page.is_visible(".menu")
            assert page.evaluate("document.querySelector('.hero .win').getBoundingClientRect().height") == 430
            assert not page.is_visible(".demo .side")
            gutter = page.evaluate("document.querySelector('#how h2').getBoundingClientRect().left")
            assert gutter == 16, gutter
        reveal_all(page)
        assert page.evaluate(CONTRAST) == []
        assert page.evaluate(STRUCTURE) == []
        # The header gets its border once the page scrolls.
        assert "scrolled" not in page.get_attribute("[data-site-header]", "class")
        page.mouse.wheel(0, 600)
        page.wait_for_function("() => document.querySelector('[data-site-header]').classList.contains('scrolled')")
    finally:
        context.close()


def test_the_anchors_resolve_and_the_nav_scrolls_to_them(browser, live):
    context, page, _ = open_home(browser, live, motion="reduce")
    try:
        for ident in ("how", "cli", "security", "install"):
            page.click(f".nav a[href='#{ident}']")
            page.wait_for_function(f"() => Math.abs(document.getElementById('{ident}').getBoundingClientRect().top) < 120")
        # Request access is an email link now (variant B): it opens the mail client and scrolls nowhere.
        assert page.locator(".hero a.btn.primary").get_attribute("href") == "mailto:dylan@dylanhallahan.com"
        assert page.locator(".hero a[href='#closing']").count() == 0
        for ident in ("how", "install", "security", "closing"):
            page.goto(f"{live}/#{ident}")
            page.wait_for_timeout(100)
            top = page.evaluate(f"document.getElementById('{ident}').getBoundingClientRect().top")
            if ident == "closing":  # the last band: the page is scrolled to the end with it in view
                assert top < 768 and page.evaluate("innerHeight + scrollY >= document.body.scrollHeight - 1")
            else:
                assert top < 120, ident
    finally:
        context.close()


def test_the_phone_menu_opens_and_reaches_the_anchors(browser, live):
    context, page, _ = open_home(browser, live, size="phone", motion="reduce")
    try:
        page.click(".menu summary")
        page.click(".menu-list a[href='#security']")
        page.wait_for_function("() => Math.abs(document.getElementById('security').getBoundingClientRect().top) < 120")
        assert page.evaluate("document.querySelector('.menu').open") is False
    finally:
        context.close()


def test_the_demo_plays_only_on_screen_and_while_visible(browser, live):
    context, page, _ = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        state = page.evaluate(ANIMATIONS)
        assert state["count"] > 0 and state["running"] == state["count"]
        # Scrolled away: paused.
        page.evaluate("document.getElementById('security').scrollIntoView()")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'waiting'")
        held = page.evaluate(ANIMATIONS)
        assert held["running"] == 0
        page.wait_for_timeout(400)
        assert page.evaluate(ANIMATIONS)["time"] == held["time"]  # the clock really stopped
        # Back on screen: playing again.
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        # The tab hidden: paused (headless Chromium never hides a tab, so the page is told it is hidden).
        page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
                                 document.dispatchEvent(new Event('visibilitychange')); }""")
        assert page.evaluate(ANIMATIONS)["running"] == 0
        page.evaluate("""() => { Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
                                 document.dispatchEvent(new Event('visibilitychange')); }""")
        assert page.evaluate(ANIMATIONS)["running"] > 0
        assert page.locator("[style]").count() == 0  # pausing is a class, never an inline style
    finally:
        context.close()


def test_the_demo_just_plays_with_no_controls_or_captions(browser, live):  # noqa: F811
    """Pass 2a: no Pause, Restart, progress bar or captions; it plays by itself, starting on the finished thread,
    and keeps the screen-reader description."""
    context, page, _ = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        demo = page.locator("[data-demo]")
        assert demo.locator(".ctrl, .prog, .caps, [class^='cap']").count() == 0
        assert demo.locator("button").count() == 1  # only the hidden-at-rest pause (WCAG 2.2.2)
        assert page.evaluate("getComputedStyle(document.querySelector('.demo-toggle')).opacity") == "0"
        assert "Maya's agent sends a question to Jon." in demo.locator("figcaption.sr-only").text_content()
        state = page.evaluate(ANIMATIONS)
        assert state["running"] == state["count"] > 0 and state["time"] >= 11400  # the finished thread first
        assert page.evaluate("getComputedStyle(document.querySelector('.win')).opacity") == "1"
    finally:
        context.close()


def test_the_demo_pause_is_hidden_at_rest_and_works_by_keyboard(browser, live):  # noqa: F811
    """WCAG 2.2.2 with no visible chrome: the icon-only toggle is invisible at rest, Tab reaches it (and shows it), it
    pauses and resumes, its name stays "Pause demo" while aria-pressed follows, and scrolling away and back doesn't
    resume a pause."""
    context, page, _ = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        toggle = page.locator("[data-demo-toggle]")
        opacity = "getComputedStyle(document.querySelector('[data-demo-toggle]')).opacity"
        assert page.evaluate(opacity) == "0"  # nothing shows at rest
        box = toggle.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        for _ in range(40):
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.hasAttribute('data-demo-toggle')"):
                break
        else:
            pytest.fail("the demo's pause is not reachable by Tab")
        page.wait_for_timeout(250)
        assert page.evaluate(opacity) == "1"
        ring = page.evaluate("getComputedStyle(document.activeElement, '::before').boxShadow")
        assert "2px" in ring, ring  # the visible focus ring, inside the title bar
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause demo", "false")
        page.keyboard.press("Enter")
        # Review F2: the name stays; aria-pressed (true = paused) carries the state.
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause demo", "true")
        assert page.evaluate(ANIMATIONS)["running"] == 0
        assert page.get_attribute("[data-demo]", "data-demo-state") == "paused"
        page.evaluate("document.getElementById('security').scrollIntoView()")
        page.wait_for_timeout(200)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        assert page.evaluate(ANIMATIONS)["running"] == 0  # still paused
        toggle.focus()
        page.keyboard.press("Space")
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause demo", "false")
        assert page.evaluate(ANIMATIONS)["running"] > 0
        page.evaluate("document.activeElement.blur()")
        page.mouse.move(5, 760)
        page.wait_for_timeout(250)
        assert page.evaluate(opacity) == "0"  # hidden again once it loses focus
        page.hover(".hero .win")
        page.wait_for_timeout(250)
        assert page.evaluate(opacity) == "1"  # shown while the window is hovered
    finally:
        context.close()


def test_the_demo_pause_on_a_touch_screen(browser, live):  # noqa: F811
    """Main's decision on review F1: with no hover (a phone), nothing shows at rest; a tap on the demo window shows
    the pause and it stays (focusable) until the demo stops or a tap elsewhere; the pause works."""
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = context.new_page()
    try:
        page.goto(live + "/", wait_until="load")
        assert page.evaluate("matchMedia('(hover: none)').matches")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        opacity = "getComputedStyle(document.querySelector('[data-demo-toggle]')).opacity"
        assert page.evaluate(opacity) == "0"  # invisible at rest
        page.tap(".hero .win", position={"x": 150, "y": 220})
        page.wait_for_timeout(600)  # longer than a hover flash
        assert page.evaluate(opacity) == "1"  # shown, and it stays
        page.wait_for_timeout(600)
        assert page.evaluate(opacity) == "1"
        page.tap("[data-demo-toggle]")
        toggle = page.locator("[data-demo-toggle]")
        assert (toggle.get_attribute("aria-label"), toggle.get_attribute("aria-pressed")) == ("Pause demo", "true")
        assert page.evaluate(ANIMATIONS)["running"] == 0
        assert page.evaluate(opacity) == "1"
        page.tap("#hero-title")  # elsewhere on the page
        page.wait_for_timeout(400)
        assert page.evaluate(opacity) == "0"
        assert page.evaluate(ANIMATIONS)["running"] == 0  # hiding it doesn't resume a pause
    finally:
        context.close()


def test_without_javascript_there_is_no_pause_button(browser, live):  # noqa: F811
    """Review F5: the toggle is rendered hidden and only the script reveals it, so no JavaScript, no dead control."""
    context = browser.new_context(viewport={"width": 1366, "height": 768}, java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(live + "/", wait_until="load")
        assert page.locator("[data-demo-toggle]").count() == 1
        assert not page.is_visible("[data-demo-toggle]")
        assert page.evaluate("getComputedStyle(document.querySelector('[data-demo-toggle]')).display") == "none"
    finally:
        context.close()


def test_when_the_demo_stops_focus_moves_from_the_pause_to_the_demo(browser, live):  # noqa: F811
    """Review F5: the pause goes when the demo stops; if it had focus, focus moves to the demo figure."""
    context, page, _ = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        page.locator("[data-demo-toggle]").focus()
        page.evaluate("""() => document.querySelector('[data-demo]').getAnimations({subtree: true})
            .forEach((a) => { a.currentTime = 16000 + 16000 + 12600; })""")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'done'",
                               timeout=5000)
        assert page.evaluate("document.activeElement === document.querySelector('[data-demo]')")
        assert page.get_attribute("[data-demo]", "tabindex") == "-1"
        assert not page.is_visible("[data-demo-toggle]")
    finally:
        context.close()


# The finished frame: both messages, the file and every state lit, Jon ready.
FINISHED = """() => ['.win', '.m1', '.m2', '.file', '.p-ready', '.st-stored', '.st-received', '.st-submitted',
    '.st-replied'].map((s) => getComputedStyle(document.querySelector(s)).opacity).concat(
    [getComputedStyle(document.querySelector('.p-busy')).opacity])"""

# Move every animation's clock on (keeps it playing), as if that much time had passed.
JUMP = """(ms) => document.querySelector('[data-demo]').getAnimations({subtree: true}).forEach((a) => {
    a.currentTime = ms; })"""


def test_the_demo_stops_on_the_finished_frame_after_two_plays(browser, live):  # noqa: F811
    """Main's decision (pass 2a): two plays, then it holds on the finished frame, with no controls. The clock is the
    animations' own, so the off-screen pause delays the stop; the stop happens at 13 s into the second full play
    (the first join at 11.4 s is the tail of a loop, not a play)."""
    context, page, log = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        # The second play, part way: still running.
        page.evaluate(JUMP, 16000 + 16000 + 6000)
        page.wait_for_timeout(500)
        assert page.evaluate(ANIMATIONS)["running"] > 0
        assert page.get_attribute("[data-demo]", "data-demo-state") == "playing"
        # Scrolled away (paused), with the clock just before the stop: it can't reach the stop point.
        page.evaluate("document.getElementById('security').scrollIntoView()")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'waiting'")
        page.evaluate(JUMP, 16000 + 16000 + 12800)  # setting the time of a paused animation keeps it paused
        page.wait_for_timeout(1000)
        assert page.get_attribute("[data-demo]", "data-demo-state") == "waiting"
        assert page.evaluate(ANIMATIONS)["running"] == 0
        # Back on screen: it plays the last moment and stops, holding the finished frame with nothing animating.
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'done'",
                               timeout=5000)
        assert page.evaluate(ANIMATIONS)["count"] == 0
        assert page.evaluate(FINISHED) == ["1"] * 9 + ["0"]
        # It stays stopped, scrolled away and back.
        page.evaluate("document.getElementById('security').scrollIntoView()")
        page.wait_for_timeout(300)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)
        assert page.evaluate(ANIMATIONS)["count"] == 0
        assert page.get_attribute("[data-demo]", "data-demo-state") == "done"
        assert not page.is_visible("[data-demo-toggle]")  # nothing left to pause
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_reduced_motion_shows_the_final_frame(browser, live, artifacts, scheme):
    context, page, log = open_home(browser, live, motion="reduce", scheme=scheme)
    try:
        page.wait_for_timeout(200)
        assert page.evaluate(ANIMATIONS)["count"] == 0
        assert page.get_attribute("[data-demo]", "data-demo-state") is None  # the script never starts it
        assert not page.is_visible("[data-demo-toggle]")  # no pause needed: nothing moves
        assert page.evaluate(FINISHED) == ["1"] * 9 + ["0"]  # the finished frame, straight away
        assert page.locator(".b-scene, .demo .ctrl, .demo .caps").count() == 0
        frame = page.evaluate("""() => ['.win', '.m1', '.m2', '.file', '.p-ready']
            .map((s) => getComputedStyle(document.querySelector(s)).opacity)""")
        assert frame == ["1"] * 5
        assert page.evaluate("getComputedStyle(document.querySelector('.p-busy')).opacity") == "0"  # ready
        lit = page.evaluate("Array.from(document.querySelectorAll('.states span'), (e) => getComputedStyle(e).color)")
        ok = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--ok').trim()")
        assert len(set(lit)) == 1 and ok  # every state lit in the same --ok colour
        # The demo's final frame passes contrast too (states in green, the code chips, the file line); under
        # reduced motion every section is shown without its fade.
        assert page.evaluate("document.querySelectorAll('.rv').length") > 20
        assert page.evaluate("document.documentElement.classList.contains('js')") is False
        assert page.evaluate(CONTRAST) == []
        page.locator("[data-demo]").screenshot(path=str(artifacts / f"demo-{scheme}-reduced-motion.png"))
        if scheme == "light":
            page.screenshot(path=str(artifacts / "home-desktop-light-reduced-motion.png"))
            page.screenshot(path=str(artifacts / "home-desktop-light-reduced-motion-full.png"), full_page=True)
        assert log["console"] == [] and log["errors"] == []
    finally:
        context.close()


# The spec's timeline (demo-app-only): what is on stage at each moment of the 16 s loop.
FRAME = """() => {
    const op = (s) => Number(getComputedStyle(document.querySelector(s)).opacity);
    const lit = (s) => getComputedStyle(document.querySelector(s)).color === getComputedStyle(document.querySelector('.st-stored')).color
        && op(s) > 0.9;
    return {app: op('.win'), m1: op('.m1'), m2: op('.m2'), file: op('.file'),
            busy: op('.p-busy'), ready: op('.p-ready'), stored: op('.st-stored'), received: op('.st-received'),
            submitted: op('.st-submitted'), replied: op('.st-replied')};
}"""
TIMELINE = [  # (ms, expectations)
    (1000, {"busy": 0, "m2": 0}),
    (3500, {"busy": 0, "stored": 1, "received": 1, "m2": 0}),
    (6000, {"busy": 1, "ready": 0, "submitted": 1, "m2": 0}),
    (9600, {"busy": 0, "ready": 1, "m2": 1, "file": 1, "replied": 1}),
    (13000, {"busy": 0, "m1": 1, "m2": 1, "file": 1, "replied": 1}),
]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_timeline_matches_the_spec(browser, live, artifacts, scheme):
    """App only, 16 s: the app is on screen for the whole loop; messages, states and presence at the
    spec's moments; busy only between submitted (4.4 s) and the reply (7.8 s)."""
    context, page, log = open_home(browser, live, scheme=scheme)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        page.evaluate("document.querySelector('[data-demo]').scrollIntoView({block: 'center'})")
        for ms, want in TIMELINE:
            page.evaluate(SEEK, ms)
            f = page.evaluate(FRAME)
            assert f["app"] == 1, (ms, f)
            for key, value in want.items():
                assert round(f[key]) == value, (ms, key, f)
            page.locator("[data-demo]").screenshot(path=str(artifacts / f"demo-{scheme}-t{ms / 1000:04.1f}s.png"))
        # Busy only between submitted and the reply.
        for ms in range(0, 16000, 250):
            page.evaluate(SEEK, ms)
            busy = page.evaluate("Number(getComputedStyle(document.querySelector('.p-busy')).opacity)")
            if busy > 0.5:
                assert 4400 <= ms <= 7800, ms
        page.evaluate(SEEK, None)
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_phone_demo(browser, live, artifacts, scheme):
    context, page, log = open_home(browser, live, size="phone", scheme=scheme)
    try:
        page.evaluate("document.querySelector('[data-demo]').scrollIntoView({block: 'center'})")
        page.evaluate(SEEK, 9600)
        assert page.evaluate("getComputedStyle(document.querySelector('.side')).display") == "none"
        box = page.evaluate("document.querySelector('.demo .win').getBoundingClientRect().right")
        assert box <= 390
        page.locator("[data-demo]").screenshot(path=str(artifacts / f"demo-phone-{scheme}-t09.6s.png"))
        page.evaluate(SEEK, None)
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()



@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_request_access_links_in_the_hero_and_closing(browser, live, artifacts, size, scheme):
    """Request access (header on desktop, hero, closing) is a mailto link; no console or CSP errors."""
    context, page, log = open_home(browser, live, size=size, scheme=scheme, motion="reduce")
    try:
        for selector in (".hero a.btn.primary", "#closing a.btn.primary") + ((".top-bar a.btn.primary",) if size == "desktop" else ()):
            assert page.locator(selector).get_attribute("href") == "mailto:dylan@dylanhallahan.com"
        assert page.locator("#closing p", has_text="Already invited?").count() == 1
        page.locator(".hero").screenshot(path=str(artifacts / f"request-access-hero-{size}-{scheme}.png"))
        page.locator("#closing").screenshot(path=str(artifacts / f"request-access-closing-{size}-{scheme}.png"))
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_sign_in_page_matches_in_blue(browser, live, artifacts, scheme):  # noqa: F811
    """/login takes the homepage's brand blue (concept C) for its accent and button; contrast holds, no errors."""
    context = browser.new_context(viewport={"width": 1366, "height": 768}, color_scheme=scheme)
    page = context.new_page()
    log = []
    page.on("console", lambda m: log.append(m.text) if m.type in ("error", "warning") else None)
    try:
        page.goto(live + "/login", wait_until="networkidle")
        button = page.evaluate("getComputedStyle(document.querySelector('.auth-card .btn')).backgroundColor")
        assert button == ("rgb(31, 95, 209)" if scheme == "light" else "rgb(122, 167, 255)"), button
        statement = " ".join(page.text_content(".auth-statement").split())
        assert statement == "Agent Direct Messaging for your team"  # pass 2a: the hero's line (marketing)
        assert page.text_content(".auth-statement em") == "for your team"
        accent = page.evaluate("getComputedStyle(document.querySelector('.auth-statement em')).color")
        assert accent == button
        assert page.evaluate(CONTRAST) == []
        page.screenshot(path=str(artifacts / f"login-{scheme}.png"))
        assert log == [], log
    finally:
        context.close()


@pytest.mark.parametrize("path", ["/login"])
def test_the_sign_in_page_fits_a_phone(browser, live, artifacts, path):  # noqa: F811
    """390 px: one column (headline above the card), no horizontal scroll, even with the email field focused."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        page.goto(live + path, wait_until="networkidle")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert page.evaluate("window.scrollX") == 0
        card = page.evaluate("document.querySelector('.auth-card').getBoundingClientRect().toJSON()")
        intro = page.evaluate("document.querySelector('.auth-intro').getBoundingClientRect().toJSON()")
        assert card["left"] >= 0 and card["right"] <= 390 and intro["bottom"] <= card["top"], (intro, card)
        page.screenshot(path=str(artifacts / "login-phone.png"))
    finally:
        context.close()
