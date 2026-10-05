"""Person-session stage for scripts/runtime-platform-smoke.py (protocol §16.3, §16.4, §16.11).

Against a real throwaway server: uvicorn on a loopback port over a fresh PostgreSQL
database created for this run and dropped after it (RAINCLI_TEST_DATABASE_URL, as
scripts/test-postgres.sh provides). Two machines sign in headless on a pseudo-terminal
(`raincli login`, then `raincli login --person` after `me sign-out`), and the real CLI
processes run `me inbox --watch`, `send @email` with an attachment, `me read`,
`me fetch`, `me reply` and `me send`. Linux only (it types passwords on a pty);
skipped without a test database.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

PASSWORD = "person smoke password, typed on a pty only"


def _psycopg(url):
    return "postgresql+psycopg://" + url.split("://", 1)[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_stage(root, login_on_pty, scrub, show):
    """Returns False when skipped."""
    admin_url = os.environ.get("RAINCLI_TEST_DATABASE_URL")
    if not sys.platform.startswith("linux") or not admin_url:
        print("SKIP: person stage, needs Linux and RAINCLI_TEST_DATABASE_URL (scripts/test-postgres.sh)", flush=True)
        return False
    from sqlalchemy import create_engine, text
    from raincli_server.migrate import upgrade

    name = f"raincli_smoke_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_psycopg(admin_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _psycopg(admin_url.rsplit("/", 1)[0] + "/" + name)
    work = Path(tempfile.mkdtemp(prefix="raincli-person-"))
    server = None
    try:
        upgrade(url)
        seed(url)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("RAINCLI_")}
        server_env = dict(env, RAINCLI_DATABASE_URL=url, RAINCLI_SECRET_KEY="smoke-" + "k" * 40,
                          RAINCLI_PUBLIC_URL=base, RAINCLI_COOKIE_SECURE="0")
        log = open(work / "server.log", "wb")
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "raincli_server.app:app_from_env", "--factory",
                                   "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                                  cwd=root / "raincli", env=server_env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                with urllib.request.urlopen(base + "/api/v1/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the throwaway server did not become healthy")
        flow(work, base, env, login_on_pty, scrub)
        return True
    except BaseException:
        show("person stage", work / "server.log")
        raise
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=15)
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        shutil.rmtree(work, ignore_errors=True)


def seed(url):
    """One team, two members (no machines: they sign in below)."""
    from raincli_server import identity
    from raincli_server.db import make_engine, make_sessionmaker
    engine = make_engine(url)
    session = make_sessionmaker(engine)()
    alice = identity.create_user(session, "alice@smoke.test", "Alice Smoke", PASSWORD)
    bob = identity.create_user(session, "bob@smoke.test", "Bob Smoke", PASSWORD)
    team = identity.create_team(session, "smoke", "Smoke", alice)
    identity.add_member(session, team, bob)
    session.commit()
    session.close()
    engine.dispose()


def flow(work, base, env, login_on_pty, scrub):
    configs = {who: work / who / "agent.json" for who in ("alice", "bob")}
    outputs = []

    def cli(who, *args, input=None, check=True):
        proc = subprocess.run([sys.executable, "-m", "raincli_agent", "--config", str(configs[who]), *args],
                              env=env, input=input, capture_output=True, text=True, timeout=90)
        outputs.append(proc.stdout + proc.stderr)
        if check and proc.returncode != 0:
            raise AssertionError(f"raincli {args[:3]} as {who} -> {proc.returncode}: {scrub(proc.stderr)}")
        return proc

    for who in ("alice", "bob"):
        status, transcript = login_on_pty([sys.executable, "-m", "raincli_agent", "--config", str(configs[who]),
                                           "login", "--email", f"{who}@smoke.test", "--machine-name", f"{who}-box",
                                           "--api-url", base], PASSWORD)
        outputs.append(transcript)
        assert status == 0 and f"signed in as {who}-box" in transcript, scrub(transcript)
    # A person session can be ended and added again without touching the machine.
    cli("alice", "me", "sign-out")
    status, transcript = login_on_pty([sys.executable, "-m", "raincli_agent", "--config", str(configs["alice"]),
                                       "login", "--person", "--email", "alice@smoke.test"], PASSWORD)
    outputs.append(transcript)
    assert status == 0 and "added a person session" in transcript, scrub(transcript)
    assert (configs["alice"].parent / "person.json").stat().st_mode & 0o777 == 0o600
    print("PASS: person (real server): headless login and login --person on a pty; person.json 0600", flush=True)

    watch = subprocess.Popen([sys.executable, "-m", "raincli_agent", "--config", str(configs["alice"]), "me", "inbox",
                              "--watch", "--once", "--timeout", "60", "--json"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    note = work / "notes.md"
    note.write_bytes("# Release notes\n\n- ✓ done\n".encode())
    mid = str(uuid.uuid4())
    cli("bob", "send", "@alice@smoke.test", "--body-file", "-", "--id", mid, "--attach", str(note),
        input="Could you check the release notes?")
    stdout, stderr = watch.communicate(timeout=90)
    outputs.append(stdout + stderr)
    assert watch.returncode == 0, scrub(stderr)
    [seen] = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert seen["id"] == mid and seen["from"] == "bob-box" and not seen["acked_at"]
    print("PASS: person (real server): me inbox --watch printed bob's message as it arrived (not marked read)",
          flush=True)

    shown = cli("alice", "me", "read", mid).stdout
    assert "| Could you check the release notes?" in shown
    assert cli("alice", "me", "inbox", "--json").stdout.strip() == ""  # reading marked it read
    dest = work / "downloads"
    cli("alice", "me", "fetch", mid, "--attachment", "1", "--to", str(dest))
    assert (dest / "notes.md").read_bytes() == note.read_bytes()
    rid = str(uuid.uuid4())
    cli("alice", "me", "reply", mid, "--body-file", "-", "--id", rid, input="Looks good.")
    reply = json.loads(cli("bob", "show", rid, "--json").stdout)["message"]
    assert reply["from"] == "@alice@smoke.test" and reply["to"] == "bob-box" and reply["in_reply_to"] == mid
    sent = json.loads(cli("alice", "me", "send", "bob-box", "--body-file", "-", "--json", input="ping").stdout)
    assert sent["message"]["to"] == "bob-box" and sent["created"]
    assert cli("alice", "me", "send", "bob-box", "--body", "x", check=False).returncode == 2  # never from argv
    print("PASS: person (real server): me read, me fetch (verified bytes), me reply to the machine, me send",
          flush=True)

    secrets_found = []
    for who in ("alice", "bob"):
        data = json.loads((configs[who].parent / "person.json").read_text())
        secrets_found.append(data["person_session"])
        secrets_found.append(json.loads((configs[who].parent / "app-install.json").read_text())["app_install_token"])
    joined = "\n".join(outputs)
    assert PASSWORD not in joined and not any(s in joined for s in secrets_found)
    print("PASS: person (real server): no password, person session or app token in any output", flush=True)
