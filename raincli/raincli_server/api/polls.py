"""Concurrent long-poll limits (protocol §18.1 item 4).

At most ``per_credential`` long-polls wait at once for one machine credential or person session, and at most
``per_user`` for all of one user's credentials and sessions together. A poll past either limit is not refused:
it is answered at once, as a short poll, with ``Retry-After``. The counters live in this process, which is
correct for the single uvicorn worker the service runs (``--workers 1``); several workers would need the
counters in shared state.
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
RETRY_AFTER_SECONDS = 5


class PollSlots:
    def __init__(self, per_credential: int = PER_CREDENTIAL, per_user: int = PER_USER) -> None:
        self.per_credential, self.per_user = per_credential, per_user
        self._held: dict[Hashable, int] = {}
        self._lock = threading.Lock()

    def acquire(self, credential: Hashable, user: Hashable) -> bool:
        with self._lock:
            if self._held.get(credential, 0) >= self.per_credential or self._held.get(user, 0) >= self.per_user:
                return False
            for key in (credential, user):
                self._held[key] = self._held.get(key, 0) + 1
            return True

    def release(self, credential: Hashable, user: Hashable) -> None:
        with self._lock:
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
                    poll: Callable[[bool], tuple[dict, tuple[Hashable, Hashable]]]) -> dict | JSONResponse:
    """Run ``poll(count)`` until it finds messages or ``wait`` seconds pass. ``poll`` returns the response body
    and the ``(credential, user)`` slot keys; only the first call is counted against the rate limit."""
    deadline = time.monotonic() + wait
    result, keys = await run_in_threadpool(poll, True)
    if result["messages"] or wait <= 0:
        return result
    if not slots.acquire(*keys):  # over a limit: answered now as a short poll
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


