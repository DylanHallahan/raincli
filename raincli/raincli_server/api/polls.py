"""Concurrent long-poll limits (protocol §18.1 item 4).

At most ``per_credential`` long-polls wait at once for one machine credential or person session, at most
``per_user`` for all of one user's credentials and sessions together, and at most ``per_process`` in all, which
keeps most of uvicorn's connection slots (``--limit-concurrency 200``) for everything else. A poll past any
limit is not refused: it waits ``OVER_LIMIT_HOLD`` seconds without a slot (so a client that polls again at once
still polls slowly), looks once more, and is answered as a short poll with ``Retry-After``. The counters live
in this process, which is correct for the single uvicorn worker the service runs (``--workers 1``); several
workers would need the counters in shared state.

TODO(client): v0.5.3 clients should wait ``Retry-After`` seconds after an empty ``200`` that carries it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Hashable

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from raincli_server.identity import MACHINE_LIMIT

PER_CREDENTIAL = 2
PER_USER = 2 * MACHINE_LIMIT + 4  # every machine and a session for each, with room to spare
PER_PROCESS = 150  # of uvicorn's 200 connection slots
OVER_LIMIT_HOLD = 2.0  # seconds an over-limit poll waits, holding no slot, before it is answered
RETRY_AFTER_SECONDS = 5


class PollSlots:
    def __init__(self, per_credential: int = PER_CREDENTIAL, per_user: int = PER_USER,
                 per_process: int = PER_PROCESS) -> None:
        self.per_credential, self.per_user, self.per_process = per_credential, per_user, per_process
        self._held: dict[Hashable, int] = {}
        self._total = 0
        self._lock = threading.Lock()

    def acquire(self, credential: Hashable, user: Hashable) -> bool:
        with self._lock:
            if (self._total >= self.per_process or self._held.get(credential, 0) >= self.per_credential
                    or self._held.get(user, 0) >= self.per_user):
                return False
            for key in (credential, user):
                self._held[key] = self._held.get(key, 0) + 1
            self._total += 1
            return True

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def release(self, credential: Hashable, user: Hashable) -> None:
        with self._lock:
            self._total = max(0, self._total - 1)
            for key in (credential, user):
                left = self._held.get(key, 0) - 1
                if left > 0:
                    self._held[key] = left
                else:
                    self._held.pop(key, None)

    def held(self, key: Hashable) -> int:
        with self._lock:
            return self._held.get(key, 0)


async def long_poll(request: Request, slots: PollSlots, wait: float, interval: float,
                    poll: Callable[[bool], tuple[dict, tuple[Hashable, Hashable]]],
                    hold: float = OVER_LIMIT_HOLD) -> dict | JSONResponse:
    """Run ``poll(count)`` until it finds messages or ``wait`` seconds pass. ``poll`` returns the response body
    and the ``(credential, user)`` slot keys; only the first call is counted against the rate limit."""
    deadline = time.monotonic() + wait
    result, keys = await run_in_threadpool(poll, True)
    if result["messages"] or wait <= 0:
        return result
    if not slots.acquire(*keys):  # over a limit: a short wait without a slot, one more look, then the answer
        await asyncio.sleep(min(wait, hold))
        if not await request.is_disconnected():
            result, _ = await run_in_threadpool(poll, False)
        return JSONResponse(result, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or await request.is_disconnected():
                return result
            await asyncio.sleep(min(interval, remaining))
            result, _ = await run_in_threadpool(poll, False)
            if result["messages"]:
                return result
    finally:
        slots.release(*keys)


