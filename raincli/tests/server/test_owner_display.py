"""How machine owners are shown (protocol §18.1 item 7), real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from api_helpers import auth, send
from raincli_server import identity, owners
from raincli_server.models import User


def directory(client, token):
    return {a["handle"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()["agents"]}


def test_the_directory_and_messages_name_the_owner_by_display_name(client, world):
    shown = directory(client, world["tokens"]["alice"])
    assert shown["bob-agent"]["owner"] == "Bob" and shown["alice-agent"]["owner"] == "Alice"
    assert send(client, world["tokens"]["bob"], "alice-agent", "hi").status_code == 201
    message = client.get("/api/v1/inbox", headers=auth(world["tokens"]["alice"])).json()["messages"][0]
    assert message["from_owner"] == "Bob"


def test_owners_whose_names_look_alike_are_told_apart_within_the_team(client, world, session):
    robert = identity.create_user(session, "robert@example.test", "Воb", "correct horse battery")  # Cyrillic В, о
    identity.add_member(session, world["teams"]["acme"], robert)
    identity.register_agent(session, world["teams"]["acme"], robert, "build-01")
    session.commit()
    shown = directory(client, world["tokens"]["alice"])
    assert shown["bob-agent"]["owner"] == "Bob (bob)"
    assert shown["build-01"]["owner"] == "Воb (robert)"
    assert shown["alice-agent"]["owner"] == "Alice"
    # Another team, where the names do not collide, is unaffected.
    assert directory(client, world["tokens"]["eve"])["eve-agent"]["owner"] == "Eve"


def test_a_person_sender_has_no_owner(client, world, session):
    r = client.post("/api/v1/app/login", json={"email": "bob@example.test", "password": "correct horse battery",
                                               "machine_name": "bob-pc", "person_session": True})
    person = r.json()["person_session"]
    r = client.post("/api/v1/person/send", headers=auth(person),
                    json={"id": "11111111-1111-4111-8111-111111111111", "to": "alice-agent", "body": "from bob"})
    assert r.status_code == 201 and r.json()["message"]["from_owner"] is None


def test_name_skeletons():
    assert owners.skeleton("Alice") == owners.skeleton("ALICE") == owners.skeleton("Аlice") == owners.skeleton("Al1ce")
    assert owners.skeleton("José") == owners.skeleton("Jose") and owners.skeleton("Ｂob") == owners.skeleton("bob")
    assert owners.skeleton("Alice") != owners.skeleton("Alicia")


def member(session, team, email, name):
    user = identity.create_user(session, email, name, "correct horse battery")
    identity.add_member(session, team, user)
    return user


def assert_all_distinct(shown):
    assert len(set(shown.values())) == len(shown), shown
    assert len({owners.skeleton(v) for v in shown.values()}) == len(shown), shown


def test_a_name_chosen_to_look_like_a_disambiguated_one_is_still_told_apart(world, session):
    team = world["teams"]["acme"]
    real = member(session, team, "alice.smith@example.test", "Alice Smith")
    member(session, team, "asmith@example.test", "Alice Smith")  # a genuine namesake: both get a suffix
    copy = member(session, team, "x@example.test", "Alice Smith (alice.smith)")
    session.commit()
    shown = owners.displays(session, team.id)
    assert_all_distinct(shown)
    assert shown[real.id] != shown[copy.id] and owners.skeleton(shown[real.id]) != owners.skeleton(shown[copy.id])


def test_invisible_characters_are_refused_in_new_names_and_ignored_in_old_ones(world, session):
    team = world["teams"]["acme"]
    for name in ("Alice​ Smith", "Alice⁠Smith", "‮Alice Smith", "Ali‍ce"):
        with pytest.raises(identity.IdentityError):
            identity.create_user(session, f"{len(name)}{ord(name[3])}@example.test", name, "correct horse battery")
    real = member(session, team, "alice.smith@example.test", "Alice Smith")
    old = member(session, team, "old@example.test", "placeholder")
    session.execute(update(User).where(User.id == old.id).values(display_name="Alice​ Smith"))  # a row from before
    session.commit()
    session.expire_all()
    shown = owners.displays(session, team.id)
    assert shown[real.id] == "Alice Smith (alice.smith)" and shown[old.id] == "Alice​ Smith (old)"
    assert_all_distinct(shown)


def test_letters_from_other_scripts_that_look_latin_are_told_apart(world, session):
    team = world["teams"]["acme"]
    real = member(session, team, "bob.jones@example.test", "Bob Jones")
    lookalike = member(session, team, "bj@example.test", "Bօb Jones")  # Armenian օ
    session.commit()
    shown = owners.displays(session, team.id)
    assert shown[real.id] == "Bob Jones (bob.jones)" and shown[lookalike.id] == "Bօb Jones (bj)"
    assert_all_distinct(shown)
