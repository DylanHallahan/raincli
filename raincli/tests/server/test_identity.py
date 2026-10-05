import pytest
from sqlalchemy import select, text

from raincli_server import identity, security
from raincli_server.models import AgentCredential


def test_migrations_created_schema(engine):
    with engine.connect() as conn:
        tables = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
    assert {"users", "teams", "agents", "agent_credentials", "messages", "delivery_events"} <= tables
    assert "alembic_version" in tables


def test_tokens_are_hashed_and_authenticate(session, world):
    token = world["tokens"]["alice"]
    stored = session.scalars(select(AgentCredential.token_hash)).all()
    assert security.hash_token(token) in stored and token not in stored
    auth = identity.authenticate_agent(session, token)
    assert auth.agent.handle == "alice-agent" and auth.team.slug == "acme"
    assert auth.has_scope("messages:send")
    assert identity.authenticate_agent(session, "rca_wrong") is None
    assert identity.authenticate_agent(session, None) is None


def test_rotate_invalidates_old_token(session, world):
    old = world["tokens"]["bob"]
    new = identity.rotate_agent_credential(session, world["agents"]["bob"], world["users"]["bob"])
    session.commit()
    assert identity.authenticate_agent(session, old) is None
    assert identity.authenticate_agent(session, new).agent.handle == "bob-agent"


def test_revoke_agent_blocks_all_credentials(session, world):
    identity.revoke_agent(session, world["agents"]["bob"], world["users"]["alice"])  # team owner
    session.commit()
    assert identity.authenticate_agent(session, world["tokens"]["bob"]) is None
    with pytest.raises(identity.IdentityError):
        identity.rotate_agent_credential(session, world["agents"]["bob"])


def test_non_owner_cannot_manage_others_agent(session, world):
    with pytest.raises(identity.PermissionDenied):
        identity.revoke_agent(session, world["agents"]["alice"], world["users"]["bob"])
    with pytest.raises(identity.PermissionDenied):
        identity.rotate_agent_credential(session, world["agents"]["alice"], world["users"]["eve"])


def test_invitation_single_use_and_owner_only(session, world):
    acme = world["teams"]["acme"]
    with pytest.raises(identity.PermissionDenied):
        identity.create_invitation(session, acme, world["users"]["bob"])
    inv, token = identity.create_invitation(session, acme, world["users"]["alice"], "carol@example.test")
    assert token.startswith("rci_") and inv.token_hash == security.hash_token(token)
    user, team = identity.accept_invitation(
        session, token, email="carol@example.test", display_name="Carol", password="another long password")
    assert team.slug == "acme" and identity.membership(session, acme.id, user.id).role == "member"
    with pytest.raises(identity.IdentityError):
        identity.accept_invitation(session, token, email="x@example.test", display_name="X", password="p" * 12)


def test_invitation_email_binding(session, world):
    _, token = identity.create_invitation(session, world["teams"]["acme"], world["users"]["alice"], "dan@example.test")
    with pytest.raises(identity.IdentityError):
        identity.accept_invitation(session, token, user=world["users"]["eve"])


def test_password_hashing():
    h = security.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and security.verify_password("correct horse battery", h)
    assert not security.verify_password("wrong password!!", h)
    assert not security.verify_password("x", "garbage")


def test_handle_and_body_validation():
    assert security.valid_handle("bob-agent") and not security.valid_handle("Bob") and not security.valid_handle("b")
    assert security.valid_message_body("hi\n\tthere")
    for bad in ["", "   ", "a\x1b[31m", "a ", "x" * 16001]:
        assert not security.valid_message_body(bad)


def test_attachment_validation():
    ok = ["report.md", "Q3 notes v2.md", "a.b-c_d.md", "R" + "x" * 94 + ".md"]
    bad = ["../x.md", "a/b.md", ".hidden.md", "x.txt", "x.MD.exe", "con.md", "a..b.md", "", "x" * 101 + ".md",
           "x\n.md", "é.md", None]
    assert all(security.valid_attachment_name(n) for n in ok)
    assert not any(security.valid_attachment_name(n) for n in bad)
    assert security.attachment_content_problem(b"# Title\r\nbody\n") is None
    assert security.attachment_content_problem(b"") is not None
    assert security.attachment_content_problem(b"a\x00b") is not None
    assert security.attachment_content_problem(b"\xff\xfe") is not None
    assert security.attachment_content_problem(b"x" * (256 * 1024 + 1)) is not None


def test_attachment_table_enforces_integrity(session, world):
    import uuid

    from sqlalchemy.exc import IntegrityError

    from raincli_server.models import Attachment, Conversation, Message

    a, b = world["agents"]["alice"], world["agents"]["bob"]
    lo, hi = sorted([a.id, b.id])
    conv = Conversation(team_id=a.team_id, agent_a_id=lo, agent_b_id=hi, a_key=f"m:{lo}", b_key=f"m:{hi}")
    session.add(conv)
    session.flush()
    msg = Message(id=uuid.uuid4(), team_id=a.team_id, conversation_id=conv.id,
                  sender_agent_id=a.id, recipient_agent_id=b.id, body="see attached")
    session.add(msg)
    session.flush()
    data = b"# Report\n"
    session.add(Attachment(message_id=msg.id, position=0, filename="Report.md", size=len(data),
                           sha256=security.sha256_hex(data), content=data))
    session.flush()
    session.add(Attachment(message_id=msg.id, position=1, filename="report.MD".replace("MD", "md"),
                           size=len(data), sha256=security.sha256_hex(data), content=data))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
