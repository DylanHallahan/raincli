"""TEMP layout diagnostic (POST /app/layout-diag, the shell bug): app mode only, CSRF, a strict schema, a 4 KiB
cap, a per-session rate limit, 204, and one log line with only the validated fields."""

from __future__ import annotations

import json
import logging
import re

import pytest

from raincli_server.web import routes
from test_app_mode import alice_app  # noqa: F401  (fixture)
from test_web_app import app_csrf, login

LAYOUT = {
    "event": "load", "page": "conversation", "inner_w": 910, "inner_h": 512, "client_h": 512, "dpr": 1.5,
    "children_n": 3, "children": ["nav.rc-nav", "section.rc-list", "main.rc-main"], "grid_rows": "512px",
    "body_height": "512px", "nav_bottom": 512, "list_bottom": 512, "main_bottom": 512, "compose_bottom": 512,
    "doc_scroll_top": 643, "doc_scroll_h": 512, "doc_client_h": 512, "nav_scroll_h": 512, "nav_client_h": 512,
    "visibility": "visible", "height_fix": False, "scroll_guard": False,
}


HEALTHY = {**LAYOUT, "doc_scroll_top": 0}  # LAYOUT is an anomaly (the document scrolled), as the client sends


class Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def diag(monkeypatch):
    monkeypatch.setattr(routes, "_diag_limiter", routes._DiagLimiter(routes.LAYOUT_DIAG_PER_WINDOW, routes.LAYOUT_DIAG_WINDOW))
    logger = logging.getLogger("raincli_server.layout_diag")
    handler = Lines()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler
    logger.removeHandler(handler)


def csrf_of_app(webview):
    return re.search(r'data-csrf="([^"]+)"', webview.get("/app/inbox").text).group(1)


def post(client, csrf, layout=None, **extra):
    body = {"csrf_token": csrf, "layout": LAYOUT if layout is None else layout, **extra}
    return client.post("/app/layout-diag", content=json.dumps(body), headers={"Content-Type": "application/json"})


def test_a_report_is_204_and_logs_one_line_with_only_the_fields(alice_app, diag):  # noqa: F811
    webview = alice_app["webview"]
    r = post(webview, csrf_of_app(webview))
    assert r.status_code == 204 and r.content == b""
    assert len(diag.lines) == 1
    line = diag.lines[0]
    m = re.fullmatch(r"layout-diag machine=(\S+) version=(\S+) (\{.*\})", line)
    assert m, line
    assert m.group(1) == "alice-laptop"  # the app machine of the person session
    assert m.group(2) == "none"  # the test client sends no RainCLIAppVersion
    logged = json.loads(m.group(3))
    assert logged == LAYOUT and list(logged) == list(routes.LAYOUT_DIAG_FIELDS)
    for secret in ("csrf", alice_app["token"], "@", "http", "/app/"):
        assert secret not in line


def test_the_app_version_is_logged_from_the_user_agent(alice_app, diag, monkeypatch):  # noqa: F811
    # The session is bound to its exact app User-Agent, so the version is supplied through the parser itself.
    seen = []
    monkeypatch.setattr(routes.security, "app_version", lambda ua: seen.append(ua) or (0, 5, 5))
    webview = alice_app["webview"]
    assert post(webview, csrf_of_app(webview)).status_code == 204
    assert " version=0.5.5 " in diag.lines[-1] and "RainCLIApp/" in seen[-1]


def test_csrf_is_required(alice_app, diag):  # noqa: F811
    assert post(alice_app["webview"], "wrong").status_code == 403
    assert post(alice_app["webview"], "").status_code == 403
    assert diag.lines == []


def test_a_website_session_gets_404(client, world, diag):
    login(client)
    assert post(client, app_csrf(client)).status_code == 404 and diag.lines == []


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"event": "click"}, {"page": "/app/conversations/x"}, {"inner_w": -1}, {"inner_w": 10 ** 9},
    {"inner_h": 1.5}, {"dpr": "1.5"}, {"dpr": True}, {"children": ["x" * 41]}, {"children": ["a"] * 11},
    {"children": ["tab\there"]}, {"grid_rows": "é"}, {"grid_rows": "x" * 81}, {"body_height": None},
    {"nav_bottom": "512"}, {"height_fix": 1}, {"visibility": "gone"}, {"children_n": True},
])
def test_the_schema_is_strict(alice_app, diag, change):  # noqa: F811
    layout = {**LAYOUT}
    for key, value in change.items():
        layout[key] = value
    r = post(alice_app["webview"], csrf_of_app(alice_app["webview"]), layout)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid" and diag.lines == []


def test_missing_fields_and_unknown_top_level_keys_are_refused(alice_app, diag):  # noqa: F811
    webview = alice_app["webview"]
    csrf = csrf_of_app(webview)
    layout = {k: v for k, v in LAYOUT.items() if k != "dpr"}
    assert post(webview, csrf, layout).status_code == 400
    assert post(webview, csrf, url="https://example.com").status_code == 400
    assert post(webview, csrf, []).status_code == 400
    assert webview.post("/app/layout-diag", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert diag.lines == []


def test_the_body_is_capped_at_4_kib(alice_app, diag):  # noqa: F811
    webview = alice_app["webview"]
    r = post(webview, csrf_of_app(webview), padding="x" * 4100)
    assert r.status_code == 413 and diag.lines == []


def test_the_rate_limit_is_per_session(alice_app, diag):  # noqa: F811
    webview = alice_app["webview"]
    csrf = csrf_of_app(webview)
    codes = [post(webview, csrf).status_code for _ in range(routes.LAYOUT_DIAG_PER_WINDOW + 2)]
    assert codes == [204] * routes.LAYOUT_DIAG_PER_WINDOW + [429, 429]
    assert len(diag.lines) == routes.LAYOUT_DIAG_PER_WINDOW


def test_the_switch_turns_it_off(alice_app, diag, monkeypatch):  # noqa: F811
    monkeypatch.setattr(routes, "LAYOUT_DIAG", False)
    assert post(alice_app["webview"], csrf_of_app(alice_app["webview"])).status_code == 404 and diag.lines == []


def test_a_healthy_window_is_accepted_but_not_logged(alice_app, diag):  # noqa: F811
    webview = alice_app["webview"]
    csrf = csrf_of_app(webview)
    for healthy in (HEALTHY,
                    {**HEALTHY, "children_n": 2, "children": ["nav.rc-nav", "main.rc-main"], "list_bottom": None},
                    {**HEALTHY, "list_bottom": None, "compose_bottom": None},  # narrow breakpoint, Inbox
                    {**HEALTHY, "nav_bottom": 510, "main_bottom": 514}):  # within 2 px
        assert post(webview, csrf, healthy).status_code == 204
    assert diag.lines == []


@pytest.mark.parametrize("anomaly", [
    {"doc_scroll_top": 1}, {"height_fix": True}, {"scroll_guard": True}, {"children_n": 4},
    {"children_n": 1, "children": ["main.rc-main"]}, {"children": ["nav.rc-nav", "div.injected", "main.rc-main"]},
    {"main_bottom": 98}, {"compose_bottom": 509}, {"nav_bottom": 515}, {"list_bottom": 400},
])
def test_each_anomaly_is_logged(alice_app, diag, anomaly):  # noqa: F811
    webview = alice_app["webview"]
    assert post(webview, csrf_of_app(webview), {**HEALTHY, **anomaly}).status_code == 204
    assert len(diag.lines) == 1 and diag.lines[0].startswith("layout-diag machine=alice-laptop ")


def test_the_switch_reads_the_environment():
    assert routes._layout_diag_on({}) is True
    assert routes._layout_diag_on({"RAINCLI_LAYOUT_DIAG": "1"}) is True
    assert routes._layout_diag_on({"RAINCLI_LAYOUT_DIAG": " 0 "}) is False
    assert routes._layout_diag_on({"RAINCLI_LAYOUT_DIAG": "0"}) is False
