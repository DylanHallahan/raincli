"""Group chats screenshots 1-4 (brief "RainCLI group chats", Screenshots; protocol §22 G6), in Chromium, light and
dark, at 1180x780 and 760x520, on the shared group seed (tests/conftest_groups.py). Not collected by default (no
``test_`` prefix). Run explicitly:

    RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_groups_web.py

1. The conversation list with the Groups section (an app window that doesn't open the latest, so Inbox stays).
2. "checkout", read-only: Maya's agent's post mentioning Jon and Priya, their agents' replies, "Delivered to 2 of 2"
   (Ryan, on an old client, gets no copy), no composer.
3. The member panel: Jon, Maya, Priya, and Ryan "not taking part", the "Ask Dylan..." line, no controls.
4. A launch (a 0.5.5+ window) opening the group, its post being the newest activity (D12).
The Windows window's shots come from the Windows app e2e (the client branch).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import APP_TOKEN, INSTALL_HASH, api, browser, live  # noqa: F401

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "groups-web-screenshots")
SIZES = {"1180x780": (1180, 780), "760x520": (760, 520)}


def window(browser, base, token, size, scheme, *, opens_latest):  # noqa: F811
    probe = browser.new_page()
    agent = probe.evaluate("navigator.userAgent") + " RainCLIApp/" + APP_TOKEN
    probe.close()
    if opens_latest:
        agent += " RainCLIAppVersion/0.6.1"
    width, height = SIZES[size]
    context = browser.new_context(user_agent=agent, viewport={"width": width, "height": height}, color_scheme=scheme,
                                  device_scale_factor=1)
    page = context.new_page()
    page.goto(api(base, "/api/v1/app/handoff", {"app_install_hash": INSTALL_HASH}, token)["url"])
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)
    return context, page


def test_screenshots(live, browser, groups_seed):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    s = groups_seed
    maya = s["person_tokens"]["Maya"]
    checkout = f"{live}/app/groups/{s['groups']['checkout'].id}"
    for size in SIZES:
        for scheme in ("light", "dark"):
            tag = f"{size}-{scheme}"
            context, page = window(browser, live, maya, size, scheme, opens_latest=False)
            page.goto(live + "/app/inbox")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(OUT / f"1-list-with-groups-{tag}.png"))
            page.goto(checkout + "#latest")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / f"2-checkout-read-only-{tag}.png"))
            page.click(".rc-members > summary")
            page.wait_for_timeout(150)
            page.screenshot(path=str(OUT / f"3-member-panel-{tag}.png"))
            context.close()
            context, page = window(browser, live, maya, size, scheme, opens_latest=True)
            assert page.url.startswith(checkout) or size == "760x520", page.url  # below the breakpoint Inbox stays
            page.screenshot(path=str(OUT / f"4-launch-opens-the-group-{tag}.png"))
            context.close()
