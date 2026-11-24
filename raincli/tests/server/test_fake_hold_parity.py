"""The client tests' fake server (tests/agent/fake_server.py) keeps its own copy of the server's hold grouping and
coarse API codes (§21 C5.5, C13 M3), so client tests see what the real server sends. This pins the copy to the
server's tables and behaviour, so a change to presentation can't leave it stale (next-turn review N1). No database."""

from __future__ import annotations

import pytest

from agent import fake_server
from raincli_server import presentation

# Every allowlisted raw code, a few that only ever arrive as outcomes, and junk: "failed" covers the rest.
CODES = sorted(presentation._GROUPS) + ["target_mismatch", "too_large_for_hook", "owner_unreachable", "delivered",
                                         "made_up_code", "", None]


def test_the_tables_are_the_servers():
    assert fake_server._HOLD_GROUPS == presentation._GROUPS
    assert fake_server._API_CODES == presentation._API_CODES
    assert fake_server._API_REPLY_CODES == presentation._API_REPLY_CODES


@pytest.mark.parametrize("route", [None, "inbox", "reply", "person"])
@pytest.mark.parametrize("code", CODES)
def test_the_coarse_code_is_the_servers(code, route):
    assert fake_server.api_hold_code(code, route) == presentation.api_hold_code(code, route)
