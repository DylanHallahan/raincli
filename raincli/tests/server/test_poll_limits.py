"""Concurrent long-poll limits (protocol §18.1 item 4), real PostgreSQL: past a limit a poll is answered at once
as a short poll; one user's many machines and sessions all keep receiving."""

from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from api_helpers import auth, send
from raincli_server.api import polls
from test_app_login import app_login


def poll(app, token, results, path="/api/v1/inbox", wait=4):
    with TestClient(app) as c:
        started = time.monotonic()
        r = c.get(f"{path}?wait={wait}", headers=auth(token))
        results.append((r, time.monotonic() - started))


def wait_until(predicate, deadline=10):
    end = time.monotonic() + deadline
    while not predicate():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.05)


def test_a_third_concurrent_poll_on_one_credential_returns_at_once(client, world, app):
    slots = app.state.api.state.poll_slots
    token = world["tokens"]["bob"]
    results = []
    waiting = [threading.Thread(target=poll, args=(app, token, results)) for _ in range(2)]
    for t in waiting:
        t.start()
    wait_until(lambda: slots.held(("user", world["users"]["bob"].id)) == 2)
    third = []
    poll(app, token, third)
    r, took = third[0]
    # Not a refusal: a short poll, held about two seconds without a slot so a client that polls again at once
    # still polls slowly.
    assert r.status_code == 200 and r.json()["messages"] == [] and polls.OVER_LIMIT_HOLD - 0.5 < took < 4
    assert r.headers["retry-after"] == str(polls.RETRY_AFTER_SECONDS)
    send(client, world["tokens"]["alice"], "bob-agent", "wake up")
    for t in waiting:
        t.join(15)
    assert [len(r.json()["messages"]) for r, _ in results] == [1, 1]  # the two waiting polls received it
    assert slots.held(("user", world["users"]["bob"].id)) == 0  # released when the polls ended


def test_one_users_twelve_machines_and_twelve_sessions_all_keep_receiving(client, world, app):
    slots = app.state.api.state.poll_slots
    machines = [app_login(client, machine_name=f"box-{i:02d}").json()["token"] for i in range(12)]
    sessions = []
    for token in machines:
        r = client.post("/api/v1/app/login", json={"email": "alice@example.test", "password": "correct horse battery",
                                                   "person_only": True, "previous_token": token})
        assert r.status_code == 200, r.text
        sessions.append(r.json()["person_session"])
    results = []
    threads = [threading.Thread(target=poll, args=(app, t, results)) for t in machines] + \
        [threading.Thread(target=poll, args=(app, s, results, "/api/v1/person/inbox")) for s in sessions]
    for t in threads:
        t.start()
    wait_until(lambda: slots.held(("user", world["users"]["alice"].id)) == 24)
    for i in range(12):
        send(client, world["tokens"]["bob"], f"box-{i:02d}", f"to box {i}")
    send(client, world["tokens"]["bob"], {"person": "alice@example.test"}, "to alice")
    for t in threads:
        t.join(20)
    assert len(results) == 24
    assert all(r.status_code == 200 and r.json()["messages"] and "retry-after" not in r.headers
               for r, _ in results)


def test_a_slot_is_released_when_the_client_goes_away():
    slots = polls.PollSlots(per_credential=1, per_user=5)

    class Gone:
        async def is_disconnected(self):
            return True

    def empty(count):
        return {"messages": [], "cursor": 0}, (("credential", 1), ("user", 1))

    result = asyncio.run(polls.long_poll(Gone(), slots, 10, 0.01, empty))
    assert result == {"messages": [], "cursor": 0}
    assert slots.held(("credential", 1)) == 0 and slots.held(("user", 1)) == 0
    assert slots.acquire(("credential", 1), ("user", 1))


def test_the_per_user_limit_leaves_room_for_every_machine_and_a_session_each():
    from raincli_server.identity import MACHINE_LIMIT

    assert polls.PER_CREDENTIAL == 2 and polls.PER_USER == 2 * MACHINE_LIMIT + 4 == 44
    assert polls.PER_PROCESS == 150  # of uvicorn's --limit-concurrency 200


def test_held_polls_are_capped_per_process():
    slots = polls.PollSlots(per_credential=5, per_user=5, per_process=3)
    keys = [(("credential", i), ("user", i)) for i in range(4)]
    assert [slots.acquire(*k) for k in keys] == [True, True, True, False]
    assert slots.total == 3
    slots.release(*keys[0])
    assert slots.acquire(*keys[3]) and slots.total == 3


def test_a_poll_past_the_process_limit_is_a_held_short_poll(client, world, app):
    slots = app.state.api.state.poll_slots
    slots.per_process = 1
    results = []
    first = threading.Thread(target=poll, args=(app, world["tokens"]["bob"], results))
    first.start()
    wait_until(lambda: slots.total == 1)
    other = []
    poll(app, world["tokens"]["alice"], other, wait=1)  # another user: only the process limit applies
    r, took = other[0]
    assert r.status_code == 200 and r.headers["retry-after"] == str(polls.RETRY_AFTER_SECONDS)
    assert 0.5 < took < 3  # held for min(wait, 2) seconds
    send(client, world["tokens"]["alice"], "bob-agent", "wake")
    first.join(15)
    assert results[0][0].json()["messages"]


def test_a_held_short_poll_still_returns_a_message_that_arrived_meanwhile():
    slots = polls.PollSlots(per_credential=0, per_user=5)
    seen = []

    class Here:
        async def is_disconnected(self):
            return False

    def poll_once(count):
        seen.append(count)
        return {"messages": [] if count else [{"id": "m"}], "cursor": 0}, (("credential", 1), ("user", 1))

    response = asyncio.run(polls.long_poll(Here(), slots, 10, 0.01, poll_once, hold=0.05))
    assert seen == [True, False] and b'"m"' in response.body and response.headers["retry-after"] == "5"
