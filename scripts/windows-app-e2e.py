"""Windows app end to end, against a throwaway in-job server and a local fake release endpoint.

Runs ONLY on a disposable GitHub Actions Windows runner (it edits the runner's hosts file,
LocalMachine Root store, HKCU Run value and user PATH, and installs into its profile). It
takes two installers built from the same source as 0.4.0 and 0.4.1 by
packaging/windows/build.py, and checks (protocol §15, §15.8):

1. a silent per-user install with no admin: the layout, install.json, the HKCU Run value and
   uninstall key, the shim first on the user PATH, the Start menu entries, the H7 record;
2. first-run sign-in through the installed CLI's login (`raincli.exe login`), with the
   password typed into a ConPTY prompt, never argv or the environment; the credential is
   stored DPAPI-protected and the runtime config is machine mode;
3. launching exactly what the Run value names; presence reports 0.4.0, automatic, current;
4. a pushed upgrade to 0.4.1 through the installer-asset path: the release resolved from the
   canonical repository, both assets fetched through the API asset URL and redirected to the
   exact GitHub download hosts, /UPDATE changing neither the Run value nor the uninstall key,
   install.json swapped and the app relaunched from versions\\0.4.1;
5. the downgrade to 0.4.0 refused, then allowed with --allow-downgrade;
6. sign-out through the PATH shim, then uninstall: what M10 removes is gone;
7. migration from an old pip-installed client (0.2.0 from its release archive) with a
   connector config that omits agent_config and a queue: a fresh install's first run keeps
   the handle and credential (no new machine) and connector delivery continues;
8. uninstall without sign-out: agent.json, the connector config and the queue are kept.

Release traffic goes to the REAL hostnames (api.github.com, github.com,
objects.githubusercontent.com, release-assets.githubusercontent.com): a hosts-file entry points
them at 127.0.0.1:443, where a fake release endpoint serves a certificate from a test root CA
added to the runner's LocalMachine Root store (§15.8 M11). The shipped client has no override.
Every credential is generated here and never printed. See docs/release-testing.md.

With ``--real FROM TO`` (for example ``--real v0.4.0 v0.4.1``) it skips the fake endpoint, the
hosts file and the test CA: it downloads both versions' installer assets from the published
releases of the canonical repository, and the pushed update fetches the real assets from GitHub.
"""
import argparse
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "raincli"
sys.path.insert(0, str(PKG))
_spec = importlib.util.spec_from_file_location("release_e2e", ROOT / "scripts" / "windows-release-update-e2e.py")
release_e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_e2e)
from raincli_agent.runtime import updates  # noqa: E402  (REPO, the canonical repository: a constant)

Failure, SECRETS, say, wait_for, tail = (release_e2e.Failure, release_e2e.SECRETS, release_e2e.say,
                                         release_e2e.wait_for, release_e2e.tail)

TEAM, EMAIL = "app-e2e", "app-e2e@example.invalid"
MACHINE, OBSERVER, OLD_MACHINE = "e2e-app-machine", "e2e-observer", "e2e-pip-machine"
OLD, NEW = "0.4.0", "0.4.1"
OLD_PIP = "v0.2.0"
HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
HOSTS_FILE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
HOSTS_MARK = "# raincli-app-e2e"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1E3B52-8C4D-4A7B-9E21-5D0C7A9B3F14}_is1"
WAIT = 900


def profile():
    return Path(os.environ["USERPROFILE"])


def app_root():
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / "RainCLI"


def default_agent_config():
    return profile() / ".config" / "raincli" / "agent.json"


# -- the Windows bits ---------------------------------------------------------------------

def reg_get(key, name, hive=None):
    import winreg

    try:
        with winreg.OpenKey(hive or winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except FileNotFoundError:
        return None


def reg_values(key, hive=None):
    import winreg

    try:
        with winreg.OpenKey(hive or winreg.HKEY_CURRENT_USER, key) as handle:
            values, i = {}, 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(handle, i)
                except OSError:
                    return values
                values[name] = data
                i += 1
    except FileNotFoundError:
        return None


def user_path_entries():
    return [p.strip().rstrip("\\") for p in (reg_get("Environment", "Path") or "").split(";") if p.strip()]


def running_app_exes():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'RainCLI.exe','RainCLI-app.exe',"
                          "'raincli.exe' } | ForEach-Object { $_.ExecutablePath }"],
                         capture_output=True, text=True, timeout=60).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def run(args, check=True, timeout=300, **kw):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout, **kw)
    if check and result.returncode != 0:
        raise Failure(f"{Path(str(args[0])).name} {' '.join(str(a) for a in args[1:3])} exited {result.returncode}: "
                      + SECRETS.scrub(result.stdout + result.stderr)[-3000:])
    return result


def openssl():
    found = shutil.which("openssl") or next(
        (p for p in (r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\OpenSSL\bin\openssl.exe")
         if Path(p).is_file()), None)
    if not found:
        raise Failure("openssl not found (Git for Windows ships one)")
    return found


# -- the fake release endpoint on the real hostnames ------------------------------------------

class TestCA:
    """A throwaway root CA in LocalMachine\\Root and a leaf for exactly the four release hosts."""

    def __init__(self, work):
        self.work = work
        self.cn = f"RainCLI app e2e test root {secrets.token_hex(4)}"
        self.added = False

    def create(self):
        tool, w = openssl(), self.work
        (w / "leaf.ext").write_text(
            "subjectAltName=" + ",".join(f"DNS:{h}" for h in HOSTS) + "\n"
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
        run([tool, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", w / "ca.key", "-out", w / "ca.pem",
             "-days", "2", "-subj", f"/CN={self.cn}", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash"])
        run([tool, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", w / "leaf.key", "-out", w / "leaf.csr",
             "-subj", "/CN=api.github.com"])
        run([tool, "x509", "-req", "-in", w / "leaf.csr", "-CA", w / "ca.pem", "-CAkey", w / "ca.key",
             "-CAcreateserial", "-out", w / "leaf.pem", "-days", "2", "-extfile", w / "leaf.ext"])
        run(["certutil", "-addstore", "-f", "Root", w / "ca.pem"])
        self.added = True
        (w / "ca.key").unlink()  # nothing more is signed with it

    def remove(self):
        if self.added:
            run(["certutil", "-delstore", "Root", self.cn], check=False)


class HostsEntry:
    def add(self):
        text = HOSTS_FILE.read_text("utf-8", errors="replace")
        if HOSTS_MARK in text:
            raise Failure("the hosts file already has this e2e's entry; is another run active?")
        line = "127.0.0.1 " + " ".join(HOSTS) + f"  {HOSTS_MARK}\n"
        HOSTS_FILE.write_text(text.rstrip("\n") + "\n" + line, "utf-8")
        run(["ipconfig", "/flushdns"], check=False)
        for host in HOSTS:
            if socket.gethostbyname(host) != "127.0.0.1":
                raise Failure(f"{host} does not resolve to 127.0.0.1 through the hosts file")

    def remove(self):
        with contextlib.suppress(OSError):
            lines = HOSTS_FILE.read_text("utf-8", errors="replace").splitlines(keepends=True)
            HOSTS_FILE.write_text("".join(l for l in lines if HOSTS_MARK not in l), "utf-8")
            run(["ipconfig", "/flushdns"], check=False)


class FakeGitHub:
    """GitHub's release API and download hosts for two stable releases, on 127.0.0.1:443."""

    def __init__(self, work, installers):
        self.work = work
        self.requests = []  # (host, method, path, accept)
        self.assets = {}  # id -> (name, bytes, download host)
        self.releases = {}
        for n, version in enumerate((OLD, NEW)):
            setup = installers / f"RainCLI-Setup-{version}.exe"
            checksum = installers / f"RainCLI-Setup-{version}.exe.sha256"
            data, line = setup.read_bytes(), checksum.read_bytes()
            if line != f"{hashlib.sha256(data).hexdigest()}  {setup.name}\n".encode():
                raise Failure(f"{checksum.name} is not '<sha256>  {setup.name}'")
            ids = (1000 + 2 * n, 1001 + 2 * n)
            self.assets[ids[0]] = (setup.name, data, "objects.githubusercontent.com")
            self.assets[ids[1]] = (checksum.name, line, "release-assets.githubusercontent.com")
            tag = "v" + version
            self.releases[tag] = {
                "id": 50 + n, "tag_name": tag, "name": f"RainCLI {version}", "draft": False, "prerelease": False,
                "assets": [{"id": i, "name": self.assets[i][0], "size": len(self.assets[i][1]),
                            "url": f"https://api.github.com/repos/{updates.REPO}/releases/assets/{i}",
                            "browser_download_url":
                                f"https://github.com/{updates.REPO}/releases/download/{tag}/{self.assets[i][0]}"}
                           for i in ids]}
        self.commits = {tag: hashlib.sha1(tag.encode()).hexdigest() for tag in self.releases}
        self.server = None

    def start(self, cert, key, port=443):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, body=b"", content_type="application/json", headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                accept = self.headers.get("Accept", "")
                fake.requests.append((host, "GET", self.path, accept))
                repo = f"/repos/{updates.REPO}"
                if host == "api.github.com" and self.path.startswith(repo + "/releases/tags/"):
                    release = fake.releases.get(self.path.rsplit("/", 1)[1])
                    return self.send(200, json.dumps(release).encode()) if release else self.send(404, b"{}")
                if host == "api.github.com" and self.path == repo + "/releases/latest":
                    return self.send(200, json.dumps(fake.releases["v" + NEW]).encode())
                if host == "api.github.com" and self.path.startswith(repo + "/commits/"):
                    sha = fake.commits.get(self.path.rsplit("/", 1)[1])
                    return self.send(200, json.dumps({"sha": sha}).encode()) if sha else self.send(404, b"{}")
                if host == "api.github.com" and self.path.startswith(repo + "/releases/assets/"):
                    asset_id = int(self.path.rsplit("/", 1)[1]) if self.path.rsplit("/", 1)[1].isdigit() else -1
                    if asset_id not in fake.assets:
                        return self.send(404, b"{}")
                    name, _data, cdn = fake.assets[asset_id]
                    if accept != "application/octet-stream":
                        return self.send(200, json.dumps({"id": asset_id, "name": name}).encode())
                    return self.send(302, headers=[("Location", f"https://{cdn}/assets/{asset_id}/{name}?e2e=1")])
                if host == "github.com" and "/releases/download/" in self.path:
                    name = self.path.rsplit("/", 1)[1]
                    match = next((i for i, a in fake.assets.items() if a[0] == name), None)
                    if match is None:
                        return self.send(404, b"Not Found", "text/plain")
                    name, _data, cdn = fake.assets[match]
                    return self.send(302, headers=[("Location", f"https://{cdn}/assets/{match}/{name}?e2e=1")])
                if host in ("objects.githubusercontent.com", "release-assets.githubusercontent.com"):
                    parts = self.path.split("?")[0].split("/")
                    if len(parts) == 4 and parts[1] == "assets" and parts[2].isdigit():
                        asset = fake.assets.get(int(parts[2]))
                        if asset and asset[2] == host and asset[0] == parts[3]:
                            return self.send(200, asset[1], "application/octet-stream")
                return self.send(404, b'{"message": "Not Found"}')

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def fetched(self, version):
        """Hosts that served each asset of ``version``: (installer host, checksum host)."""
        names = (f"RainCLI-Setup-{version}.exe", f"RainCLI-Setup-{version}.exe.sha256")
        return tuple({h for h, _m, p, _a in self.requests if p.split("?")[0].endswith("/" + n)} for n in names)


# -- the installed app -----------------------------------------------------------------------

class App:
    def __init__(self, work):
        self.work = work
        self.root = app_root()

    def install(self, version, installers, label):
        log = self.work / f"install-{label}.log"
        run([installers / f"RainCLI-Setup-{version}.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
             f"/LOG={log}"], timeout=600)
        return log

    def install_json(self):
        try:
            return json.loads((self.root / "install.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def run_value(self):
        return reg_get(RUN_KEY, "RainCLI")

    def stub(self, *args, check=False):
        return run([self.root / "RainCLI.exe", *args], check=check, timeout=180)

    def quit(self):
        if (self.root / "RainCLI.exe").is_file():
            self.stub("--quit")

    def cli(self, *args, check=True, timeout=300):
        return run([self.root / "bin" / "raincli.exe", *args], check=check, timeout=timeout)

    def launch_run_value(self):
        """Exactly what Windows runs at logon: the Run value's command line, unparsed."""
        command = self.run_value()
        if not command:
            raise Failure("no RainCLI Run value to launch")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(command, close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return command

    def runs(self, version):
        exes = [Path(p) for p in running_app_exes()]
        app = self.root / "versions" / version / "RainCLI-app.exe"
        return any(str(p).casefold() == str(app).casefold() for p in exes)

    def uninstall(self):
        uninstaller = self.root / "unins000.exe"
        if not uninstaller.is_file():
            raise Failure("no uninstaller at the install root")
        run([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={self.work / 'uninstall.log'}"],
            timeout=600)
        # The uninstaller re-runs itself from TEMP; wait until it has removed its own key and file.
        wait_for("the uninstaller to finish",
                 lambda: reg_values(UNINSTALL_KEY) is None and not uninstaller.exists(), timeout=300)


def login_through_conpty(app, server, password, machine):
    """`raincli.exe login` with the password typed at its no-echo prompt in a pseudo console."""
    from winpty import PtyProcess

    exe = str(app.root / "bin" / "raincli.exe")
    proc = PtyProcess.spawn([exe, "login", "--email", EMAIL, "--machine-name", machine, "--api-url", server.url],
                            dimensions=(30, 160))
    output, done = [], threading.Event()

    def reader():
        while True:
            try:
                chunk = proc.read(4096)
            except EOFError:
                break
            except OSError:
                break
            if chunk:
                output.append(chunk)
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    wait_for("the password prompt", lambda: "assword" in "".join(output), timeout=120, interval=0.5)
    proc.write(password + "\r\n")
    done.wait(180)
    text = SECRETS.scrub("".join(output))
    if password in "".join(output):
        raise Failure("the password was echoed by the login prompt")
    with contextlib.suppress(Exception):
        proc.wait()
    status = proc.exitstatus
    if status not in (0, None) or "error" in text.lower():
        raise Failure(f"raincli login exited {status}: {text[-2000:]}")
    return text


# -- server observations ----------------------------------------------------------------------

def api(server, token, path, body=None, method=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(server.url + path, data=data, method=method or ("POST" if body else "GET"),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def entry(server, token, handle):
    try:
        return next((a for a in api(server, token, "/api/v1/agents")["agents"] if a["handle"] == handle), None)
    except (OSError, ValueError):
        return None


def shows(server, token, handle, version, state="current", error=None, mode="automatic"):
    e = entry(server, token, handle)
    machine = (e or {}).get("machine") or {}
    ok = (e is not None and machine.get("client_version") == version and machine.get("update_state") == state
          and machine.get("error") == error and machine.get("update_mode") == mode
          and e["presence"]["status"] != "offline")
    return e if ok else None


def handles(server, admin_token):
    return sorted(a["handle"] for a in api(server, admin_token, "/api/v1/agents")["agents"])


def delivered(server, token, message_id):
    try:
        state = api(server, token, f"/api/v1/messages/{message_id}")["message"]["delivery_state"]
    except (OSError, ValueError, KeyError):
        return None
    return state if state != "stored" else None


def send(server, token, to, body):
    message_id = str(uuid.uuid4())
    api(server, token, "/api/v1/messages", {"id": message_id, "to": to, "body": body})
    return message_id


# -- the run --------------------------------------------------------------------------------

def check(condition, message):
    if not condition:
        raise Failure(message)


def write_private(path, data):
    from raincli_agent.fsutil import atomic_write_json

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data, mode=0o600)


def prepare_old_pip_client(work):
    """0.2.0 from its release archive, in its own venv (done before the hosts file changes)."""
    venv = work / "old-pip"
    run([sys.executable, "-m", "venv", venv], timeout=180)
    python = venv / "Scripts" / "python.exe"
    archive = f"https://github.com/{updates.REPO}/archive/refs/tags/{OLD_PIP}.zip"
    run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", f"raincli @ {archive}#subdirectory=raincli"],
        timeout=900)
    version = run([venv / "Scripts" / "raincli.exe", "--version"]).stdout.strip()
    check(version == "raincli " + OLD_PIP[1:], f"the old pip client reports {version!r}")
    return venv


def download_release_installers(work):
    """The published RainCLI-Setup assets of OLD and NEW, by exact name, checksum-verified."""
    target = work / "installers"
    target.mkdir()
    for version in (OLD, NEW):
        release = json.loads(updates.fetch(f"{updates.API}/releases/tags/v{version}", 1024 * 1024))
        check(not release.get("draft") and not release.get("prerelease"), f"v{version} is not a stable release")
        files = {}
        for name in (f"RainCLI-Setup-{version}.exe", f"RainCLI-Setup-{version}.exe.sha256"):
            asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
            check(asset is not None, f"release v{version} has no asset {name}; attach it first (docs/release-testing.md)")
            request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "RainCLI-app-e2e"})
            with urllib.request.urlopen(request, timeout=300) as response:
                files[name] = response.read()
            (target / name).write_bytes(files[name])
        setup = f"RainCLI-Setup-{version}.exe"
        check(files[setup + ".sha256"] == f"{hashlib.sha256(files[setup]).hexdigest()}  {setup}\n".encode(),
              f"the published checksum of {setup} does not match it")
    return target


def run_e2e(args, work, stack):
    installers = download_release_installers(work) if args.real else Path(args.installers).resolve()
    server = release_e2e.Server(work / "server", args.server_python)
    server.work.mkdir()
    server.prepare()
    old_venv = prepare_old_pip_client(work)
    say(f"PASS: old pip client {OLD_PIP} installed from its release archive")
    pg = Path(tempfile.gettempdir()) / f"raincli-app-pg-{secrets.token_hex(6)}"
    pg.mkdir()
    stack.callback(shutil.rmtree, pg, True)
    database = release_e2e.Cluster(pg)
    url = SECRETS.add(database.start())
    stack.callback(database.stop)
    server.configure(url)
    server.start()
    stack.callback(server.stop)
    password = SECRETS.add(secrets.token_urlsafe(18))
    server.admin("create-user", "--email", EMAIL, "--name", "App E2E", input=password + "\n")
    server.admin("create-team", "--slug", TEAM, "--name", "App E2E", "--owner", EMAIL)
    staged = work / "observer.json"
    server.admin("register-agent", "--team", TEAM, "--owner", EMAIL, "--handle", OBSERVER, "--out", str(staged))
    observer = SECRETS.add(json.loads(staged.read_text())["token"])
    staged.unlink()
    say(f"PASS: throwaway server on {server.url}; user, team {TEAM} and observer {OBSERVER}")

    fake = None
    if args.real:
        say(f"PASS: published installers of v{OLD} and v{NEW} downloaded and checksum-verified")
    else:
        ca = TestCA(work)
        ca.create()
        stack.callback(ca.remove)
        fake = FakeGitHub(work, installers)
        fake.start(str(work / "leaf.pem"), str(work / "leaf.key"))
        stack.callback(fake.stop)
        hosts = HostsEntry()
        hosts.add()
        stack.callback(hosts.remove)
        with urllib.request.urlopen(f"https://api.github.com/repos/{updates.REPO}/releases/tags/v{NEW}",
                                    timeout=20) as response:  # the system trust store accepts the test CA
            check(json.loads(response.read())["tag_name"] == "v" + NEW, "the fake release endpoint is not reachable")
        fake.requests.clear()
        say("PASS: fake release endpoint on the real hostnames (hosts file, test root CA, port 443)")

    app = App(work)
    stack.callback(lambda: release_e2e.kill_marked({str(app.root)}))
    stack.callback(app.quit)
    check(not app.root.exists() and app.run_value() is None, "the runner already has a RainCLI app install")

    # 1. silent per-user install
    app.install(OLD, installers, "first")
    layout = [app.root / "RainCLI.exe", app.root / "bin" / "raincli.exe", app.root / "unins000.exe",
              app.root / "versions" / OLD / "RainCLI-app.exe", app.root / "versions" / OLD / "raincli.exe"]
    check(all(p.is_file() for p in layout), f"missing after install: {[str(p) for p in layout if not p.is_file()]}")
    check(app.install_json() == {"current": OLD, "previous": None, "probation": None},
          f"install.json is {app.install_json()}")
    stub_command = f'"{app.root}\\RainCLI.exe" --background'
    check(app.run_value() == stub_command, f"Run value is {app.run_value()!r}")
    check(reg_values(UNINSTALL_KEY) is not None, "no per-user (HKCU) uninstall key")
    import winreg
    check(reg_values(UNINSTALL_KEY, winreg.HKEY_LOCAL_MACHINE) is None, "a machine-wide uninstall key was written")
    check(user_path_entries()[:1] == [str(app.root / "bin")], f"user PATH starts {user_path_entries()[:1]}")
    menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "RainCLI"
    check((menu / "RainCLI.lnk").is_file() and (menu / "Uninstall RainCLI.lnk").is_file(), "Start menu entries missing")
    record = (app.root / "installer-record.log").read_text("utf-8")
    check("run value" in record and "none" in record, "installer-record.log did not record the Run value first")
    check(app.cli("--version").stdout.strip() == f"raincli {OLD}", "the PATH shim does not run the current CLI")
    say(f"PASS: 1. silent per-user install of {OLD}: layout, install.json, HKCU Run value and uninstall key, "
        "shim first on PATH, Start menu, H7 record")

    # 2. first-run sign-in through the login functions (the installed CLI), password on a pseudo console
    app.quit()  # the installer started the tray; sign in from the CLI instead of its dialog
    login_through_conpty(app, server, password, MACHINE)
    agent_config = json.loads(default_agent_config().read_text("utf-8"))
    check("token_dpapi" in agent_config and "token" not in agent_config, "agent.json is not in the DPAPI form")
    runtime = json.loads((default_agent_config().parent / "runtime.json").read_text("utf-8"))
    check(runtime.get("machine_config") and not runtime.get("connectors"), "runtime.json is not machine mode")
    check(MACHINE in handles(server, observer), "the server has no signed-in machine")
    say(f"PASS: 2. raincli login through a pseudo console: machine {MACHINE} created, DPAPI credential, machine mode")

    # 3. what the Run value names, then presence
    launched = app.launch_run_value()
    check(launched == stub_command, "the launched command is not the Run value")
    wait_for(f"presence {OLD} automatic current", lambda: shows(server, observer, MACHINE, OLD), timeout=240)
    check(app.runs(OLD), f"RainCLI-app.exe from versions\\{OLD} is not running")
    say(f"PASS: 3. launched the Run value; presence reports {OLD}, automatic, current")

    # 4. pushed upgrade through the installer assets
    run_before, key_before = app.run_value(), reg_values(UNINSTALL_KEY)
    server.admin("set-client-version", "--team", TEAM, "v" + NEW)
    wait_for(f"the pushed update to {NEW}", lambda: shows(server, observer, MACHINE, NEW))
    wait_for(f"RainCLI-app.exe from versions\\{NEW}", lambda: app.runs(NEW), timeout=240)
    state = app.install_json()
    check(state.get("current") == NEW and state.get("previous") == OLD, f"install.json after update: {state}")
    check(app.run_value() == run_before and reg_values(UNINSTALL_KEY) == key_before,
          "/UPDATE changed the Run value or the uninstall key")
    check(not list((app.root / "versions" / NEW).glob("unins*")), "/UPDATE left an uninstaller")
    if fake is not None:
        installer_hosts, checksum_hosts = fake.fetched(NEW)
        check(installer_hosts == {"objects.githubusercontent.com"}, f"installer served by {installer_hosts}")
        check(checksum_hosts == {"release-assets.githubusercontent.com"}, f"checksum served by {checksum_hosts}")
        asset_calls = [r for r in fake.requests if "/releases/assets/" in r[2]]
        check(asset_calls and all(r[0] == "api.github.com" and r[3] == "application/octet-stream"
                                  for r in asset_calls),
              "assets were not fetched through the API asset URL with Accept: application/octet-stream")
        check(all(r[0] in HOSTS for r in fake.requests), "a request named a host outside the allowlist")
    say(f"PASS: 4. pushed {NEW} through the {'published' if args.real else 'fake'} release assets"
        f"{'' if args.real else ' (API asset URL, exact download hosts)'}: /UPDATE left the Run value and "
        "uninstall key alone, install.json swapped, relaunched from the new version")

    # 5. downgrade refused, then allowed
    server.admin("set-client-version", "--team", TEAM, "v" + OLD)
    wait_for("the refused downgrade", lambda: shows(server, observer, MACHINE, NEW, "failed", "downgrade_not_allowed"),
             timeout=240)
    time.sleep(40)
    check(app.install_json().get("current") == NEW, "the version changed without --allow-downgrade")
    server.admin("set-client-version", "--team", TEAM, "v" + OLD, "--allow-downgrade")
    wait_for(f"the allowed downgrade to {OLD}", lambda: shows(server, observer, MACHINE, OLD))
    wait_for(f"RainCLI-app.exe from versions\\{OLD}", lambda: app.runs(OLD), timeout=240)
    server.admin("set-client-version", "--team", TEAM, "--clear")
    say(f"PASS: 5. downgrade to {OLD} refused, then allowed with --allow-downgrade; target cleared")

    # 6. sign out through the PATH shim, then uninstall
    app.cli("logout", "--yes")
    check(not default_agent_config().exists(), "logout left the credential")
    check((entry(server, observer, MACHINE) or {}).get("active") is False, "sign-out did not revoke the machine")
    app.uninstall()
    gone = [app.root / "RainCLI.exe", app.root / "bin" / "raincli.exe", app.root / "versions", app.root / "install.json",
            menu]
    check(not [p for p in gone if p.exists()], f"uninstall left {[str(p) for p in gone if p.exists()]}")
    check(app.run_value() is None and str(app.root / "bin") not in user_path_entries(),
          "uninstall left the Run value or the PATH entry")
    check((app.root / "installer-record.log").is_file(), "uninstall removed the installer record")
    say("PASS: 6. signed out through the shim; uninstall removed the Run value, PATH entry, shortcuts, versions, "
        "stub, shim and install.json, and kept the record")

    # 7. migration from an old pip client with a connector config and a queue
    staged = work / "old-agent.json"
    server.admin("register-agent", "--team", TEAM, "--owner", EMAIL, "--handle", OLD_MACHINE, "--out", str(staged))
    issued = json.loads(staged.read_text())
    old_token = SECRETS.add(issued["token"])
    staged.unlink()
    config_dir = default_agent_config().parent
    write_private(default_agent_config(), {"api_url": issued["api_url"], "token": old_token})
    connector = config_dir / "connector.json"
    queue = work / "old-queue"
    write_private(connector, {"herdr_agent": "e2e-inbox", "herdr_bin": "raincli-e2e-no-herdr",
                              "state_dir": str(queue), "poll_wait": 1})  # no agent_config: the default path
    write_private(config_dir / "runtime.json", {"connectors": [str(connector)], "state_dir": str(work / "old-state")})
    old_raincli = old_venv / "Scripts" / "raincli.exe"
    with open(work / "old-runtime.log", "ab") as out:
        old_runtime = subprocess.Popen([old_raincli, "runtime", "run", "--config", config_dir / "runtime.json"],
                                       stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    first = send(server, observer, OLD_MACHINE, "before migration")
    wait_for("the old pip connector to take a message", lambda: delivered(server, observer, first), timeout=240)
    run([old_raincli, "runtime", "stop", "--config", config_dir / "runtime.json"], check=False)
    with contextlib.suppress(subprocess.TimeoutExpired):
        old_runtime.wait(timeout=150)
    if old_runtime.poll() is None:
        old_runtime.kill()
    before = handles(server, observer)
    queue_files = sorted(p.name for p in queue.rglob("*") if p.is_file())
    app.install(NEW, installers, "migration")  # its last step starts the tray, whose first run migrates
    wait_for("the migrated machine's presence", lambda: shows(server, observer, OLD_MACHINE, NEW), timeout=300)
    migrated = json.loads(default_agent_config().read_text("utf-8"))
    check("token_dpapi" in migrated and "token" not in migrated, "migration did not convert the token to DPAPI")
    check(handles(server, observer) == before, "migration created or removed a machine")
    check(api(server, old_token, "/api/v1/me")["agent"]["handle"] == OLD_MACHINE, "the old credential stopped working")
    runtime = json.loads((config_dir / "runtime.json").read_text("utf-8"))
    check(runtime.get("connectors") and not runtime.get("machine_config"), "runtime.json is not in connector mode")
    second = send(server, observer, OLD_MACHINE, "after migration")
    wait_for("delivery after migration", lambda: delivered(server, observer, second), timeout=240)
    check(set(queue_files) <= {p.name for p in queue.rglob("*") if p.is_file()}, "migration dropped queue files")
    logs = list(app.root.rglob("migration.log")) + list(config_dir.rglob("migration.log")) + \
        list((work / "old-state").rglob("migration.log"))
    check(logs and old_token not in logs[0].read_text("utf-8", errors="replace"), "no migration.log, or it holds the token")
    say(f"PASS: 7. migrated pip {OLD_PIP} with a connector config (no agent_config) and queue: same handle "
        f"{OLD_MACHINE} and credential, DPAPI, connector mode, delivery continues, no new machine")

    # 8. uninstall without sign-out keeps the credential and the connector state
    app.uninstall()
    check(default_agent_config().is_file() and connector.is_file() and queue.is_dir() and logs[0].is_file(),
          "uninstall without sign-out removed agent.json, the connector config, the queue or migration.log")
    check(api(server, old_token, "/api/v1/me")["agent"]["handle"] == OLD_MACHINE, "uninstall signed the machine out")
    say("PASS: 8. uninstall without sign-out kept agent.json, the connector config, the queue and migration.log")


def diagnose(work):
    say("===== DIAGNOSTICS (secrets redacted) =====")
    root = app_root()
    for path in [root / "install.json", root / "installer-record.log", *sorted(root.rglob("*.log")),
                 *sorted(work.glob("install-*.log")), work / "uninstall.log", work / "old-runtime.log",
                 work / "server" / "server.log", default_agent_config().parent / "runtime.json"]:
        if path.is_file():
            say(f"--- {path} ---\n{tail(path, 6000)}")
    with contextlib.suppress(Exception):
        say("running: " + ", ".join(running_app_exes()))
    say("===== END DIAGNOSTICS =====")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--installers",
                        help=f"directory holding locally built RainCLI-Setup-{OLD}.exe and -{NEW}.exe with .sha256 files")
    source.add_argument("--real", nargs=2, metavar=("FROM", "TO"),
                        help="published releases vX.Y.Z (v0.4.0 or later) whose installer assets to use")
    parser.add_argument("--server-python", help="an existing Python with raincli/requirements.lock installed")
    args = parser.parse_args(argv)
    global OLD, NEW
    if args.real:
        tags = args.real
        if (not all(updates.TAG_RE.fullmatch(t) for t in tags) or updates.version_key(tags[0]) < (0, 4, 0)
                or updates.version_key(tags[0]) >= updates.version_key(tags[1])):
            parser.error("--real takes two release tags vX.Y.Z, v0.4.0 or later, oldest first")
        OLD, NEW = tags[0][1:], tags[1][1:]
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("this e2e changes the hosts file, the trusted roots, the Run value and the user PATH; "
                     "it runs only on a disposable GitHub Actions Windows runner")
    work = Path(tempfile.mkdtemp(prefix="raincli-app-e2e-"))
    say(f"Windows app e2e: {OLD} -> {NEW}; work dir {work}")
    ok = False
    try:
        with contextlib.ExitStack() as stack:
            try:
                run_e2e(args, work, stack)
                ok = True
            except BaseException as exc:
                say(f"FAIL: {SECRETS.scrub(str(exc)) or type(exc).__name__}")
                diagnose(work)
                if not isinstance(exc, (Failure, KeyboardInterrupt)):
                    raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    say("WINDOWS APP E2E PASSED" if ok else "WINDOWS APP E2E FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
