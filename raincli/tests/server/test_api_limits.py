"""Body size limit, rate limiting and long-poll."""

import threading
import time
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient

from api_helpers import auth, err, send
from raincli_server import messaging
from raincli_server.api import RateLimiter
from raincli_server.app import create_app
from raincli_server.db import make_sessionmaker
from raincli_server.models import Agent


def test_body_over_64k_is_413(client, world):
    """64 KiB everywhere except POST /messages (2 MiB, see test_api_attachments)."""
    t = world["tokens"]["alice"]
    mid = send(client, t, "bob-agent").json()["message"]["id"]
    url = f"/api/v1/messages/{mid}/events"
    big = b'{"state": "held", "detail": "' + b"x" * 70000 + b'"}'
    r = client.post(url, content=big, headers={**auth(t), "Content-Type": "application/json"})
    assert r.status_code == 413 and err(r) == "too_large"

    def chunks():  # no Content-Length: enforced while streaming
        for _ in range(20):
            yield b"x" * 4096
    r = client.post(url, content=chunks(), headers=auth(t))
    assert r.status_code == 413 and err(r) == "too_large"
    r = client.post("/api/v1/messages", content=(b"x" * 4096 for _ in range(600)), headers=auth(t))
    assert r.status_code == 413 and err(r) == "too_large"
    # 16000 characters of 4-byte UTF-8 (64000 bytes of text) is a valid body
    assert send(client, t, "bob-agent", "\U0001F600" * 16000).status_code == 201


def test_rate_limit_429_with_retry_after(settings, world):
    app = create_app(replace(settings, rate_limit_per_min=3))
    try:
        with TestClient(app) as c:
            t = world["tokens"]["alice"]
            assert [c.get("/api/v1/me", headers=auth(t)).status_code for _ in range(3)] == [200] * 3
            r = c.get("/api/v1/me", headers=auth(t))
            assert r.status_code == 429 and err(r) == "rate_limited"
            assert 1 <= int(r.headers["Retry-After"]) <= 60
            # per credential: another agent is unaffected
            assert c.get("/api/v1/me", headers=auth(world["tokens"]["bob"])).status_code == 200
    finally:
        app.state.engine.dispose()


def test_rate_limiter_window():
    now = [1000.0]
    rl = RateLimiter(2, clock=lambda: now[0])
    assert rl.check("k") is None and rl.check("k") is None
    assert rl.check("k") == 60
    now[0] += 30
    assert rl.check("k") == 30
    now[0] += 30.5
    assert rl.check("k") is None


def test_long_poll_returns_early_on_arrival(client, world, engine, settings):
    t_bob = world["tokens"]["bob"]
    box = {}

    def poll():
        start = time.monotonic()
        box["resp"] = client.get("/api/v1/inbox?wait=20", headers=auth(t_bob))
        box["elapsed"] = time.monotonic() - start

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(1.0)
    s = make_sessionmaker(engine)()
    try:
        alice = s.get(Agent, world["agents"]["alice"].id)
        msg, _ = messaging.send_message(s, alice, id=uuid.uuid4(), to_handle="bob-agent", body="ping",
                                        max_pending=settings.max_pending)
        s.commit()
    finally:
        s.close()
    th.join(timeout=15)
    assert not th.is_alive()
    assert box["resp"].status_code == 200
    assert [m["id"] for m in box["resp"].json()["messages"]] == [str(msg.id)]
    assert 0.9 <= box["elapsed"] < 5


def test_long_poll_other_requests_not_blocked(client, world):
    """A waiting long-poll must not block the event loop for other requests."""
    t_bob = world["tokens"]["bob"]
    th = threading.Thread(target=lambda: client.get("/api/v1/inbox?wait=3", headers=auth(t_bob)))
    th.start()
    time.sleep(0.3)
    start = time.monotonic()
    assert client.get("/api/v1/me", headers=auth(world["tokens"]["alice"])).status_code == 200
    assert time.monotonic() - start < 1.5
    th.join()


def test_long_poll_empty_after_wait(client, world):
    start = time.monotonic()
    r = client.get("/api/v1/inbox?wait=1.2&after=7", headers=auth(world["tokens"]["bob"]))
    elapsed = time.monotonic() - start
    assert r.status_code == 200 and r.json() == {"messages": [], "cursor": 7}
    assert 1.1 <= elapsed < 3
