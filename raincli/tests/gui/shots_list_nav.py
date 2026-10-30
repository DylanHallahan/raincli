"""List and nav polish screenshots (brief "RainCLI app list and nav polish", 2026-10-30), in Chromium, light and
dark, at 1180x780 and 760x520, on the shared group seed (tests/conftest_groups.py). Not collected by default (no
``test_`` prefix). Run explicitly:

    RAINCLI_SHOTS_DIR=<dir> pytest tests/gui/shots_list_nav.py

1. The conversation list with the groups mixed in (checkout first, its latest post as the preview; infra, with no
   posts yet, last with its member count), on New message so Inbox doesn't open the newest.
2. The icon-only rail: People current, keyboard focus on a rail link (its ring).
The Windows window's shots come from the Windows app e2e (part G, listnav-*).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest_groups import groups_seed  # noqa: F401  (fixture)
from conftest_v06 import v06_seed  # noqa: F401  (fixture)

from .test_app_mode_gui import browser, live  # noqa: F401
from .test_groups_gui import open_app

pytest.importorskip("playwright.sync_api")

OUT = Path(os.environ.get("RAINCLI_SHOTS_DIR") or "list-nav-screenshots")


def test_screenshots(live, browser, groups_seed):  # noqa: F811
    OUT.mkdir(parents=True, exist_ok=True)
    for width, height in ((1180, 780), (760, 520)):
        for scheme in ("light", "dark"):
            tag = f"{width}x{height}-{scheme}"
            context, page = open_app(browser, live, groups_seed["person_tokens"]["Maya"], width=width, height=height,
                                     scheme=scheme)
            page.goto(live + "/app/compose")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(OUT / f"1-list-with-a-group-{tag}.png"))
            page.goto(live + "/app/people")
            page.wait_for_load_state("networkidle")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            for _ in range(12):
                page.keyboard.press("Tab")
                if page.evaluate("!!document.activeElement.closest('nav.rc-nav')"):
                    break
            page.keyboard.press("Tab")  # from Inbox to People: the focus ring on the current page's link
            page.screenshot(path=str(OUT / f"2-icon-nav-{tag}.png"))
            context.close()
