"""Real-release pushed-update end to end, against a throwaway local server.

Exercises the product's real GitHub release path (the canonical repository's stable
release, the tag resolved to its commit, the archive checked against that commit,
a staged environment built without pip) on this machine:

1. a disposable PostgreSQL on loopback: a fresh ``initdb`` cluster (the Windows
   runner's preinstalled PostgreSQL, found through PGBIN), or a throwaway database
   on the admin URL in RAINCLI_TEST_DATABASE_URL (local test PostgreSQL);
2. the server from this checkout, migrated, on 127.0.0.1 with a random secret key;
3. a disposable user, team and machine handle enrolled through raincli-admin;
4. a managed client install of --from-version through the real updater;
5. the runtime and a connector (with no Herdr) started through the managed launcher;
6. a pushed upgrade to --to-version with raincli-admin set-client-version;
7. unless --no-downgrade: a refused downgrade, then an allowed one, then --clear.

No production secret, server or team is involved: every credential is created here
and deleted with the temporary directory. See docs/release-testing.md.
"""
import argparse
import contextlib
import glob
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "raincli"
sys.path.insert(0, str(PKG))
from raincli_agent.runtime import updates  # noqa: E402  (the checkout's real updater)

TEAM, HANDLE, EMAIL = "release-e2e", "e2e-machine", "release-e2e@example.invalid"
WAIT = 900  # seconds per phase: download, staged install, graceful handover, two reports


class Failure(Exception):
    pass


def say(text):
    print(text, flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(what, check, timeout=WAIT, interval=2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    raise Failure(f"timed out after {timeout} s waiting for {what}")


class Secrets:
    """Everything this run generates that must never be printed."""

    def __init__(self):
        self.values = set()

    def add(self, value):
        self.values.add(value)
        return value

    def scrub(self, text):
        for value in self.values:
            text = text.replace(value, "<redacted>")
        return re.sub(r"rc[ai]_[A-Za-z0-9_-]{8,}", "rc?_<redacted>", text)


SECRETS = Secrets()


def tail(path, limit=8000):
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"<unreadable: {type(exc).__name__}>"
    return SECRETS.scrub(text[-limit:])


# -- PostgreSQL ------------------------------------------------------------------

def postgres_bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = [os.environ.get("PGBIN", "")]
    candidates += sorted(glob.glob("C:/Program Files/PostgreSQL/*/bin"), reverse=True) if os.name == "nt" else \
        sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for directory in candidates:
        if directory and (Path(directory) / exe).is_file():
            return str(Path(directory) / exe)
    return shutil.which(name)


class Cluster:
    """A fresh initdb cluster on a random loopback port, owned by this run."""

    def __init__(self, work):
        self.work = work
        self.data = work / "data"
        self.log = work / "postgres.log"
        self.port = free_port()
        self.password = SECRETS.add(secrets.token_urlsafe(24))
        self.started = False

    def start(self):
        initdb, pg_ctl = postgres_bin("initdb"), postgres_bin("pg_ctl")
        if not initdb or not pg_ctl:
            raise Failure("no PostgreSQL found: set RAINCLI_TEST_DATABASE_URL, or PGBIN to a PostgreSQL bin directory")
        self.pg_ctl = pg_ctl
        pwfile = self.work / "pwfile"
        pwfile.write_text(self.password + "\n")
        try:
            subprocess.run([initdb, "-D", str(self.data), "-U", "raincli", f"--pwfile={pwfile}", "-A", "scram-sha-256",
                            "-E", "UTF8", "--no-locale"], check=True, capture_output=True, text=True, timeout=180)
        except subprocess.CalledProcessError as exc:
            raise Failure("initdb failed: " + SECRETS.scrub((exc.stdout or "") + (exc.stderr or ""))[-2000:]) from None
        finally:
            pwfile.unlink()
        options = f"-p {self.port} -c listen_addresses=127.0.0.1"
        if os.name != "nt":
            sockets = self.work / "sock"
            sockets.mkdir()
            options += f" -k {sockets}"
        subprocess.run([pg_ctl, "-D", str(self.data), "-l", str(self.log), "-w", "-t", "60", "-o", options, "start"],
                       check=True, capture_output=True, timeout=90)
        self.started = True
        return f"postgresql://raincli:{self.password}@127.0.0.1:{self.port}/postgres"

    def stop(self):
        if self.started:
            subprocess.run([self.pg_ctl, "-D", str(self.data), "-m", "fast", "-w", "-t", "60", "stop"],
                           capture_output=True, timeout=90)


class ThrowawayDatabase:
    """A throwaway database on an existing test server (never production)."""

    def __init__(self, admin_url, server):
        self.admin_url, self.server = admin_url, server
        self.name = "raincli_release_e2e_" + secrets.token_hex(6)
        self.created = False

    def _run(self, sql):
        code = ("import sys, psycopg\n"
                "with psycopg.connect(sys.argv[1], autocommit=True) as c: c.execute(sys.argv[2])")
        self.server.python("-c", code, self.admin_url, sql)

    def start(self):
        self._run(f'CREATE DATABASE "{self.name}"')
        self.created = True
        return self.admin_url.rsplit("/", 1)[0] + "/" + self.name

    def stop(self):
        if self.created:
            self._run(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')


# -- the throwaway server -----------------------------------------------------------

class Server:
    def __init__(self, work, python=None):
        self.work = work
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = work / "server.log"
        self.process = None
        self._python = python
        self.env = None

    def prepare(self):
        if self._python is None:
            venv = self.work / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=180)
            self._python = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            lock = (PKG / "requirements.lock").read_text().splitlines()
            if os.name == "nt":  # uvloop has no Windows build; uvicorn then uses asyncio
                lock = [line for line in lock if not line.lower().startswith("uvloop")]
            requirements = self.work / "requirements.txt"
            requirements.write_text("\n".join(lock) + "\n")
            subprocess.run([self._python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "-r", str(requirements)], check=True, timeout=900)
        say(f"server python: {self._python}")

    def configure(self, database_url):
        self.env = {**os.environ, "PYTHONPATH": str(PKG), "PYTHONUTF8": "1",
                    "RAINCLI_DATABASE_URL": database_url,
                    "RAINCLI_SECRET_KEY": SECRETS.add(secrets.token_urlsafe(48)),
                    "RAINCLI_PUBLIC_URL": self.url, "RAINCLI_COOKIE_SECURE": "0"}

    def python(self, *args, input=None):
        env = self.env or {**os.environ, "PYTHONPATH": str(PKG)}
        try:
            return subprocess.run([self._python, *args], env=env, input=input, capture_output=True, text=True,
                                  check=True, timeout=300, cwd=str(PKG))
        except subprocess.CalledProcessError as exc:
            raise Failure(f"server command {args[:3]} failed: "
                          + SECRETS.scrub((exc.stdout or "") + (exc.stderr or ""))[-3000:]) from None

    def admin(self, *args, input=None):
        return self.python("-m", "raincli_server.admin", *args, input=input)

    def start(self):
        self.admin("migrate")
        with open(self.log, "wb") as output:
            self.process = subprocess.Popen(
                [self._python, "-m", "uvicorn", "raincli_server.app:app_from_env", "--factory",
                 "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log"],
                env=self.env, cwd=str(PKG), stdout=output, stderr=subprocess.STDOUT)

        def healthy():
            if self.process.poll() is not None:
                raise Failure("the server exited: " + tail(self.log, 3000))
            try:
                with urllib.request.urlopen(self.url + "/api/v1/health", timeout=5) as response:
                    return json.loads(response.read()).get("ok")
            except (OSError, ValueError):
                return None
        wait_for("the server's health check", healthy, timeout=90, interval=1)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()


# -- the managed client ---------------------------------------------------------------

def process_cmdline(pid):
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                                 capture_output=True, text=True, timeout=30).stdout
            return out.strip()
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def kill_marked(markers):
    """Last resort: kill processes whose command line names this run's client directory."""
    fold = str.casefold if os.name == "nt" else str
    marks = [fold(m) for m in markers]
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
                              "ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.CommandLine }"],
                             capture_output=True, text=True, timeout=60).stdout
        pids = [int(line.split(" ", 1)[0]) for line in out.splitlines()
                if line.strip() and any(m in fold(line) for m in marks)]
        for pid in pids:
            if pid != os.getpid():
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return pids
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            line = process_cmdline(entry.name)
            if any(m in line for m in marks):
                pids.append(int(entry.name))
                with contextlib.suppress(OSError):
                    os.kill(int(entry.name), signal.SIGKILL)
    return pids


class Client:
    def __init__(self, work, server):
        self.work = work
        self.server = server
        self.root = work / "managed"
        self.home = work / "home"
        self.state = work / "runtime-state"
        self.agent = work / "agent.json"
        self.config = work / "runtime.json"
        self.launch_log = work / "launcher.log"
        self.launcher = None
        self.home.mkdir(parents=True)
        # Children never see a Herdr session or this checkout's modules, and get a private HOME.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("HERDR") and k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), PYTHONUTF8="1")

    def install(self, tag):
        """The real updater: the canonical stable release with exactly this tag."""
        try:
            release = updates.resolve(tag)
        except updates.ReleaseNotFound:
            raise Failure(f"release {tag} not found in {updates.REPO} (publish it as a stable release first)") from None
        result = updates.install(self.root, release)
        if result.get("status") != "installed":
            raise Failure(f"managed install of {tag} returned {result.get('status')}")
        return release

    def launch(self, *args, check=True):
        result = subprocess.run([sys.executable, str(self.root / "launch.py"), *args], env=self.env, capture_output=True,
                                text=True, timeout=180, cwd=str(self.work))
        if check and result.returncode != 0:
            raise Failure(f"launch.py {' '.join(args[:2])} exited {result.returncode}: "
                          + SECRETS.scrub(result.stdout + result.stderr)[-3000:])
        return result

    def pointer(self):
        return updates.read_pointer(self.root)

    def status(self):
        try:
            return json.loads((self.state / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def configure_runtime(self):
        connector = self.work / "connector.json"
        _write_private(connector, {"agent_config": str(self.agent), "herdr_agent": "e2e-inbox",
                                   "herdr_bin": "raincli-e2e-no-herdr", "state_dir": str(self.work / "queue"),
                                   "poll_wait": 1})
        _write_private(self.config, {"connectors": [str(connector)], "state_dir": str(self.state)})

    def start(self):
        with open(self.launch_log, "ab") as output:
            self.launcher = subprocess.Popen([sys.executable, str(self.root / "launch.py"), "runtime", "run",
                                              "--config", str(self.config)], env=self.env, cwd=str(self.work),
                                             stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def stop(self):
        if (self.root / "launch.py").is_file() and self.config.is_file():
            with contextlib.suppress(Exception):
                self.launch("runtime", "stop", "--config", str(self.config), check=False)
            with contextlib.suppress(Failure):
                wait_for("the runtime to stop", lambda: self.status().get("status") in ("stopped", None, ""),
                         timeout=150, interval=2)
        if self.launcher is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.launcher.wait(timeout=30)
        killed = kill_marked({str(self.work), str(self.work.resolve())})
        if killed:
            say(f"cleanup: killed leftover client processes {killed}")

    def runs(self, tag):
        """The runtime reports tag as current, from the pointer's interpreter, and has published."""
        status, pointer = self.status(), self.pointer()
        client = status.get("client") or {}
        connectors = status.get("connectors") or []
        if (pointer.get("tag") != tag or client.get("version") != tag[1:] or client.get("update_state") != "current"
                or status.get("status") != "running" or not connectors or not connectors[0].get("reported")):
            return None
        cmdline = process_cmdline(status.get("pid", 0))
        fold = str.casefold if os.name == "nt" else str
        return status if cmdline and fold(str(Path(pointer["python"]))) in fold(cmdline) else None


def _write_private(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def directory_entry(server, token):
    request = urllib.request.Request(server.url + "/api/v1/agents", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=15) as response:
        agents = json.loads(response.read())["agents"]
    return next(a for a in agents if a["handle"] == HANDLE)


def server_shows(server, token, version, state="current", error=None):
    try:
        entry = directory_entry(server, token)
    except (OSError, ValueError, StopIteration):
        return None
    machine = entry.get("machine") or {}
    ok = (machine.get("client_version") == version[1:] and machine.get("update_state") == state
          and machine.get("error") == error and entry["presence"]["status"] != "offline")
    return entry if ok else None


# -- the run --------------------------------------------------------------------------

def run(args, work, stack):
    server = Server(work / "server", args.server_python)
    server.work.mkdir()
    server.prepare()
    admin_url = os.environ.get("RAINCLI_TEST_DATABASE_URL", "")
    if admin_url:
        SECRETS.add(admin_url)
        database = ThrowawayDatabase(admin_url, server)
        say("database: a throwaway database on RAINCLI_TEST_DATABASE_URL")
    else:
        # The cluster lives OUTSIDE the mkdtemp work dir. Since Python 3.13, mkdtemp's 0o700 applies a
        # restrictive ACL on Windows (owner-only; for an administrator the owner is the Administrators
        # group), and initdb/postgres re-run themselves with a restricted token in which Administrators
        # is deny-only, so they could not open their own files there ("initdb: could not open file
        # ...\pg\pwfile"). A plain mkdir inherits the user's TEMP ACL, which is already private to the
        # runner user. The pwfile exists only for the initdb call.
        pg = Path(tempfile.gettempdir()) / f"raincli-rel-pg-{secrets.token_hex(6)}"
        pg.mkdir()
        (work / "pg-path").write_text(str(pg))  # lets diagnose() find the cluster log
        stack.callback(shutil.rmtree, pg, True)  # registered before database.stop, so it runs after it
        database = Cluster(pg)
        say("database: a fresh initdb cluster on loopback")
    url = SECRETS.add(database.start())
    stack.callback(database.stop)
    server.configure(url)
    server.start()
    stack.callback(server.stop)
    say(f"PASS: throwaway server from this checkout on {server.url}, migrated")

    password = SECRETS.add(secrets.token_urlsafe(18))
    server.admin("create-user", "--email", EMAIL, "--name", "Release E2E", input=password + "\n")
    server.admin("create-team", "--slug", TEAM, "--name", "Release E2E", "--owner", EMAIL)
    client = Client(work / "client", server)
    server.admin("register-agent", "--team", TEAM, "--owner", EMAIL, "--handle", HANDLE, "--out", str(client.agent))
    token = SECRETS.add(json.loads(client.agent.read_text())["token"])
    say(f"PASS: disposable user, team {TEAM} and machine {HANDLE} enrolled with raincli-admin")

    first = client.install(args.from_version)
    stack.callback(client.stop)
    version = client.launch("--version").stdout.strip()
    pointer = client.pointer()
    if version != "raincli " + args.from_version[1:] or pointer.get("update_mode") != "automatic":
        raise Failure(f"managed install runs {version!r} in mode {pointer.get('update_mode')!r}")
    say(f"PASS: real release {args.from_version} (commit {first['commit'][:12]}) installed managed, "
        f"automatic, and runs through the launcher")

    client.configure_runtime()
    client.start()
    wait_for(f"the runtime on {args.from_version}", lambda: client.runs(args.from_version), timeout=180)
    wait_for("the server to show the machine", lambda: server_shows(server, token, args.from_version), timeout=120)
    say(f"PASS: runtime started through the launcher on {args.from_version} and reports to the server")

    server.admin("set-client-version", "--team", TEAM, args.to_version)
    wait_for(f"the pushed update to {args.to_version}", lambda: client.runs(args.to_version))
    wait_for(f"the server to show {args.to_version}", lambda: server_shows(server, token, args.to_version), timeout=120)
    say(f"PASS: pushed {args.to_version}: pointer, runtime interpreter, status and server directory all current")

    if args.downgrade:
        server.admin("set-client-version", "--team", TEAM, args.from_version)
        wait_for("the refused downgrade to be reported", lambda: server_shows(
            server, token, args.to_version, "failed", "downgrade_not_allowed"), timeout=180)
        time.sleep(40)  # at least one more report: nothing may be installed meanwhile
        status = client.status()
        if (client.pointer().get("tag") != args.to_version
                or (status.get("client") or {}).get("version") != args.to_version[1:]
                or not server_shows(server, token, args.to_version, "failed", "downgrade_not_allowed")):
            raise Failure("the version changed without --allow-downgrade")
        say(f"PASS: downgrade to {args.from_version} refused without --allow-downgrade; still on {args.to_version}")

        server.admin("set-client-version", "--team", TEAM, args.from_version, "--allow-downgrade")
        wait_for(f"the allowed downgrade to {args.from_version}", lambda: client.runs(args.from_version))
        wait_for(f"the server to show {args.from_version}", lambda: server_shows(server, token, args.from_version),
                 timeout=120)
        say(f"PASS: downgrade to {args.from_version} with --allow-downgrade: pointer, interpreter and server current")

        server.admin("set-client-version", "--team", TEAM, "--clear")
        wait_for("the cleared target to be reported", lambda: server_shows(server, token, args.from_version),
                 timeout=120)
        status_out = server.admin("client-status", "--team", TEAM).stdout
        if not status_out.startswith("target\tnone"):
            raise Failure("client-status still shows a target after --clear")
        say(f"PASS: target cleared; {HANDLE} stays on {args.from_version}")
    say("client-status:\n" + SECRETS.scrub(server.admin("client-status", "--team", TEAM).stdout).rstrip())
    return server, client


def diagnose(work):
    say("===== DIAGNOSTICS (secrets redacted) =====")
    client = work / "client"
    for path in [client / "runtime-state/status.json", client / "managed/current.json",
                 client / "managed/update-state.json", client / "managed/update-mode.json",
                 client / "launcher.log", client / "managed/runtime.log", client / "managed/update.log"]:
        if path.exists():
            say(f"--- {path.relative_to(work)} ---\n{tail(path)}")
    for path in sorted((client / "runtime-state").glob("connector-*.log*")):
        say(f"--- {path.relative_to(work)} ---\n{tail(path, 4000)}")
    for path in sorted((client / "managed/versions").glob("*")):
        say(f"managed version dir: {path.name}")
    pg_path = work / "pg-path"
    pg_log = Path(pg_path.read_text()) / "postgres.log" if pg_path.exists() else work / "pg/postgres.log"
    for path in (work / "server/server.log", pg_log):
        if path.exists():
            say(f"--- {path.relative_to(work)} ---\n{tail(path, 4000)}")
    say("===== END DIAGNOSTICS =====")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--from-version", default=os.environ.get("FROM_VERSION") or "v0.3.0")
    parser.add_argument("--to-version", default=os.environ.get("TO_VERSION") or "v0.3.1")
    parser.add_argument("--no-downgrade", dest="downgrade", action="store_false",
                        default=os.environ.get("DOWNGRADE", "true").lower() not in ("false", "0", "no"))
    parser.add_argument("--server-python", help="an existing Python with raincli/requirements.lock installed")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory (holds credentials)")
    args = parser.parse_args(argv)
    for name in ("from_version", "to_version"):
        tag = getattr(args, name)
        if not updates.TAG_RE.fullmatch(tag) or updates.version_key(tag) < updates.MIN_TARGET:
            parser.error(f"{tag!r} is not a release tag vX.Y.Z of v0.3.0 or later")
    if updates.version_key(args.from_version) >= updates.version_key(args.to_version):
        parser.error("--to-version must be newer than --from-version")
    return args


def main(argv=None):
    args = parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="raincli-rel-"))
    say(f"release update e2e: {args.from_version} -> {args.to_version}"
        f"{' -> downgrade' if args.downgrade else ''}; work dir {work}")
    ok = False
    try:
        with contextlib.ExitStack() as stack:
            try:
                run(args, work, stack)
                ok = True
            except BaseException as exc:
                say(f"FAIL: {SECRETS.scrub(str(exc)) or type(exc).__name__}")
                if not isinstance(exc, Failure) or "not found" not in str(exc):
                    diagnose(work)
                if not isinstance(exc, (Failure, KeyboardInterrupt)):
                    raise
    finally:
        if args.keep:
            say(f"kept {work} (it holds throwaway credentials; delete it when done)")
        else:
            shutil.rmtree(work, ignore_errors=True)
    say("RELEASE UPDATE E2E PASSED" if ok else "RELEASE UPDATE E2E FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
