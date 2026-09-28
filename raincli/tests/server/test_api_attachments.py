"""Markdown attachments (protocol §8) through the HTTP API and the service layer."""

import base64
import hashlib
import uuid

from sqlalchemy import func, select

from api_helpers import auth, err, send
from raincli_server import identity, messaging, security
from raincli_server.models import Attachment, Message


def att(name: str, data: bytes, sha: str | None = None) -> dict:
    return {"filename": name, "content_b64": base64.b64encode(data).decode(),
            "sha256": sha or hashlib.sha256(data).hexdigest()}


def counts(engine) -> tuple[int, int]:
    with engine.connect() as c:
        return (c.execute(select(func.count()).select_from(Message)).scalar(),
                c.execute(select(func.count()).select_from(Attachment)).scalar())


def test_exact_byte_round_trip_and_headers(client, world):
    t_alice, t_bob = world["tokens"]["alice"], world["tokens"]["bob"]
    files = [
        ("notes.md", b"# Title\r\n\r\nline with trailing space   \r\nlast line without newline"),
        ("Unicode Plan.v2.md", "# Plän ✓\n\n- naïve café 日本語\n\t\n".encode()),
    ]
    r = send(client, t_alice, "bob-agent", "see attached", attachments=[att(n, d) for n, d in files])
    assert r.status_code == 201, r.text
    meta = r.json()["message"]["attachments"]
    assert [(a["filename"], a["size"], a["sha256"], a["media_type"]) for a in meta] == [
        (n, len(d), hashlib.sha256(d).hexdigest(), "text/markdown") for n, d in files]
    assert all("content" not in a and "content_b64" not in a for a in meta)
    mid = r.json()["message"]["id"]

    inbox_meta = client.get("/api/v1/inbox", headers=auth(t_bob)).json()["messages"][0]["attachments"]
    assert inbox_meta == meta
    for (name, data), a in zip(files, meta):
        for token in (t_alice, t_bob):  # both participants
            d = client.get(f"/api/v1/messages/{mid}/attachments/{a['id']}", headers=auth(token))
            assert d.status_code == 200 and d.content == data
            assert d.headers["content-type"] == "text/markdown; charset=utf-8"
            assert d.headers["content-disposition"] == f'attachment; filename="{name}"'
            assert d.headers["x-content-type-options"] == "nosniff"
            assert d.headers["x-raincli-sha256"] == hashlib.sha256(data).hexdigest()
            assert d.headers["content-length"] == str(len(data))
            assert d.headers["cache-control"] == "no-store"


def test_sha_mismatch_and_bad_base64(client, world, engine):
    t = world["tokens"]["alice"]
    bad = att("a.md", b"hello", sha=hashlib.sha256(b"other").hexdigest())
    r = send(client, t, "bob-agent", attachments=[bad])
    assert r.status_code == 400 and err(r) == "invalid" and "a.md" in r.json()["error"]["message"]
    for b64 in ("aGVsbG8", "aGVs bG8=", "aGVsbG8=\n", "!!!!"):
        a = {"filename": "a.md", "content_b64": b64, "sha256": hashlib.sha256(b"hello").hexdigest()}
        assert err(send(client, t, "bob-agent", attachments=[a])) == "invalid", b64
    upper = att("a.md", b"hello")
    upper["sha256"] = upper["sha256"].upper()
    assert err(send(client, t, "bob-agent", attachments=[upper])) == "invalid"
    for shape in ({"filename": "a.md", "content_b64": "aGk="}, {**att("a.md", b"hi"), "x": 1}, "a.md"):
        assert err(send(client, t, "bob-agent", attachments=[shape])) == "invalid"
    assert err(send(client, t, "bob-agent", attachments={"a.md": "x"})) == "invalid"
    assert counts(engine) == (0, 0)


def test_bad_names_and_content(client, world, engine):
    t = world["tokens"]["alice"]
    for name in ("../x.md", "a/b.md", ".x.md", "a\\b.md", "x.txt", "x.md.exe", "con.md", "a..b.md",
                 "x" * 98 + ".md", " lead.md", ""):
        r = send(client, t, "bob-agent", attachments=[att(name, b"ok")])
        assert r.status_code == 400 and err(r) == "invalid", name
    for data in (b"", b"nul\x00byte", b"\xff\xfe not utf-8"):
        assert err(send(client, t, "bob-agent", attachments=[att("a.md", data)])) == "invalid"
    assert counts(engine) == (0, 0)


def test_duplicate_names_ignoring_case(client, world, engine):
    r = send(client, world["tokens"]["alice"], "bob-agent",
             attachments=[att("Report.md", b"a"), att("REPORT.md", b"b")])
    assert r.status_code == 400 and "duplicate" in r.json()["error"]["message"]
    assert counts(engine) == (0, 0)


def test_size_count_and_total_limits(client, world, engine):
    t = world["tokens"]["alice"]
    max_one = b"x" * security.ATTACHMENT_MAX_BYTES
    assert send(client, t, "bob-agent", attachments=[att("big.md", max_one)]).status_code == 201
    r = send(client, t, "bob-agent", attachments=[att("big.md", max_one + b"x")])
    assert r.status_code == 400 and err(r) == "invalid"
    six = [att(f"f{i}.md", b"x") for i in range(6)]
    assert err(send(client, t, "bob-agent", attachments=six)) == "invalid"
    assert send(client, t, "bob-agent", attachments=six[:5]).status_code == 201
    # 4 x 256 KiB = 1 MiB total is allowed; a fifth byte over is not
    four = [att(f"q{i}.md", max_one) for i in range(4)]
    assert send(client, t, "bob-agent", attachments=four).status_code == 201
    r = send(client, t, "bob-agent", attachments=four + [att("extra.md", b"x")])
    assert r.status_code == 400 and "total" in r.json()["error"]["message"]
    assert counts(engine) == (3, 1 + 5 + 4)


def test_request_body_limit_is_2mib_for_send_only(client, world):
    t = world["tokens"]["alice"]
    over = b'{"pad": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    r = client.post("/api/v1/messages", content=over, headers={**auth(t), "Content-Type": "application/json"})
    assert r.status_code == 413 and err(r) == "too_large"
    mid = send(client, t, "bob-agent").json()["message"]["id"]
    r = client.post(f"/api/v1/messages/{mid}/events", content=b"x" * (65 * 1024), headers=auth(t))
    assert r.status_code == 413


def test_idempotency_includes_attachments(client, world):
    t = world["tokens"]["alice"]
    mid = str(uuid.uuid4())
    files = [att("a.md", b"one"), att("b.md", b"two")]
    first = send(client, t, "bob-agent", "x", id=mid, attachments=files)
    assert first.status_code == 201
    retry = send(client, t, "bob-agent", "x", id=mid, attachments=files)
    assert retry.status_code == 200 and retry.json()["message"] == first.json()["message"]
    for changed in ([att("a.md", b"one"), att("b.md", b"TWO")], files[::-1], files[:1], None):
        extra = {"attachments": changed} if changed is not None else {}
        r = send(client, t, "bob-agent", "x", id=mid, **extra)
        assert r.status_code == 409 and err(r) == "id_conflict"


def test_download_authorization(client, world, session):
    t_alice, t_bob, t_eve = world["tokens"]["alice"], world["tokens"]["bob"], world["tokens"]["eve"]
    _, t_carol = identity.register_agent(session, world["teams"]["acme"], world["users"]["bob"], "carol-agent")
    session.commit()
    m1 = send(client, t_alice, "bob-agent", attachments=[att("a.md", b"secret one")]).json()["message"]
    m2 = send(client, t_alice, "bob-agent", attachments=[att("b.md", b"secret two")]).json()["message"]
    url = f"/api/v1/messages/{m1['id']}/attachments/{m1['attachments'][0]['id']}"
    for token in (t_carol, t_eve):  # same-team non-participant, other team
        r = client.get(url, headers=auth(token))
        assert r.status_code == 404 and err(r) == "not_found" and "secret" not in r.text
    # an attachment id from a different message is not reachable through this message
    cross = f"/api/v1/messages/{m1['id']}/attachments/{m2['attachments'][0]['id']}"
    assert client.get(cross, headers=auth(t_bob)).status_code == 404
    assert client.get(f"/api/v1/messages/{m1['id']}/attachments/{uuid.uuid4()}", headers=auth(t_bob)).status_code == 404
    assert client.get(f"/api/v1/messages/{m1['id']}/attachments/nope", headers=auth(t_bob)).status_code == 404
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(t_bob)).content == b"secret one"


def test_bad_third_attachment_stores_nothing(client, world, engine):
    files = [att("one.md", b"1"), att("two.md", b"2"), att("three.md", b"bad\x00")]
    r = send(client, world["tokens"]["alice"], "bob-agent", attachments=files)
    assert r.status_code == 400 and "three.md" in r.json()["error"]["message"]
    assert counts(engine) == (0, 0)


def test_db_failure_mid_insert_rolls_back_everything(world, engine, session, monkeypatch):
    """Atomicity at the DB level: if an attachment insert fails, neither message nor attachments remain."""
    original = messaging.validate_attachments

    def sneak_bad_row(attachments):  # bypass validation: a fourth row reusing the first filename
        files = original(attachments)
        return files + [(files[0][0], b"dup", security.sha256_hex(b"dup"))]

    monkeypatch.setattr(messaging, "validate_attachments", sneak_bad_row)
    alice = world["agents"]["alice"]
    try:
        messaging.send_message(session, alice, id=uuid.uuid4(), to_handle="bob-agent", body="x", max_pending=5,
                               attachments=[("one.md", b"1"), ("two.md", b"2"), ("three.md", b"3")])
        session.commit()
        raise AssertionError("expected the unique filename index to reject the fourth row")
    except Exception as exc:  # IntegrityError from uq_attachments_message_filename
        assert "uq_attachments_message_filename" in str(exc)
        session.rollback()
    assert counts(engine) == (0, 0)


def test_service_functions_for_web(session, world):
    alice = world["agents"]["alice"]
    msg, created = messaging.send_message(
        session, alice, id=uuid.uuid4(), to_handle="bob-agent", body="from web", max_pending=5,
        attachments=[("plan.md", b"# plan\n")])
    session.commit()
    meta = messaging.attachments_json(msg)
    assert created and [a["filename"] for a in meta] == ["plan.md"]
    got = messaging.get_attachment_for_agent(session, world["agents"]["bob"], msg.id, meta[0]["id"])
    assert bytes(got.content) == b"# plan\n"
    try:
        messaging.get_attachment_for_agent(session, world["agents"]["eve"], msg.id, meta[0]["id"])
        raise AssertionError("cross-team read must fail")
    except messaging.MessagingError as exc:
        assert exc.status == 404
