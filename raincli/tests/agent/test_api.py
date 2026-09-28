import threading
import time
import uuid

import pytest

from raincli_agent.api import ApiClient
from raincli_agent.errors import (Forbidden, IdConflict, InboxFull, Invalid, NotAcked, NotFound,
                                  RateLimited, RedirectRefused, Unauthorized, Unavailable, Unreachable)

from .conftest import client_for, send


def test_redirect_refused_and_token_not_sent_to_target(fake_api, recorder):
    fake_api.state.redirect_to = recorder.url
    api = client_for(fake_api, fake_api.alice)
    with pytest.raises(RedirectRefused):
        api.me()
    with pytest.raises(RedirectRefused):
        api.send("bob", "hi")
    assert recorder.requests == []
    assert fake_api.state.send_commits == 0


def test_503_retried_without_duplicate_send(fake_api):
    fake_api.state.fail("POST", r"/messages$", (503, "unavailable", {}), times=2)
    sleeps = []
    api = ApiClient(fake_api.url, fake_api.alice, sleep=sleeps.append)
    message, created = api.send("bob", "hello")
    assert created is True
    assert len(sleeps) == 2
    assert fake_api.state.send_commits == 1


def test_dropped_connection_after_commit_does_not_duplicate(fake_api):
    fake_api.state.fail("POST", r"/messages$", "drop_after")
    mid = str(uuid.uuid4())
    api = client_for(fake_api, fake_api.alice)
    message, created = api.send("bob", "hello", message_id=mid)
    # first attempt committed but the response was lost; the retry reused the id
    assert message["id"] == mid and created is False
    assert fake_api.state.send_commits == 1
    posts = [r for r in fake_api.state.requests if r[0] == "POST" and r[1].endswith("/messages")]
    assert len(posts) == 2


def test_dropped_connection_before_commit_is_retried(fake_api):
    fake_api.state.fail("POST", r"/messages$", "drop")
    fake_api.state.fail("GET", r"/me$", "drop")
    api = client_for(fake_api, fake_api.alice)
    assert api.me()["agent"]["handle"] == "alice"
    message, created = api.send("bob", "hello")
    assert created is True and fake_api.state.send_commits == 1


def test_503_exhausted_is_unavailable(fake_api):
    fake_api.state.fail("GET", r"/me$", (503, "unavailable", {}), times=10)
    api = ApiClient(fake_api.url, fake_api.alice, max_attempts=3, sleep=lambda s: None)
    with pytest.raises(Unavailable):
        api.me()


def test_rate_limited_honors_retry_after(fake_api):
    fake_api.state.fail("GET", r"/agents$", (429, "rate_limited", {"Retry-After": "2"}))
    sleeps = []
    api = ApiClient(fake_api.url, fake_api.alice, sleep=sleeps.append)
    assert {a["handle"] for a in api.agents()} == {"alice", "bob", "mallory"}
    assert sleeps == [2.0]


def test_inbox_full_not_retried(fake_api):
    fake_api.state.max_pending = 1
    api = ApiClient(fake_api.url, fake_api.alice, sleep=lambda s: pytest.fail("retried"))
    api.send("bob", "one")
    with pytest.raises(InboxFull):
        api.send("bob", "two")


def test_unreachable(fake_api):
    api = ApiClient("http://127.0.0.1:1", fake_api.alice, max_attempts=2, sleep=lambda s: None)
    with pytest.raises(Unreachable):
        api.me()


def test_typed_errors(fake_api):
    alice, bob = client_for(fake_api, fake_api.alice), client_for(fake_api, fake_api.bob)
    msg, _ = alice.send("bob", "hello")
    with pytest.raises(Forbidden):
        alice.ack(msg["id"])  # only the recipient acks
    with pytest.raises(NotAcked):
        bob.event(msg["id"], "held", "busy")
    with pytest.raises(NotFound):
        client_for(fake_api, fake_api.mallory).get_message(msg["id"])
    with pytest.raises(IdConflict):
        alice.send("bob", "different body", message_id=msg["id"])
    with pytest.raises(Invalid):
        alice.send("eve", "cross-team")
    with pytest.raises(Unauthorized):
        ApiClient(fake_api.url, "rca_" + "x" * 43).me()


def test_ack_idempotent_and_event_after_ack(fake_api):
    msg = send(fake_api, fake_api.alice, "bob")
    bob = client_for(fake_api, fake_api.bob)
    assert bob.ack(msg["id"])[1] is True
    assert bob.ack(msg["id"])[1] is False
    assert bob.event(msg["id"], "submitted", "ok")["delivery_state"] == "submitted"


def test_token_never_in_repr_or_errors(fake_api):
    api = client_for(fake_api, fake_api.alice)
    assert fake_api.alice not in repr(api)
    assert fake_api.alice not in repr(vars(api))
    try:
        api.get_message(str(uuid.uuid4()))
    except NotFound as exc:
        assert fake_api.alice not in str(exc) and fake_api.alice not in repr(exc)


def test_https_used_for_remote_hosts_only():
    with pytest.raises(Exception):
        ApiClient("http://example.com", "rca_" + "A" * 43)
    ApiClient("https://example.com", "rca_" + "A" * 43)


def test_long_poll_returns_early(fake_api):
    bob = client_for(fake_api, fake_api.bob)
    threading.Timer(0.3, lambda: send(fake_api, fake_api.alice, "bob", "late")).start()
    start = time.monotonic()
    messages, cursor = bob.inbox(wait=10)
    assert time.monotonic() - start < 5
    assert [m["body"] for m in messages] == ["late"] and cursor == messages[0]["seq"]
