"""Shared helpers for tests/server/test_api_*.py."""

from __future__ import annotations

import uuid


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def send(client, token: str, to: str, body: str = "hello", **extra):
    payload = {"id": str(extra.pop("id", uuid.uuid4())), "to": to, "body": body, **extra}
    return client.post("/api/v1/messages", json=payload, headers=auth(token))


def err(resp) -> str:
    data = resp.json()
    assert set(data) == {"error"} and set(data["error"]) == {"code", "message"}, data
    return data["error"]["code"]
