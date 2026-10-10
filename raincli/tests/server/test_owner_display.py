"""How machine owners are shown (protocol §18.1 item 7), real PostgreSQL."""

from __future__ import annotations

from api_helpers import auth, send
from raincli_server import identity, owners


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
