"""raincli-admin CLI and the migration roundtrip."""

import io
import json
import os
import stat
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from api_helpers import auth
from conftest import TEST_ADMIN_URL, _psycopg_url
from raincli_server import admin, identity
from raincli_server.migrate import alembic_config


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


def run(database_url, *argv, stdin_text="", tty=False, prompt=None, extra_env=None):
    out, errs = io.StringIO(), io.StringIO()
    env = {"RAINCLI_DATABASE_URL": database_url, "RAINCLI_PUBLIC_URL": "https://agents.example.test",
           "RAINCLI_ROOT_PATH": "/rc", **(extra_env or {})}
    kwargs = {"prompt": prompt} if prompt else {}
    code = admin.main(list(argv), env=env, stdin=FakeStdin(stdin_text, tty), stdout=out, stderr=errs, **kwargs)
    return code, out.getvalue(), errs.getvalue()


def test_password_is_never_an_argument():
    parser = admin.build_parser()
    for action in parser._subparsers._group_actions[0].choices["create-user"]._actions:
        assert "password" not in " ".join(action.option_strings)
    with pytest.raises(SystemExit):
        parser.parse_args(["create-user", "--email", "a@b.test", "--name", "A", "--password", "x"])


def test_admin_end_to_end(database_url, engine, client, tmp_path):
    code, out, _ = run(database_url, "create-user", "--email", "op@example.test", "--name", "Op",
                       stdin_text="operator password 1\n")
    assert code == 0 and "operator password" not in out
    assert run(database_url, "create-user", "--email", "m@example.test", "--name", "M",
               tty=True, prompt=lambda _: "member password 12")[0] == 0
    code, _, errs = run(database_url, "create-user", "--email", "z@example.test", "--name", "Z",
                        tty=True, prompt=lambda p, it=iter(["password one 12", "password two 12"]): next(it))
    assert code == 1 and "do not match" in errs
    assert run(database_url, "create-team", "--slug", "ops", "--name", "Ops", "--owner", "op@example.test")[0] == 0
    assert run(database_url, "add-member", "--team", "ops", "--email", "m@example.test")[0] == 0
    code, out, _ = run(database_url, "invite", "--team", "ops", "--by", "op@example.test")
    assert code == 0 and out.strip().startswith("https://agents.example.test/rc/invite/rci_")
    assert run(database_url, "invite", "--team", "ops", "--by", "m@example.test")[0] == 1  # not an owner

    cfg = tmp_path / "op.json"
    code, out, errs = run(database_url, "register-agent", "--team", "ops", "--owner", "op@example.test",
                          "--handle", "op-agent", "--out", str(cfg))
    assert code == 0 and "rca_" not in out + errs
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    conf = json.loads(cfg.read_text())
    assert conf["api_url"] == "https://agents.example.test/rc" and conf["token"].startswith("rca_")
    assert client.get("/api/v1/me", headers=auth(conf["token"])).json()["agent"]["handle"] == "op-agent"

    # refuses to overwrite, and registers nothing when it refuses
    code, _, errs = run(database_url, "register-agent", "--team", "ops", "--owner", "op@example.test",
                        "--handle", "op-two", "--out", str(cfg))
    assert code == 1 and "exists" in errs
    code, out, _ = run(database_url, "list-agents", "--team", "ops")
    assert "op-two" not in out and "op-agent" in out and "rca_" not in out.replace(conf["token"][:12], "")
    assert conf["token"] not in out

    # without --out the token goes to stdout only, with a warning on stderr
    code, out, errs = run(database_url, "register-agent", "--team", "ops", "--owner", "m@example.test",
                          "--handle", "m-agent")
    assert code == 0 and out.strip().startswith("rca_") and "warning" in errs and "rca_" not in errs
    m_token = out.strip()

    rotated = tmp_path / "op2.json"
    assert run(database_url, "rotate-agent", "--team", "ops", "--handle", "op-agent", "--out", str(cfg))[0] == 1
    assert run(database_url, "rotate-agent", "--team", "ops", "--handle", "op-agent", "--out", str(rotated))[0] == 0
    assert stat.S_IMODE(os.stat(rotated).st_mode) == 0o600
    new_token = json.loads(rotated.read_text())["token"]
    assert client.get("/api/v1/me", headers=auth(conf["token"])).status_code == 401
    assert client.get("/api/v1/me", headers=auth(new_token)).status_code == 200

    assert run(database_url, "revoke-agent", "--team", "ops", "--handle", "m-agent")[0] == 0
    assert client.get("/api/v1/me", headers=auth(m_token)).status_code == 401
    code, out, _ = run(database_url, "list-agents")
    lines = {line.split("\t")[1]: line for line in out.strip().splitlines()}
    assert "revoked" in lines["m-agent"] and "active" in lines["op-agent"]
    assert new_token not in out and m_token not in out


def test_admin_requires_database_url(tmp_path):
    out, errs = io.StringIO(), io.StringIO()
    code = admin.main(["list-agents"], env={}, stdin=FakeStdin(), stdout=out, stderr=errs)
    assert code == 1 and "RAINCLI_DATABASE_URL" in errs.getvalue()


def test_migration_downgrade_upgrade_roundtrip():
    if not TEST_ADMIN_URL:
        pytest.skip("RAINCLI_TEST_DATABASE_URL not set")
    from alembic import command

    name = f"raincli_mig_{uuid.uuid4().hex[:12]}"
    server = create_engine(_psycopg_url(TEST_ADMIN_URL), isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _psycopg_url(TEST_ADMIN_URL.rsplit("/", 1)[0] + "/" + name)
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        db = create_engine(url)
        try:
            assert "messages" in inspect(db).get_table_names()
            command.downgrade(cfg, "base")
            assert set(inspect(db).get_table_names()) <= {"alembic_version"}
            command.upgrade(cfg, "head")
            assert {"messages", "agents", "delivery_events", "agent_presence", "machine_agents",
                    "client_targets"} <= set(inspect(db).get_table_names())
            command.downgrade(cfg, "0003")  # 0004 drops only its own tables and columns
            tables = set(inspect(db).get_table_names())
            assert "agent_presence" in tables and not {"machine_agents", "client_targets"} & tables
            assert "client_version" not in {c["name"] for c in inspect(db).get_columns("agent_presence")}
            command.upgrade(cfg, "head")
            # admin migrate is idempotent at head
            assert run(url, "migrate")[0] == 0
        finally:
            db.dispose()
    finally:
        with server.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()
