"""Website Phase B, the homepage in headless Chromium: no CSP violations or console errors in either theme, no
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
        assert not any("/fonts/" in u or u.endswith((".woff", ".woff2", ".png", ".jpg")) for u in log["requests"])
        # No horizontal scroll, and the theme really is the requested one.
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg == ("rgb(255, 255, 255)" if scheme == "light" else "rgb(33, 33, 33)"), bg
        page.evaluate("window.scrollTo(0, 0)")
        if size == "desktop":
            # Above the fold at 1366x768: the hero, the first caption and the top of the demo frame.
            fold = page.evaluate("""() => ({
                cap: document.querySelector('.cap1').getBoundingClientRect().bottom,
                stage: document.querySelector('.stage').getBoundingClientRect().top,
                small: document.querySelector('.hero-small').getBoundingClientRect().bottom})""")
            assert fold["small"] < 768 and fold["cap"] < 768 and fold["stage"] < 768 - 40, fold
            assert page.is_visible(".site-nav") and not page.is_visible(".site-menu")
        else:
            assert not page.is_visible(".site-nav") and page.is_visible(".site-menu")
            assert page.evaluate("document.querySelector('.stage').getBoundingClientRect().height") == 420
            assert not page.is_visible(".demo .side")
            gutter = page.evaluate("document.querySelector('#how h2').getBoundingClientRect().left")
            assert gutter == 16, gutter
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
        for ident in ("how", "security", "install"):
            page.click(f".site-nav a[href='#{ident}']")
            page.wait_for_function(f"() => Math.abs(document.getElementById('{ident}').getBoundingClientRect().top) < 120")
        # Request access is an email link now (variant B): it opens the mail client and scrolls nowhere.
        assert page.locator(".hero a.site-btn-primary").get_attribute("href") == "mailto:dylan@dylanhallahan.com"
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
        page.click(".site-menu summary")
        page.click(".site-menu-list a[href='#security']")
        page.wait_for_function("() => Math.abs(document.getElementById('security').getBoundingClientRect().top) < 120")
        assert page.evaluate("document.querySelector('.site-menu').open") is False
    finally:
        context.close()


def test_the_demo_plays_only_on_screen_and_while_visible(browser, live):
    context, page, _ = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        state = page.evaluate(ANIMATIONS)
        assert state["count"] > 0 and state["running"] == state["count"]
        # Scrolled away: paused.
        page.evaluate("document.getElementById('faq').scrollIntoView()")
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


def test_pause_and_restart_work_by_keyboard(browser, live):
    context, page, _ = open_home(browser, live)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        for _ in range(40):  # Tab through the page to the Pause button
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement.hasAttribute('data-demo-pause')"):
                break
        else:
            pytest.fail("Pause is not reachable by Tab")
        outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        assert outline == "solid"  # visible focus
        page.keyboard.press("Enter")
        assert page.get_attribute("[data-demo-pause]", "aria-pressed") == "true"
        assert page.text_content("[data-demo-pause]") == "Play"
        assert page.evaluate(ANIMATIONS)["running"] == 0
        # Scrolling away and back doesn't resume a demo the person paused.
        page.evaluate("document.getElementById('faq').scrollIntoView()")
        page.wait_for_timeout(200)
        page.evaluate("document.querySelector('[data-demo]').scrollIntoView({block: 'center'})")
        page.wait_for_timeout(200)
        assert page.evaluate(ANIMATIONS)["running"] == 0
        page.keyboard.press("Space")
        assert page.get_attribute("[data-demo-pause]", "aria-pressed") == "false"
        assert page.evaluate(ANIMATIONS)["running"] > 0
        page.wait_for_timeout(1500)
        before = page.evaluate(ANIMATIONS)["time"]
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.hasAttribute('data-demo-restart')")
        page.keyboard.press("Enter")
        after = page.evaluate(ANIMATIONS)
        assert after["running"] > 0 and after["time"] < before and after["time"] < 500, (before, after)
        # Restarting starts from 0 s with the app already visible (no fade-in of the app frame).
        assert page.evaluate("getComputedStyle(document.querySelector('.a-scene')).opacity") == "1"
        # Restart while paused starts it playing.
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Enter")
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")
        assert page.get_attribute("[data-demo-pause]", "aria-pressed") == "false"
        assert page.evaluate(ANIMATIONS)["running"] > 0
    finally:
        context.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_reduced_motion_shows_the_final_frame(browser, live, artifacts, scheme):
    context, page, log = open_home(browser, live, motion="reduce", scheme=scheme)
    try:
        page.wait_for_timeout(200)
        assert page.evaluate(ANIMATIONS)["count"] == 0
        assert not page.is_visible(".demo .ctrl") and page.locator(".b-scene").count() == 0
        frame = page.evaluate("""() => ['.a-scene', '.m1', '.m2', '.file', '.cap4', '.p-ready']
            .map((s) => getComputedStyle(document.querySelector(s)).opacity)""")
        assert frame == ["1"] * 6
        assert page.evaluate("getComputedStyle(document.querySelector('.cap1')).opacity") == "0"
        assert page.evaluate("getComputedStyle(document.querySelector('.p-busy')).opacity") == "0"  # ready
        lit = page.evaluate("Array.from(document.querySelectorAll('.states span'), (e) => getComputedStyle(e).color)")
        ok = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--rc-ok').trim()")
        assert len(set(lit)) == 1 and ok  # every state lit in the same --rc-ok colour
        # The demo's final frame passes contrast too (states in green, the code chips, the file line).
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
    return {app: op('.a-scene'), caps: [1, 2, 3, 4].map((i) => op('.cap' + i)), m1: op('.m1'), m2: op('.m2'), file: op('.file'),
            busy: op('.p-busy'), ready: op('.p-ready'), stored: op('.st-stored'), received: op('.st-received'),
            submitted: op('.st-submitted'), replied: op('.st-replied')};
}"""
TIMELINE = [  # (ms, the caption shown, expectations)
    (1000, 1, {"busy": 0, "m2": 0}),
    (3500, 2, {"busy": 0, "stored": 1, "received": 1, "m2": 0}),
    (6000, 2, {"busy": 1, "ready": 0, "submitted": 1, "m2": 0}),
    (9600, 3, {"busy": 0, "ready": 1, "m2": 1, "file": 1, "replied": 1}),
    (13000, 4, {"busy": 0, "m1": 1, "m2": 1, "file": 1, "replied": 1}),
]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_timeline_matches_the_spec(browser, live, artifacts, scheme):
    """App only, 16 s: the app is on screen for the whole loop; captions, messages, states and presence at the
    spec's moments; busy only between submitted (4.4 s) and the reply (7.8 s)."""
    context, page, log = open_home(browser, live, scheme=scheme)
    try:
        page.wait_for_function("() => document.querySelector('[data-demo]').dataset.demoState === 'playing'")
        page.evaluate("document.querySelector('[data-demo]').scrollIntoView({block: 'center'})")
        for ms, cap, want in TIMELINE:
            page.evaluate(SEEK, ms)
            f = page.evaluate(FRAME)
            assert f["app"] == 1, (ms, f)
            assert [round(c) for c in f["caps"]] == [int(i == cap) for i in range(1, 5)], (ms, f)
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
        box = page.evaluate("document.querySelector('.demo .stage').getBoundingClientRect().width")
        assert box <= 390
        page.locator("[data-demo]").screenshot(path=str(artifacts / f"demo-phone-{scheme}-t09.6s.png"))
        page.evaluate(SEEK, None)
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()



@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_request_access_links_in_the_hero_and_closing(browser, live, artifacts, size, scheme):
    """Variant B: Request access (hero) and Email us (closing) are mailto links; no console or CSP errors."""
    context, page, log = open_home(browser, live, size=size, scheme=scheme, motion="reduce")
    try:
        for selector in (".hero a.site-btn-primary", "#closing a.site-btn-primary"):
            assert page.locator(selector).get_attribute("href") == "mailto:dylan@dylanhallahan.com"
        assert page.locator("#closing p", has_text="Already invited?").count() == 1
        page.locator(".hero").screenshot(path=str(artifacts / f"request-access-hero-{size}-{scheme}.png"))
        page.locator("#closing").screenshot(path=str(artifacts / f"request-access-closing-{size}-{scheme}.png"))
        assert log["console"] == [] and log["errors"] == [], log
    finally:
        context.close()
