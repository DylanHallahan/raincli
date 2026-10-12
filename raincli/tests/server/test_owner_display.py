"""How machine owners are shown (protocol §18.1 item 7), real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from api_helpers import auth, send
from raincli_server import identity, owners
from raincli_server.models import User


def directory(client, token):
    return {a["handle"]: a for a in client.get("/api/v1/agents", headers=auth(token)).json()["agents"]}


def test_the_directory_lists_only_your_own_machines_and_messages_name_no_owner(client, world):
    """§21 C13 M2, C5.2: /api/v1/agents is the caller's own machines; a teammate's message names the person, and
    from_owner is null."""
    shown = directory(client, world["tokens"]["alice"])
    assert set(shown) == {"alice-agent"} and shown["alice-agent"]["owner"] == "Alice"
    assert send(client, world["tokens"]["bob"], "alice@example.test", "hi").status_code == 201
    message = client.get("/api/v1/inbox", headers=auth(world["tokens"]["alice"])).json()["messages"][0]
    assert message["from_owner"] is None and message["from_endpoint"] == {"person": "bob@example.test",
                                                                          "display_name": "Bob"}


def test_owners_whose_names_look_alike_are_told_apart_on_people(app, client, world, session):
    """§18.1 item 7, for people (§21): the person is the address, so look-alike names in a team are told apart on
    People and in the picker."""
    from test_app_mode import open_app, person_session

    robert = identity.create_user(session, "robert@example.test", "Воb", "correct horse battery")  # Cyrillic В, о
    identity.add_member(session, world["teams"]["acme"], robert)
    session.commit()
    webview, _ = open_app(app, person_session(client)["person_session"], client)
    page = webview.get("/app/people").text
    assert "Bob (bob)" in page and "Воb (robert)" in page
    names = {p["name"] for p in webview.get("/app/recipients").json()["recipients"]}
    assert {"Bob (bob)", "Воb (robert)"} <= names
    webview.close()


def test_a_person_sender_has_no_owner(client, world, session):
    r = client.post("/api/v1/app/login", json={"email": "bob@example.test", "password": "correct horse battery",
                                               "machine_name": "bob-pc", "person_session": True})
    person = r.json()["person_session"]
    r = client.post("/api/v1/person/send", headers=auth(person),
                    json={"id": "11111111-1111-4111-8111-111111111111", "to": "alice@example.test", "body": "from bob"})
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
    assert len({owners.skeleton(v, whole=True) for v in shown.values()}) == len(shown), shown


def test_a_name_chosen_to_look_like_a_disambiguated_one_is_still_told_apart(world, session):
    team = world["teams"]["acme"]
    real = member(session, team, "alice.smith@example.test", "Alice Smith")
    member(session, team, "asmith@example.test", "Alice Smith")  # a genuine namesake: both get a suffix
    copy = member(session, team, "x@example.test", "placeholder")
    session.execute(update(User).where(User.id == copy.id).values(display_name="Alice Smith (alice.smith)"))
    session.commit()  # such a name can no longer be chosen, but a row from before may have it
    session.expire_all()
    shown = owners.displays(session, team.id)
    assert_all_distinct(shown)
    assert shown[real.id] != shown[copy.id]
    assert owners.skeleton(shown[real.id], whole=True) != owners.skeleton(shown[copy.id], whole=True)


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



def test_parentheses_and_at_signs_are_refused_in_new_person_names(session):
    for i, name in enumerate(("Alice (admin)", "Alice)", "alice@example", "Alice （x）", "Bob ＠ work")):
        with pytest.raises(identity.IdentityError):
            identity.create_user(session, f"p{i}@example.test", name, "correct horse battery")
    assert identity.create_user(session, "ok@example.test", "Alice-Marie O'Neil", "correct horse battery")


def test_an_old_name_carrying_a_shown_suffix_never_matches_the_real_member(world, session):
    team = world["teams"]["acme"]
    real = member(session, team, "alice.smith@example.test", "Alice Smith")
    old = member(session, team, "x@example.test", "placeholder")
    session.execute(update(User).where(User.id == old.id)
                    .values(display_name="Alice Smith (alice.smith) (alice.smith@example.test)"))
    session.commit()
    session.expire_all()
    shown = owners.displays(session, team.id)
    assert shown[real.id] == "Alice Smith (alice.smith)"  # the trailing groups made the old name clash
    assert shown[old.id] == "Alice Smith (alice.smith) (alice.smith@example.test) (x)"
    assert_all_distinct(shown)


def test_web_pages_and_revoke_prompts_show_owners_told_apart(app, world, session):
    from fastapi.testclient import TestClient

    from test_app_mode import open_app, person_session
    from test_web_app import login

    team = world["teams"]["acme"]
    robert = member(session, team, "robert@example.test", "Воb")  # Cyrillic В, о: reads like Bob
    identity.register_agent(session, team, robert, "build-01")
    session.commit()
    with TestClient(app) as browser:
        assert login(browser).status_code == 303  # alice owns acme, so she sees Revoke
        for path in ("/app/agents", "/app/teams/acme"):
            page = browser.get(path).text
            assert "Bob (bob)" in page and "Воb (robert)" in page, path
            assert "(owned by Воb (robert))" in page and "(owned by Bob (bob))" in page, path
    with TestClient(app) as client:
        webview, _ = open_app(app, person_session(client)["person_session"], client)
        page = webview.get("/app/people").text  # §21 C7.1: the app's Agents became People
        assert "Bob (bob)" in page and "Воb (robert)" in page
        webview.close()
