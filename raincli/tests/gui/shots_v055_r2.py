"""v0.5.5 screenshot 4, send in place (R2, protocol §20.2), on the v0.5.5 seed (``shots_v055.seed``).

Not collected by default (no ``test_`` prefix). Run explicitly::

    RAINCLI_SHOTS_PHASE=after RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_v055_r2.py

Maya is in her api-agent conversation with Jon after his report. She sends a follow-up with Ctrl+Enter: it lands
in the thread at once with its trail, the composer is empty, the list preview has moved, and no banner shows
(``4-send-in-place``). A second frame shows a failed send: one inline line, with the draft kept
(``4-send-failed``). Shots 1 to 3 (the picker, R1) are frozen. Files are
``<phase>-4-<screen>-<light|dark>-<WxH>.png``.
"""

from __future__ import annotations

import pytest

from .shots_v054 import BIG, PHASE, Camera, browser_pw, converse, jon_replies, live, settle  # noqa: F401
from .shots_v055 import OUT, seed  # noqa: F401

pytest.importorskip("playwright.sync_api")

FOLLOW_UP = "Thanks. Can you open a PR with the total_cents fix and link it here?"
FAILED = "And can you add a test for the receipt total?"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_capture_shot_4_send_in_place(seed, browser_pw, scheme):  # noqa: F811
    """One seed per theme, so the light and dark threads are the same."""
    OUT.mkdir(parents=True, exist_ok=True)
    cam = Camera(browser_pw, seed["base"], seed["persons"]["Maya"])
    ids = converse(seed)
    jon_replies(seed, ids)
    jon = f"/app/conversations/{ids['jon']}"
    saved = []
    size = f"{BIG[0]}x{BIG[1]}"
    context, page = cam.open(scheme, BIG, jon)
    page.evaluate("() => { window.__noReload = true; }")
    page.fill("#body", FOLLOW_UP)
    page.press("#body", "Control+Enter")
    page.wait_for_function("() => document.querySelector('.rc-thread').innerText.includes('total_cents fix')"
                           " && document.querySelector('#body').value === ''")
    page.wait_for_function("() => document.querySelector('.rc-list').innerText.includes('total_cents fix')")
    assert page.evaluate("() => window.__noReload === true") and page.locator(".rc-flash").count() == 0
    settle(page)
    name = f"{PHASE}-4-send-in-place-{scheme}-{size}.png"
    page.screenshot(path=str(OUT / name))
    saved.append(name)
    # A failed send: the network drops; one inline line, the draft stays.
    page.route("**/app/send", lambda route: route.abort())
    page.fill("#body", FAILED)
    page.press("#body", "Control+Enter")
    page.wait_for_selector(".rc-send-status:not([hidden])")
    assert page.input_value("#body") == FAILED
    settle(page)
    name = f"{PHASE}-4-send-failed-{scheme}-{size}.png"
    page.screenshot(path=str(OUT / name))
    saved.append(name)
    context.close()
    with open(OUT / f"{PHASE}-4-files.txt", "a", encoding="utf-8") as listing:
        listing.write("\n".join(saved) + "\n")
