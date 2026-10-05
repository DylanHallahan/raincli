"""The protocol error type shared by the messaging, routing and person services (protocol §3)."""

from __future__ import annotations


class MessagingError(Exception):
    """A protocol-level failure: ``status`` and ``code`` follow protocol §3. ``extra`` holds top-level
    reply fields beside ``error``, such as ``reason`` for ``not_deliverable`` (§16.2)."""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra
