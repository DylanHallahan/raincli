"""How a machine's owner is shown (protocol §18.1 item 7).

The value is always the server's own record of the owner: their display name. Within a team, a display name
that looks like another member's (equal after NFKC, case folding, removing accents and folding common
look-alike letters) is followed by the email's local part, so two owners never read the same.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from raincli_server.models import Membership, User

# Letters from other scripts that render like Latin ones, plus digits that pass for letters.
_LOOKALIKES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ӏ": "l", "һ": "h",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "ω": "w", "0": "o", "1": "l", "|": "l", "ı": "i", "ℓ": "l",
})


def skeleton(name: str) -> str:
    """A form under which names that read alike compare equal."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    folded = "".join(ch for ch in unicodedata.normalize("NFD", folded) if unicodedata.category(ch) != "Mn")
    folded = folded.translate(_LOOKALIKES).replace("i", "l")  # i and l are hard to tell apart in many fonts
    return re.sub(r"\s+", " ", folded).strip()


def displays(session: Session, team_id: uuid.UUID, extra: list[User] = ()) -> dict[uuid.UUID, str]:
    """``{user id: shown name}`` for the team's members (and ``extra`` users, such as a removed owner)."""
    users = {u.id: u for u in session.scalars(
        select(User).join(Membership, Membership.user_id == User.id).where(Membership.team_id == team_id))}
    users.update({u.id: u for u in extra if u is not None})
    by_skeleton: dict[str, set[uuid.UUID]] = {}
    for user in users.values():
        by_skeleton.setdefault(skeleton(user.display_name), set()).add(user.id)
    out = {}
    for user in users.values():
        name = user.display_name
        if len(by_skeleton[skeleton(name)]) > 1:
            name = f"{name} ({user.email.split('@', 1)[0]})"
        out[user.id] = name
    return out


def display(session: Session, team_id: uuid.UUID, user: User) -> str:
    return displays(session, team_id, [user])[user.id]
