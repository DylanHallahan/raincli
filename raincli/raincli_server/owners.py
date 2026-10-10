"""How a machine's owner is shown (protocol §18.1 item 7).

The value is always the server's own record of the owner: their display name. Within a team, a display name
that looks like another member's (equal after removing invisible characters, NFKC, case folding, removing
accents and folding common look-alike letters) is followed by the email's local part. The shown strings are
then checked again, the same way, until no two members' read alike, so a name chosen to look like another
member's disambiguated name is distinguished too.
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
    "ԛ": "q", "ԝ": "w", "ѵ": "v", "ү": "y", "ɑ": "a", "ο": "o",
    # Armenian
    "օ": "o", "ս": "u", "ց": "g", "հ": "h", "ո": "n", "ռ": "n", "ք": "p", "զ": "q", "ւ": "l", "Տ": "s",
    "Օ": "o", "Ս": "u",
})


def skeleton(name: str) -> str:
    """A form under which names that read alike compare equal. Format characters (zero-width, joiners, bidi
    controls) are dropped first: new names may not contain them, but older rows might."""
    name = "".join(ch for ch in name if unicodedata.category(ch) != "Cf")
    folded = unicodedata.normalize("NFKC", name).casefold()
    folded = "".join(ch for ch in unicodedata.normalize("NFD", folded) if unicodedata.category(ch) != "Mn")
    folded = folded.translate(_LOOKALIKES).replace("i", "l")  # i and l are hard to tell apart in many fonts
    return re.sub(r"\s+", " ", folded).strip()


def displays(session: Session, team_id: uuid.UUID, extra: list[User] = ()) -> dict[uuid.UUID, str]:
    """``{user id: shown name}`` for the team's members (and ``extra`` users, such as a removed owner)."""
    users = {u.id: u for u in session.scalars(
        select(User).join(Membership, Membership.user_id == User.id).where(Membership.team_id == team_id))}
    users.update({u.id: u for u in extra if u is not None})
    out = {uid: user.display_name for uid, user in users.items()}
    # Each round adds a distinguishing suffix to every name that still reads like another's: the email's local
    # part, then the whole email, then part of the account id, which is unique.
    for suffix in (lambda u: u.email.split("@", 1)[0], lambda u: u.email, lambda u: u.id.hex[:12]):
        clashing = _clashes(out)
        if not clashing:
            break
        for uid in clashing:
            out[uid] = f"{out[uid]} ({suffix(users[uid])})"
    return out


def _clashes(shown: dict[uuid.UUID, str]) -> set[uuid.UUID]:
    """The users whose shown name reads like (or is) another user's."""
    by_skeleton: dict[str, set[uuid.UUID]] = {}
    for uid, name in shown.items():
        by_skeleton.setdefault(skeleton(name), set()).add(uid)
    return {uid for group in by_skeleton.values() if len(group) > 1 for uid in group}


def display(session: Session, team_id: uuid.UUID, user: User) -> str:
    return displays(session, team_id, [user])[user.id]
