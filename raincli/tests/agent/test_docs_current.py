"""User docs must match current routing (protocol §16, §16.19): no wording from before v0.5.0."""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCS = [REPO / "README.md", REPO / "SETUP.md", REPO / "raincli" / "raincli_agent" / "skill" / "SKILL.md",
        *sorted(p for p in (REPO / "docs").glob("*.md") if p.name != "raincli-protocol.md")]  # the protocol keeps history
STALE = [
    "only to a machine's inbox",
    "can't be messaged",
    "cannot be messaged",
    "can't message them",
    "only to the inbox",
    "for visibility only",
    "linux and macos only",
    "don't receive messages yet",
    "until message routing arrives",
    "claude code only; not codex",
]


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_no_stale_routing_wording(path):
    text = " ".join(path.read_text(encoding="utf-8").lower().split())
    found = [phrase for phrase in STALE if phrase in text]
    assert not found, f"{path.name}: {found}"
