"""Windows app end to end, against a throwaway in-job server and a local fake release endpoint.

Runs ONLY on a disposable GitHub-hosted Actions Windows runner (it edits the runner's hosts
file, LocalMachine Root store, HKCU Run value and user PATH, and installs into its profile). It
takes two installers built from the same source as 0.5.0 and 0.5.1 by
packaging/windows/build.py, builds a third, 0.5.2, from a staging copy whose tray exits 1, and
checks (protocol §15, §15.8, §15.9). The test versions are 0.5.x: a runtime whose connector has polled
with routing=1 refuses targets below v0.5.0 (§16.12 C1):

A. A fresh app install, signed in from the CLI:
   1. a silent per-user install with no admin: the layout, install.json, the HKCU Run value and
      uninstall key, the shim first on the user PATH, the Start menu entries, the H7 record;
   2. `RainCLI.exe --quit` stops the installer-started app (exit 0, nothing left running), then
      sign-in through the installed CLI's `raincli login`, with the password typed into a ConPTY
      prompt, never argv or the environment; DPAPI credential, machine-mode runtime config;
   3. launching exactly what the Run value names; presence reports 0.5.0, automatic, current;
   4. a pushed upgrade to 0.5.1 through the installer-asset path (API asset URL, exact download
      hosts), /UPDATE changing neither the Run value nor the uninstall key, install.json swapped,
      the tray relaunched from versions\0.5.1;
   5. a pushed 0.5.2 whose tray exits 1: the stub's probation rolls back, `rolled_back` is
      reported and 0.5.1 runs again;
   6. the downgrade to 0.5.0 refused, then allowed with --allow-downgrade;
   7. an uninstall that signs out (/SIGNOUT=yes): the machine is revoked and M10's removals hold.
B. Migration of an old pip-installed client (0.2.0 from its release archive) running as a
   foreground `raincli connector run` with no runtime.json, no agent_config and a relative
   state_dir: the install waits while the old connector holds its queue, then, once it is closed,
   keeps the handle and credential (no new machine), writes a connector-mode runtime.json, and
   delivery continues. An uninstall without sign-out keeps the credential and connector state.
C. Migration of a managed v0.3.2 install in the documented layout: the credential, connector and
   runtime config in ~/.config/raincli, logon start through `runtime startup --config` on that
   default runtime.json (the app's own default too: review 2 R1), and the old runtime started by
   running exactly its Run value. The installer records that value, the app's stop request stops
   the launcher, the Run value then starts the stub, and delivery continues on the same handle.

D. The app window (protocol §16.10, §16.14, §16.15; design §10 GUI coverage), on a fresh install of
   this ref's build, signed in from the CLI: install.json has "stub": 2 and an install_stamp and the Start
   menu shortcut has no arguments; without WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS no WebView2 process
   listens on a port, carries a debugging switch or writes DevToolsActivePort; an install without "stub"
   has its shortcut pointed at --background. Then `RainCLI.exe --open` with that variable set for this
   launch only: Playwright connects over CDP and drives the window's local sign-in (a person session),
   the handoff to the inbox, reading, replying and sending, This computer and Settings, the offline
   page and Retry (the server stopped and started), and sign-out (the machine revoked, the profile
   marked for reset). A full install afterwards restores the no-argument shortcut. Screenshots go to
   build/e2e-artifacts. Skipped with --real (the published v0.4 installers have no window).

E. A stale or foreign saved setup (protocol §16.17, §16.18), each case on a fresh install of this ref:
   1. a pip-style config whose credential is revoked and owned by ANOTHER account, plus a connector config
      naming it: migration sets it aside into replaced-<stamp>, the window shows sign-in, and signing in as
      the team owner gives a new machine and a person session; the backup holds the old files;
   2. a live credential owned by another user: it is adopted, then the window's sign-in explains it and
      offers "Set up this computer as a new machine", which (with the password typed again) works, and
      the other user's machine stays active;
   3. a connector config outside the scan directories, named by a connector-mode runtime.json: both move
      (§16.18 V3);
   4. a connector runtime with two credentials, one revoked: only the revoked one is set aside, the
      runtime config is rewritten, and the valid one keeps delivering (§16.18 V2);
   5. an app machine (signed in from the CLI as another account) with an agent_held record and a by-name
      handover box, then revoked: the window's refused handoff shows sign-in, the owner's sign-in offers a
      new machine, and after it neither the record nor the box is in the new state, only in the backup
      (§16.18 V1);
   6. This computer with the pinned real Codex on the app's PATH: Not connected, Connect only on the click
      (RainCLI's hooks written, the /hooks approval sentence), then Disconnect (§16.19 item 3).
   Skipped with --real.

Release traffic goes to the REAL hostnames (api.github.com, github.com,
objects.githubusercontent.com, release-assets.githubusercontent.com): a hosts-file entry points
them at 127.0.0.1:443, where a fake release endpoint serves a certificate from a test root CA
added to the runner's LocalMachine Root store (§15.8 M11). The shipped client has no override, and
the 0.5.2 rollback build is patched only in this job's staging copy.
Every credential is generated here and never printed. See docs/release-testing.md.

With ``--real FROM TO`` (for example ``--real v0.4.0 v0.4.1``) it skips the fake endpoint, the
hosts file, the test CA and the rollback build: it downloads both versions' installer assets from
the published releases of the canonical repository, and the pushed update fetches the real assets.
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
_codex_spec = importlib.util.spec_from_file_location("codex_smoke", ROOT / "scripts" / "codex_smoke.py")
codex_smoke = importlib.util.module_from_spec(_codex_spec)
_codex_spec.loader.exec_module(codex_smoke)  # the pinned real Codex (fetch_codex), as in the client smoke
from raincli_agent.runtime import updates  # noqa: E402  (REPO, the canonical repository: a constant)

Failure, SECRETS, say, wait_for, tail = (release_e2e.Failure, release_e2e.SECRETS, release_e2e.say,
                                         release_e2e.wait_for, release_e2e.tail)

TEAM, EMAIL = "app-e2e", "app-e2e@example.invalid"
MACHINE, OBSERVER, OLD_MACHINE, MANAGED_MACHINE = "e2e-app-machine", "e2e-observer", "e2e-pip-machine", \
    "e2e-managed-machine"
OLD, NEW, BROKEN = "0.5.0", "0.5.1", "0.5.2"  # §16.12 C1: never below v0.5.0
OLD_PIP, OLD_MANAGED = "v0.2.0", "v0.3.2"
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

    def __init__(self, work, installers, versions):
        self.work = work
        self.requests = []  # (host, method, path, accept)
        self.assets = {}  # id -> (name, bytes, download host)
        self.releases = {}
        self.commits = {}
        self.server = None
        for version in versions:
            self.add_release(installers, version)

    def add_release(self, installers, version):
        if installers is None:
            return
        setup = Path(installers) / f"RainCLI-Setup-{version}.exe"
        checksum = Path(installers) / f"RainCLI-Setup-{version}.exe.sha256"
        data, line = setup.read_bytes(), checksum.read_bytes()
        if line != f"{hashlib.sha256(data).hexdigest()}  {setup.name}\n".encode():
            raise Failure(f"{checksum.name} is not '<sha256>  {setup.name}'")
        n = len(self.releases)
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
        self.commits[tag] = hashlib.sha1(tag.encode()).hexdigest()

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
                    latest = max(fake.releases, key=updates.version_key)
                    return self.send(200, json.dumps(fake.releases[latest]).encode())
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

    def quit(self, required=True):
        """`RainCLI.exe --quit` (§15.9): exit 0 and nothing of the app left running."""
        if not (self.root / "RainCLI.exe").is_file():
            return
        result = self.stub("--quit")
        if not required:
            return
        check(result.returncode == 0, f"RainCLI.exe --quit exited {result.returncode}")
        left = [p for p in running_app_exes() if str(p).casefold().startswith(str(self.root).casefold())]
        check(not left, f"still running after --quit: {left}")

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

    def uninstall(self, signout, label):
        uninstaller = self.root / "unins000.exe"
        if not uninstaller.is_file():
            raise Failure("no uninstaller at the install root")
        run([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/SIGNOUT={'yes' if signout else 'no'}",
             f"/LOG={self.work / f'uninstall-{label}.log'}"], timeout=600)
        # The uninstaller re-runs itself from TEMP; wait until it has removed its own key and file.
        log = self.work / f"uninstall-{label}.log"

        def finished():
            if log.is_file() and "InitializeUninstall returned False" in log.read_text("utf-8", errors="replace"):
                raise Failure(f"the uninstaller refused ({label}): RainCLI.exe --quit did not stop the app; "
                              "see quit-blockers.txt in the diagnostics")
            return reg_values(UNINSTALL_KEY) is None and not uninstaller.exists()
        wait_for("the uninstaller to finish", finished, timeout=300)


def login_through_conpty(app, server, password, machine, email=None):
    """`raincli.exe login` with the password typed at its no-echo prompt in a pseudo console."""
    from winpty import PtyProcess

    exe = str(app.root / "bin" / "raincli.exe")
    proc = PtyProcess.spawn([exe, "login", "--email", email or EMAIL, "--machine-name", machine, "--api-url", server.url],
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


# -- the app window (part D) ------------------------------------------------------------------

ARTIFACTS = ROOT / "build" / "e2e-artifacts"
WINDOW_MACHINE = "e2e-window-machine"


def powershell(script, env=None):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                          text=True, timeout=120, env=env).stdout


def webview_processes(root):
    """``[(pid, command line)]`` of the WebView2 processes using the app's private profile."""
    out = powershell("Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
                     "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    profile_dir = str(root / "state" / "webview").casefold()
    found = []
    for line in out.splitlines():
        pid, _, command = line.partition("\t")
        if pid.strip().isdigit() and profile_dir in command.casefold():
            found.append((int(pid), command))
    return found


def app_pids(root):
    out = powershell("Get-CimInstance Win32_Process -Filter \"Name='RainCLI-app.exe'\" | "
                     "ForEach-Object { \"$($_.ProcessId)`t$($_.ExecutablePath)\" }")
    return {int(pid) for pid, _, path in (l.partition("\t") for l in out.splitlines())
            if pid.strip().isdigit() and path.casefold().startswith(str(root).casefold())}


def listening(pids):
    """``{(address, pid)}`` of TCP sockets in LISTEN owned by ``pids`` (IPv4 and IPv6)."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=60).stdout
    out += subprocess.run(["netstat", "-ano", "-p", "TCPv6"], capture_output=True, text=True, timeout=60).stdout
    found = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[3] == "LISTENING" and parts[4].isdigit() and int(parts[4]) in pids:
            found.add((parts[1], int(parts[4])))
    return found


def devtools_answer(address):
    """True when ``host:port`` answers like a Chromium DevTools endpoint."""
    try:
        with urllib.request.urlopen(f"http://{address}/json/version", timeout=5) as response:
            return "webSocketDebuggerUrl" in response.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return False


def shortcut_arguments(link):
    return powershell("(New-Object -ComObject WScript.Shell).CreateShortcut($env:RAINCLI_LNK).Arguments",
                      env=dict(os.environ, RAINCLI_LNK=str(link))).strip()


def click_dialog_ok(title, timeout=60):
    """Press OK in the app's native confirmation dialog (a Windows message box titled ``title``)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        text, cls = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, text, 256)
        user32.GetClassNameW(hwnd, cls, 64)
        if text.value == title and cls.value == "#32770" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    def dialog():
        found.clear()
        user32.EnumWindows(each, 0)
        return found[0] if found else None
    hwnd = wait_for(f"the {title!r} dialog", dialog, timeout=timeout, interval=0.5)
    ok = user32.GetDlgItem(hwnd, 1)  # IDOK
    check(ok, f"the {title!r} dialog has no OK button")
    user32.SendMessageW(ok, 0x00F5, 0, 0)  # BM_CLICK


class WindowCDP:
    """Playwright attached to the app window over CDP. Loading a local page after a hosted one swaps the
    window's main frame (another origin), which an existing Playwright connection may not follow: the
    page is found through CDP's own target list, and the connection is made again when needed."""

    def __init__(self, pw, port):
        self.pw, self.port = pw, port
        self.browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

    def targets(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=5) as response:
                return [(t.get("type"), t.get("url") or "") for t in json.loads(response.read())]
        except (OSError, ValueError):
            return []

    def _find(self, fragment):
        for context in self.browser.contexts:
            for candidate in context.pages:
                if not candidate.is_closed() and fragment in url_of(candidate):
                    return candidate
        return None

    def page(self, fragment, timeout=120):
        def found():
            if not any(kind == "page" and fragment in url for kind, url in self.targets()):
                return None
            page = self._find(fragment)
            if page is None:  # the target is there, this connection hasn't followed it: attach again
                with contextlib.suppress(Exception):
                    self.browser.close()  # drops the CDP connection only; the app keeps running
                self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}")
                page = self._find(fragment)
            return page
        try:
            return wait_for(f"the app window showing {fragment}", found, timeout=timeout, interval=1)
        except Failure:
            seen = [c.url for context in self.browser.contexts for c in context.pages]
            raise Failure(f"no window page showing {fragment}; Playwright pages {seen}; "
                          f"CDP targets {self.targets()}") from None

    def close(self):
        with contextlib.suppress(Exception):
            self.browser.close()


def url_of(page):
    """The window's current URL, polled from here: wait_for_url rejects when the app cancels a navigation
    (the /app/local sentinel), and an aborted load is expected in D7."""
    try:
        return page.url
    except Exception:  # noqa: BLE001 - a frame in the middle of a navigation
        return ""


def shot(page, name):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(ARTIFACTS / f"{name}.png"))


def inbox_bodies(server, token):
    data = api(server, token, "/api/v1/inbox?routing=1&wait=0&limit=100")
    return [m.get("body", "") for m in data.get("messages", [])]


def part_d(app, server, installers, password, observer, menu, work):
    """The app window over CDP; see D in the module docstring."""
    from playwright.sync_api import sync_playwright

    # D1. A full install of this ref: stub 2, an install_stamp, the no-argument shortcut.
    app.install(NEW, installers, "window")
    state = app.install_json()
    check(state.get("current") == NEW and state.get("stub") == 2 and state.get("install_stamp"),
          f"install.json after a full install: {state}")
    link = menu / "RainCLI.lnk"
    check(shortcut_arguments(link) == "", "the full install's Start menu shortcut has arguments")
    app.quit()
    login_through_conpty(app, server, password, WINDOW_MACHINE)
    say("PASS: D1. full install: install.json has stub 2 and an install_stamp, the Start menu shortcut has no "
        f"arguments; {WINDOW_MACHINE} signed in from the CLI")

    # D2. No debugging without the job's variable: no port, no switch, no DevToolsActivePort.
    check("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in os.environ, "the job set the WebView2 variable globally")
    app.launch_run_value()
    wait_for("the app window's WebView2 processes", lambda: webview_processes(app.root), timeout=180)
    time.sleep(10)
    processes = webview_processes(app.root)
    webview_listens = listening({pid for pid, _ in processes})
    check(not webview_listens, f"WebView2 listens without the variable: {webview_listens}")
    # The app's own loopback server for its local pages is expected; it must never be a DevTools endpoint.
    for address, _pid in listening(app_pids(app.root)):
        check(address.startswith("127.0.0.1:"), f"the app listens beyond loopback: {address}")
        check(not devtools_answer(address), f"{address} answers as a DevTools endpoint")
    check(not [c for _, c in processes if "remote-debugging" in c or "remote-allow-origins" in c],
          "a WebView2 process carries a debugging switch without the variable")
    check(not list((app.root / "state" / "webview").rglob("DevToolsActivePort")), "DevToolsActivePort exists")
    say(f"PASS: D2. without WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: {len(processes)} WebView2 processes, no listening "
        "port, no debugging switch, no DevToolsActivePort")

    # D3. §16.15: an install without "stub" (updated in place from v0.4) gets a --background shortcut.
    app.quit()
    original = dict(state)
    write_private(app.root / "install.json", {k: v for k, v in original.items() if k != "stub"})
    app.launch_run_value()
    wait_for("the v0.4 shortcut fix", lambda: shortcut_arguments(link) == "--background", timeout=180)
    log = (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
    check("Start menu shortcut for the v0.4 stub: rewritten" in log, "the shortcut rewrite was not logged")
    app.quit()
    write_private(app.root / "install.json", original)
    say("PASS: D3. without \"stub\" in install.json the app pointed the Start menu shortcut at --background and "
        "logged it")

    # D4. `RainCLI.exe --open` with the CDP switch for this launch only; local sign-in; the handoff.
    # The v0.5 `raincli login` already added a person session (§16.12 C15): end only that one, so the
    # window's own sign-in (person_only, no machine rotation) is what adds it back.
    app.cli("me", "sign-out")
    check(not (default_agent_config().parent / "person.json").exists(), "raincli me sign-out left person.json")
    check(window_machine_active(server, observer), "raincli me sign-out revoked the machine")
    port = free_port()
    env = dict(os.environ, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={port}")
    subprocess.Popen([str(app.root / "RainCLI.exe"), "--open"], env=env, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)

    def cdp():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
                return json.loads(response.read())
        except (OSError, ValueError):
            return None
    wait_for("the window's CDP endpoint", cdp, timeout=180)
    check(any(address.endswith(f":{port}") for address, _ in listening({pid for pid, _ in webview_processes(app.root)})),
          "the positive control failed: WebView2's CDP port is not seen as listening, so D2 proved nothing")
    check(devtools_answer(f"127.0.0.1:{port}"), "the positive control failed: no DevTools answer on the CDP port")
    with sync_playwright() as pw:
        window = WindowCDP(pw, port)
        page = window.page("sign-in.html")
        # Polled from here: the local pages' CSP (script-src 'self') rightly refuses wait_for_function's eval.
        wait_for("the sign-in page's machine name", lambda: page.input_value("#machine"), timeout=60, interval=1)
        check(page.evaluate("document.visibilityState") == "visible", "--open did not show the window")
        shot(page, "d4-sign-in")
        page.fill("#email", EMAIL)
        page.fill("#password", password)
        page.click("#submit")
        page.wait_for_url("**/app/inbox", timeout=120000)
        whoami = page.inner_text(".rc-whoami")
        check(EMAIL in whoami and "Signed in as" in whoami, f"the inbox does not say who is signed in: {whoami!r}")
        check("RainCLIApp/" not in page.evaluate("navigator.userAgent"), "the install token is in the global UA")
        shot(page, "d4-inbox")
        say("PASS: D4. RainCLI.exe --open showed the window; the local sign-in added a person session, the "
            "handoff opened the inbox and the rail shows who is signed in; the token is not in navigator.userAgent")

        # D5. Read, reply and send.
        send(server, observer, {"person": EMAIL}, "Hello from the observer machine")
        page.reload()
        page.click(".rc-conv")
        page.wait_for_selector(".rc-thread")
        check("Hello from the observer machine" in page.inner_text(".rc-thread"), "the thread is missing the message")
        shot(page, "d5-thread")
        page.click(".rc-msg a:has-text('Reply')")
        page.fill("#body", "A reply from the app window")
        page.click("button[type=submit]")
        page.wait_for_url("**notice=sent**", timeout=60000)
        page.click(".rc-new")
        page.fill("#to", OBSERVER)
        page.fill("#body", "A new message from the app window")
        page.click("button[type=submit]")
        page.wait_for_url("**notice=sent**", timeout=60000)
        wait_for("the observer to receive the reply and the new message",
                 lambda: {"A reply from the app window", "A new message from the app window"}
                 <= set(inbox_bodies(server, observer)), timeout=120)
        shot(page, "d5-sent")
        say("PASS: D5. the window read the observer's message, replied and sent a new one; both reached it")

        # D6. This computer and Settings (the /app/local sentinel shows the bundled pages). The app cancels the
        # hosted navigation and loads its own page, so the click must not wait for that navigation.
        page.click("nav.rc-nav a:has-text('This computer')", no_wait_after=True)  # the app cancels it (sentinel)
        page = window.page("this-computer.html", timeout=60)  # re-attached after the swap
        wait_for("This computer to show the machine", lambda: page.inner_text("#machine") == WINDOW_MACHINE,
                 timeout=60, interval=1)
        shot(page, "d6-this-computer")
        page.click("nav.rc-nav button[data-open=settings]", no_wait_after=True)  # the app navigates, not the click
        page = window.page("settings.html", timeout=60)  # re-attached after the swap
        page.wait_for_selector("#routing-all:checked", timeout=60000)
        shot(page, "d6-settings")
        say("PASS: D6. This computer shows the machine; Settings shows the routing policy")

        # D7. Offline and Retry.
        server.stop()
        page.click("nav.rc-nav button[data-open=inbox]", no_wait_after=True)  # the app navigates, not the click
        page = window.page("offline.html", timeout=120)  # re-attached after the swap
        shot(page, "d7-offline")
        server.start()
        page.click("#retry", no_wait_after=True)  # the app navigates, not the click
        page = window.page("app/inbox", timeout=120)  # re-attached after the swap
        say("PASS: D7. with the server stopped the window showed the offline page; Retry returned to the inbox")

        # D8. Sign-out from This computer: confirmed, revoked, the profile marked for reset.
        page.click("nav.rc-nav a:has-text('This computer')", no_wait_after=True)  # the app cancels it (sentinel)
        page = window.page("this-computer.html", timeout=60)  # re-attached after the swap
        wait_for("This computer to show the machine", lambda: page.inner_text("#machine") == WINDOW_MACHINE,
                 timeout=60, interval=1)
        page.click("#sign-out", no_wait_after=True)  # the app navigates, not the click
        click_dialog_ok("Sign out")
        page = window.page("sign-in.html", timeout=120)  # re-attached after the swap
        shot(page, "d8-signed-out")
        window.close()
    wait_for("the window machine to be revoked",
             lambda: (entry(server, observer, WINDOW_MACHINE) or {}).get("active") is False, timeout=120)
    check(not default_agent_config().exists(), "sign-out left the credential")
    check((app.root / "state" / "reset-profile").is_file(), "sign-out did not mark the WebView2 profile for reset")
    check("RainCLIApp/" not in (work / "server" / "server.log").read_text("utf-8", errors="replace"),
          "the server log holds the app's User-Agent")
    say("PASS: D8. sign-out in the window revoked the machine, deleted its credential and marked the profile "
        "for reset; the server log has no app User-Agent")

    # D9. A full install restores the no-argument shortcut and a new install_stamp.
    app.quit()
    app.uninstall(signout=False, label="d")
    app.install(NEW, installers, "window-again")
    again = app.install_json()
    check(shortcut_arguments(link) == "" and again.get("stub") == 2
          and again.get("install_stamp") not in (None, original.get("install_stamp")),
          f"a full install did not restore the shortcut or renew the stamp: {again}")
    app.quit()
    app.uninstall(signout=False, label="d-again")
    say("PASS: D9. a full install restored the no-argument shortcut, with stub 2 and a new install_stamp")


def window_machine_active(server, observer):
    return (entry(server, observer, WINDOW_MACHINE) or {}).get("active") is not False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- stale or foreign setups (part E) --------------------------------------------------------------------

OTHER_EMAIL, OTHER_TEAM = "e2e-other@example.invalid", "e2e-other-team"


def open_window_cdp(app, path_first=None):
    """Start the app with ``RainCLI.exe --open`` and the CDP switch for this launch only; the port.
    ``path_first`` is put first on the app's PATH (the pinned Codex for E6)."""
    port = free_port()
    env = dict(os.environ, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={port}")
    if path_first is not None:
        env["PATH"] = str(path_first) + os.pathsep + env.get("PATH", "")
    subprocess.Popen([str(app.root / "RainCLI.exe"), "--open"], env=env, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    wait_for("the window's CDP endpoint", lambda: devtools_answer(f"127.0.0.1:{port}"), timeout=180)
    return port


def register_other(server, handle, revoked=False):
    """A machine of ANOTHER account, in its own team; optionally revoked. Returns (api_url, token)."""
    staged = server.work / f"{handle}.json"
    server.admin("register-agent", "--team", OTHER_TEAM, "--owner", OTHER_EMAIL, "--handle", handle,
                 "--out", str(staged))
    issued = json.loads(staged.read_text())
    staged.unlink()
    if revoked:
        server.admin("revoke-agent", "--team", OTHER_TEAM, "--handle", handle)
    return issued["api_url"], SECRETS.add(issued["token"])


def backups(config_dir):
    return sorted(config_dir.glob("replaced-*"))


def backup_files(config_dir):
    return {p.relative_to(b).as_posix() for b in backups(config_dir) for p in b.rglob("*") if p.is_file()}


def window_sign_in(page, password, machine=None):
    page.fill("#email", EMAIL)
    if machine is not None:
        page.fill("#machine", machine)
    page.fill("#password", password)
    page.click("#submit")


def signed_in_fresh(server, observer, handle, config_dir):
    e = entry(server, observer, handle) or {}
    return (e.get("active") is not False and e.get("handle") == handle
            and (config_dir / "person.json").is_file() and (config_dir / "agent.json").is_file())


def part_e(app, server, installers, password, observer, work, codex=None):
    """Stale or foreign saved setups; see E in the module docstring."""
    from playwright.sync_api import sync_playwright

    config_dir = default_agent_config().parent
    other_password = SECRETS.add(secrets.token_urlsafe(18))
    server.admin("create-user", "--email", OTHER_EMAIL, "--name", "Other Owner", input=other_password + "\n")
    server.admin("create-team", "--slug", OTHER_TEAM, "--name", "Other team", "--owner", OTHER_EMAIL)

    # E1. A revoked credential of another account: set aside by migration, then a fresh sign-in.
    fresh_slate(app)
    api_url, token = register_other(server, "e2e-other-revoked", revoked=True)
    write_private(default_agent_config(), {"api_url": api_url, "token": token})
    write_private(config_dir / "connector.json", {"herdr_agent": "e2e-inbox", "herdr_bin": "raincli-e2e-no-herdr",
                                                   "state_dir": "e1-queue", "poll_wait": 1})
    (config_dir / "e1-queue").mkdir()
    (config_dir / "e1-queue" / "keep.txt").write_text("the old queue stays in place")
    app.install(NEW, installers, "e1")
    wait_for("migration to set the revoked credential aside", lambda: backups(config_dir), timeout=300)
    moved = backup_files(config_dir)
    check({"agent.json", "connector.json"} <= moved, f"the backup holds {sorted(moved)}")
    check(not default_agent_config().exists() and not (config_dir / "connector.json").exists(),
          "the revoked setup was not moved")
    check((config_dir / "e1-queue" / "keep.txt").is_file(), "the old queue was not left in place")
    wait_for("the app to show sign-in after the set-aside",
             lambda: "showing sign-in" in (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
             if (app.root / "app-lock" / "app.log").is_file() else False, timeout=180)
    app.quit()
    port = open_window_cdp(app)
    with sync_playwright() as pw:
        window = WindowCDP(pw, port)
        page = window.page("sign-in.html")
        wait_for("the sign-in page's machine name", lambda: page.input_value("#machine"), timeout=60, interval=1)
        shot(page, "e1-sign-in")
        window_sign_in(page, password, "e2e-fresh-one")
        page = window.page("app/inbox", timeout=180)
        check(EMAIL in page.inner_text(".rc-whoami"), "the fresh sign-in did not open the owner's inbox")
        shot(page, "e1-inbox")
        window.close()
    wait_for("the fresh machine and its person session",
             lambda: signed_in_fresh(server, observer, "e2e-fresh-one", config_dir), timeout=120)
    check("e2e-other-revoked" not in handles(server, observer), "the other account's machine is in this team")
    say("PASS: E1. a revoked credential of another account: migration set it aside (agent.json and the connector "
        "config in replaced-<stamp>, the queue left in place); the window's sign-in as the team owner made "
        "e2e-fresh-one with a person session")
    app.quit()
    app.uninstall(signout=True, label="e1")

    # E2. A live credential of another user: adopted, then the window explains it and offers a new machine.
    fresh_slate(app)
    api_url, token = register_other(server, "e2e-other-live")
    write_private(default_agent_config(), {"api_url": api_url, "token": token})
    write_private(config_dir / "connector.json", {"herdr_agent": "e2e-inbox", "herdr_bin": "raincli-e2e-no-herdr",
                                                   "state_dir": "e2-queue", "poll_wait": 1})
    app.install(NEW, installers, "e2")
    wait_for("migration to adopt the live credential", lambda: migrated(config_dir), timeout=300)
    check(not backups(config_dir), "a live credential was set aside without the user's choice")
    app.quit()
    port = open_window_cdp(app)
    sentence = ("This computer's saved RainCLI setup belongs to a machine owned by another account. "
                "The other machine stays active for its owner until they revoke it.")
    with sync_playwright() as pw:
        window = WindowCDP(pw, port)
        page = window.page("sign-in.html")
        wait_for("the sign-in page's machine name", lambda: page.input_value("#machine"), timeout=60, interval=1)
        window_sign_in(page, password)
        wait_for("the new-machine offer", lambda: page.is_visible("#new-machine"), timeout=120, interval=1)
        check(page.inner_text("#message") == sentence, f"the offer says {page.inner_text('#message')!r}")
        check(not backups(config_dir), "the setup moved before the user chose to")
        check(page.input_value("#machine") != "e2e-other-live", "the suggested name is the old handle")
        shot(page, "e2-offer")
        page.click("#new-machine")
        check(page.input_value("#password") == "", "the password was kept for the new-machine sign-in")
        page.fill("#machine", "e2e-fresh-two")
        page.fill("#password", password)
        page.click("#submit")
        page = window.page("app/inbox", timeout=180)
        shot(page, "e2-inbox")
        window.close()
    wait_for("the second fresh machine", lambda: signed_in_fresh(server, observer, "e2e-fresh-two", config_dir),
             timeout=120)
    check({"agent.json", "connector.json", "runtime.json"} <= backup_files(config_dir),
          f"the backup lacks the old files: {sorted(backup_files(config_dir))}")
    other = api(server, token, "/api/v1/me")
    check(other["agent"]["handle"] == "e2e-other-live", "the other user's machine stopped working")
    say("PASS: E2. a live credential of another user was adopted; the window's sign-in said whose it is and "
        "offered a new machine, which (password typed again) made e2e-fresh-two; the other machine stays active")
    app.quit()
    app.uninstall(signout=True, label="e2")

    # E3. A connector config outside the scan directories, named by a connector-mode runtime.json (V3).
    fresh_slate(app)
    api_url, token = register_other(server, "e2e-other-outside", revoked=True)
    write_private(default_agent_config(), {"api_url": api_url, "token": token})
    outside = work / "elsewhere" / "outside-connector.json"
    write_private(outside, {"agent_config": str(default_agent_config()), "herdr_agent": "e2e-inbox",
                            "herdr_bin": "raincli-e2e-no-herdr", "state_dir": str(work / "elsewhere" / "q"),
                            "poll_wait": 1})
    write_private(config_dir / "runtime.json", {"connectors": [str(outside)],
                                                "state_dir": str(profile() / ".raincli" / "runtime-e3")})
    app.install(NEW, installers, "e3")
    wait_for("migration to set the outside setup aside", lambda: backups(config_dir), timeout=300)
    check(not outside.exists() and not (config_dir / "runtime.json").exists(),
          "the connector config outside the scan directories, or its runtime.json, was not moved")
    check({"agent.json", "runtime.json"} <= backup_files(config_dir), f"the backup holds {backup_files(config_dir)}")
    say("PASS: E3. a connector config outside the scan directories, named by a connector-mode runtime.json, moved "
        "with the revoked credential")
    app.quit()
    app.uninstall(signout=False, label="e3")

    # E4. Two credentials in one connector runtime, one revoked: only that one is set aside (V2).
    fresh_slate(app)
    good_url, good_token = register(server, "e2e-two-good")
    bad_url, bad_token = register_other(server, "e2e-two-revoked", revoked=True)
    good_agent, bad_agent = config_dir / "good-agent.json", config_dir / "bad-agent.json"
    write_private(good_agent, {"api_url": good_url, "token": good_token})
    write_private(bad_agent, {"api_url": bad_url, "token": bad_token})
    good_connector, bad_connector = config_dir / "good-connector.json", config_dir / "bad-connector.json"
    for connector, agent, queue in ((good_connector, good_agent, "good-queue"), (bad_connector, bad_agent, "bad-queue")):
        write_private(connector, {"agent_config": str(agent), "herdr_agent": "e2e-inbox",
                                  "herdr_bin": "raincli-e2e-no-herdr", "state_dir": queue, "poll_wait": 1})
    write_private(config_dir / "runtime.json", {"connectors": [str(good_connector), str(bad_connector)],
                                                "state_dir": str(profile() / ".raincli" / "runtime-e4")})
    app.install(NEW, installers, "e4")
    wait_for("migration to set only the revoked credential aside", lambda: backups(config_dir), timeout=300)

    def connectors_named():
        try:
            runtime = json.loads((config_dir / "runtime.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return [Path(c).name for c in runtime.get("connectors", [])]
    try:  # the backup appears first; the rewrite follows within the same set-aside
        wait_for("runtime.json rewritten without the revoked credential",
                 lambda: connectors_named() == ["good-connector.json"], timeout=180)
    except Failure:
        raise Failure(f"runtime.json was not rewritten without the revoked credential: {connectors_named()}") from None
    check(good_agent.exists() and good_connector.exists() and not bad_agent.exists() and not bad_connector.exists(),
          "the wrong files moved")
    check({"bad-agent.json", "bad-connector.json", "runtime.json"} <= backup_files(config_dir),
          f"the backup holds {backup_files(config_dir)}")
    wait_for("the valid credential's presence", lambda: shows(server, observer, "e2e-two-good", NEW), timeout=300)
    message = send(server, observer, "e2e-two-good", "after the stale one was set aside")
    wait_for("delivery to the valid credential", lambda: delivered(server, observer, message), timeout=240)
    set_aside_log = config_dir / "runtime-state" / "migration.log"
    records = [json.loads(line) for line in set_aside_log.read_text("utf-8").splitlines() if line.strip()]
    check([(Path(r["agent_config"]).name, r["result"]) for r in records if r.get("event") == "stale_credential_set_aside"]
          == [("bad-agent.json", "invalid")], f"the set-aside log holds {records}")
    say("PASS: E4. two credentials, one revoked: only the revoked one moved and was logged "
        "stale_credential_set_aside (runtime.json rewritten, the original kept in the backup), and the valid one "
        "keeps delivering")
    app.quit()
    app.uninstall(signout=False, label="e4")

    # E5. An old machine-mode setup's held named-agent record and by-name handover box are never delivered (V1).
    from raincli_agent.connector.config import load_connector_config
    from raincli_agent.connector.queue import AGENT_HELD, Queue
    from raincli_agent.runtime import sessions

    fresh_slate(app)
    app.install(NEW, installers, "e5")
    app.quit()
    login_through_conpty(app, server, other_password, "e2e-v1-old", email=OTHER_EMAIL)
    connector = load_connector_config(str(config_dir / "runtime.json"))
    state = runtime_state_dir(config_dir)
    held_id = str(uuid.uuid4())
    Queue(connector.state_dir).save({"id": held_id, "seq": 1, "from": "e2e-old-teammate", "agent": "reviewer",
                                     "body": "an old held message", "state": AGENT_HELD, "hold_reason": "offline",
                                     "history": []})
    sessions.ensure_salt(str(state))
    sessions.sessions_dir(str(state), create=True)
    box = sessions.name_box("notes")
    sessions.hand_over(str(state), box, str(uuid.uuid4()), "an old by-name handover")
    held_files = lambda root: [p for p in root.rglob(f"{held_id}.json")]  # noqa: E731
    check(held_files(state), "the held record was not written")
    server.admin("revoke-agent", "--team", OTHER_TEAM, "--handle", "e2e-v1-old")
    revoked = ("This computer's saved RainCLI setup belongs to a machine that was revoked.")
    port = open_window_cdp(app)
    with sync_playwright() as pw:
        window = WindowCDP(pw, port)
        page = window.page("sign-in.html", timeout=180)  # the handoff was refused: sign-in, not offline
        wait_for("the sign-in page's machine name", lambda: page.input_value("#machine"), timeout=60, interval=1)
        window_sign_in(page, password)
        wait_for("the new-machine offer", lambda: page.is_visible("#new-machine"), timeout=120, interval=1)
        check(page.inner_text("#message") == revoked, f"the offer says {page.inner_text('#message')!r}")
        shot(page, "e5-offer")
        page.click("#new-machine")
        page.fill("#machine", "e2e-v1-fresh")
        page.fill("#password", password)
        page.click("#submit")
        page = window.page("app/inbox", timeout=180)
        window.close()
    wait_for("the fresh machine", lambda: signed_in_fresh(server, observer, "e2e-v1-fresh", config_dir), timeout=120)
    backup = backups(config_dir)[-1]
    check(held_files(backup), "the backup lacks the old held record")
    check(not state.exists() or not held_files(state), "the old held record is in the new state directory")
    check(sessions.claim(str(state), box) == ([], []), "the new state hands over the old by-name message")
    check(any((backup / "runtime-state" / "sessions").rglob("*")), "the backup lacks the old handover box")
    say("PASS: E5. an app machine's agent_held record and by-name handover box, after a revoke: the refused "
        "handoff showed sign-in, the owner's sign-in offered a new machine, and neither the record nor the box "
        "is in the new state (both are in the backup)")
    app.quit()

    # E6. This computer connects the pinned real Codex, only on the click (§16.19 item 3).
    if codex is None:
        say("SKIP: E6. no pinned Codex for this platform")
    else:
        codex_hooks = profile() / ".codex" / "hooks.json"
        before = codex_hooks.read_text("utf-8") if codex_hooks.is_file() else None
        port = open_window_cdp(app, path_first=codex.parent)
        with sync_playwright() as pw:
            window = WindowCDP(pw, port)
            page = window.page("app/inbox", timeout=180)
            page.click("nav.rc-nav a:has-text('This computer')", no_wait_after=True)  # the app cancels it
            page = window.page("this-computer.html", timeout=60)
            row = "#hooks li[data-agent=codex]"
            wait_for("Codex's state", lambda: page.is_visible(f"{row} button"), timeout=120, interval=1)
            check(page.inner_text(f"{row} button") == "Connect", f"Codex shows {page.inner_text(row)!r}")
            check((codex_hooks.read_text("utf-8") if codex_hooks.is_file() else None) == before,
                  "hooks were installed before the click")
            shot(page, "e6-this-computer")
            page.click(f"{row} button", no_wait_after=True)
            wait_for("Codex connected", lambda: "/hooks" in page.inner_text("#hooks-message"), timeout=120,
                     interval=1)
            wait_for("needs_approval", lambda: page.locator(f"{row} [data-state=needs_approval]").count() == 1,
                     timeout=60, interval=1)
            check("raincli" in codex_hooks.read_text("utf-8").lower(), "Connect did not write RainCLI's Codex hooks")
            shot(page, "e6-connected")
            page.click(f"{row} button", no_wait_after=True)  # Disconnect
            wait_for("Codex disconnected", lambda: page.locator(f"{row} [data-state=not_connected]").count() == 1,
                     timeout=120, interval=1)
            check(not codex_hooks.is_file() or "raincli" not in codex_hooks.read_text("utf-8").lower(),
                  "Disconnect left RainCLI's Codex hooks")
            window.close()
        say(f"PASS: E6. This computer showed the pinned Codex {codex_smoke.CODEX_VERSION} as Not connected; "
            "Connect (only on the click) wrote RainCLI's hooks and said to approve them in /hooks; Disconnect "
            "removed them")
        app.quit()
    app.uninstall(signout=True, label="e5")


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


def prepare_managed_install(work):
    """A managed v0.3.2 install in %USERPROFILE%\\.raincli\\client through the real updater (done
    before the hosts file changes; it fetches the canonical release from GitHub)."""
    root = profile() / ".raincli" / "client"
    check(not root.exists(), f"{root} already exists on this runner")
    try:
        release = updates.resolve(OLD_MANAGED)
    except updates.ReleaseNotFound:
        raise Failure(f"release {OLD_MANAGED} not found in {updates.REPO}") from None
    result = updates.install(root, release)
    check(result.get("status") == "installed", f"managed install of {OLD_MANAGED} returned {result.get('status')}")
    return root


def build_rollback_installer(work):
    """0.5.2 from a STAGING COPY of the client whose tray exits 1 when started in the background, so
    the stub's probation must roll it back. The shipped source is untouched (§15.8 M11)."""
    spec = importlib.util.spec_from_file_location("raincli_build", ROOT / "packaging" / "windows" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    staging = work / "rollback-source" / "raincli_agent"
    shutil.copytree(build.CLIENT, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    tray = staging / "app" / "tray.py"
    text = tray.read_text("utf-8")
    head = "def main(argv=None):\n"
    check(text.count(head) == 1, "raincli_agent/app/tray.py has no single main(argv=None) to patch")
    tray.write_text(text.replace(head, head + '    if (sys.argv[1:] if argv is None else list(argv)) == ["--background"]:\n'
                                               '        return 1  # this e2e\'s rollback build only\n'), "utf-8")
    out = work / "rollback-installer"
    setup = build.build(BROKEN, work / "rollback-build", out, source=staging)
    say(f"PASS: rollback build {BROKEN} from a patched staging copy: {setup.name}")
    return out


def start_console(args, log):
    """A process in its own console window, as a user would have started it."""
    with open(log, "ab") as out:
        return subprocess.Popen([str(a) for a in args], stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)


def end(process, timeout=60):
    if process is not None and process.poll() is None:
        process.terminate()  # what closing its console window does
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=timeout)
        if process.poll() is None:
            process.kill()


def fresh_slate(app):
    """Between parts: no app, no Run value and no test configs left from the previous part."""
    check(not (app.root / "RainCLI.exe").exists() and app.run_value() is None, "the previous part left the app")
    for path in (app.root, profile() / ".config" / "raincli"):
        if path.exists():
            shutil.rmtree(path)


def register(server, name):
    staged = server.work / f"{name}.json"
    server.admin("register-agent", "--team", TEAM, "--owner", EMAIL, "--handle", name, "--out", str(staged))
    issued = json.loads(staged.read_text())
    staged.unlink()
    return issued["api_url"], SECRETS.add(issued["token"])


TRUST_PROBE = """
import socket, ssl, sys
context = ssl.create_default_context()
with socket.create_connection(("127.0.0.1", 443), timeout=20) as raw:
    with context.wrap_socket(raw, server_hostname="api.github.com") as tls:
        print("handshake ok", tls.version())
"""


def trust_probe(work, ca):
    """A default-context handshake as api.github.com in a fresh Python, as the client makes one."""
    result = subprocess.run([sys.executable, "-c", TRUST_PROBE], capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        say(f"PASS: the Windows trust store trusts the test root CA ({result.stdout.strip()})")
        return
    say("trust probe failed:\n" + (result.stdout + result.stderr)[-2000:])
    trust_diagnostics(work, ca)
    raise Failure("Python's default context does not trust the test root CA")


def trust_diagnostics(work, ca):
    """What the runner's stores hold for the test CA, and whether the chain itself verifies."""
    import base64 as b64

    say("===== TRUST DIAGNOSTICS =====")
    pem = (work / "ca.pem").read_text()
    der = b64.b64decode("".join(l for l in pem.splitlines() if l and not l.startswith("-----")))
    say(f"python {sys.version.split()[0]}, {ssl.OPENSSL_VERSION}; CA CN {ca.cn!r}; CA DER sha256 "
        f"{hashlib.sha256(der).hexdigest()[:16]}")
    for store in ("ROOT", "CA", "MY"):
        try:
            entries = ssl.enum_certificates(store)
        except OSError as exc:
            say(f"enum_certificates({store}) failed: {exc}")
            continue
        hits = [(enc, trust) for cert, enc, trust in entries if cert == der]
        named = [(enc, trust) for cert, enc, trust in entries if ca.cn.encode() in cert and cert != der]
        say(f"enum_certificates({store}): {len(entries)} entries; exact CA match {hits}; same CN, other DER {named}")
    context = ssl.create_default_context()
    loaded = [c for c in context.get_ca_certs() if any(ca.cn in v for rdn in c.get("subject", ()) for _k, v in rdn)]
    say(f"create_default_context(): {len(context.get_ca_certs())} CA certs loaded; test CA among them: "
        f"{bool(loaded)}; verify_flags {context.verify_flags!r}")
    for label, make in (("cafile=ca.pem, strict", lambda: ssl.create_default_context(cafile=str(work / "ca.pem"))),
                        ("cafile=ca.pem, not strict", lambda: _not_strict(ssl.create_default_context(
                            cafile=str(work / "ca.pem")))),
                        ("default store, not strict", lambda: _not_strict(ssl.create_default_context()))):
        try:
            with socket.create_connection(("127.0.0.1", 443), timeout=20) as raw:
                with make().wrap_socket(raw, server_hostname="api.github.com"):
                    say(f"handshake with {label}: ok")
        except (OSError, ssl.SSLError) as exc:
            say(f"handshake with {label}: {type(exc).__name__}: {exc}")
    tool = openssl()
    for args in (["version"], ["verify", "-x509_strict", "-CAfile", work / "ca.pem", work / "leaf.pem"],
                 ["x509", "-noout", "-text", "-in", work / "ca.pem"], ["x509", "-noout", "-text", "-in", work / "leaf.pem"]):
        out = run([tool, *args], check=False)
        say(f"openssl {args[0]} {' '.join(str(a) for a in args[1:2])}: exit {out.returncode}\n"
            + "\n".join(l for l in (out.stdout + out.stderr).splitlines()
                        if "Modulus" not in l and not l.strip().replace(":", "").isalnum())[:3000])
    out = run(["certutil", "-store", "Root", ca.cn], check=False)
    say(f"certutil -store Root: exit {out.returncode}\n{(out.stdout + out.stderr)[-1500:]}")
    say("===== END TRUST DIAGNOSTICS =====")


def _not_strict(context):
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def managed_launchers(managed):
    """Process ids running the managed install's launcher (it is not this script's child)."""
    marker = str(managed / "launch.py").casefold()
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
                          "ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.CommandLine }"],
                         capture_output=True, text=True, timeout=60).stdout
    flat = [(line, " ".join(line.casefold().replace('"', " ").split())) for line in out.splitlines()]
    return [int(line.split(" ", 1)[0]) for line, words in flat
            if marker in line.casefold() and " runtime run " in words]


def migrated(config_dir):
    """True once migration converted the token and runtime.json is in connector mode."""
    try:
        agent = json.loads((config_dir / "agent.json").read_text("utf-8"))
        runtime = json.loads((config_dir / "runtime.json").read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return ("token_dpapi" in agent and "token" not in agent and bool(runtime.get("connectors"))
            and not runtime.get("machine_config"))


def runtime_state_dir(config_dir):
    runtime = json.loads((config_dir / "runtime.json").read_text("utf-8"))
    state = Path(runtime.get("state_dir") or "runtime-state")
    return state if state.is_absolute() else config_dir / state


def run_e2e(args, work, stack):
    installers = download_release_installers(work) if args.real else Path(args.installers).resolve()
    fake = None
    if not args.real:
        # First, so a trust problem fails in a minute: the test root CA and the fake endpoint, then a
        # handshake as api.github.com through Python's default context in a fresh process.
        ca = TestCA(work)
        ca.create()
        stack.callback(ca.remove)
        fake = FakeGitHub(work, installers, (OLD, NEW))
        fake.start(str(work / "leaf.pem"), str(work / "leaf.key"))
        stack.callback(fake.stop)
        trust_probe(work, ca)
    server = release_e2e.Server(work / "server", args.server_python)
    server.work.mkdir()
    server.prepare()
    # Everything that needs the real GitHub happens before the hosts file changes.
    old_venv = prepare_old_pip_client(work)
    say(f"PASS: old pip client {OLD_PIP} installed from its release archive")
    managed = prepare_managed_install(work)
    codex = None if args.real else codex_smoke.fetch_codex(work)  # before the hosts file: the real GitHub
    stack.callback(lambda: release_e2e.kill_marked({str(managed)}))
    say(f"PASS: managed {OLD_MANAGED} installed in {managed} through the real updater")
    rollback = None if args.real else build_rollback_installer(work)
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
    observer = register(server, OBSERVER)[1]
    say(f"PASS: throwaway server on {server.url}; user, team {TEAM} and observer {OBSERVER}")

    if args.real:
        say(f"PASS: published installers of v{OLD} and v{NEW} downloaded and checksum-verified")
    else:
        fake.add_release(rollback, BROKEN)
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
    stack.callback(lambda: app.quit(required=False))
    check(not app.root.exists() and app.run_value() is None, "the runner already has a RainCLI app install")
    menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "RainCLI"
    stub_command = f'"{app.root}\\RainCLI.exe" --background'

    # == A. a fresh install, signed in from the CLI ==========================================
    # A1. silent per-user install
    app.install(OLD, installers, "first")
    layout = [app.root / "RainCLI.exe", app.root / "_internal", app.root / "bin" / "raincli.exe",
              app.root / "bin" / "_internal", app.root / "unins000.exe",
              app.root / "versions" / OLD / "RainCLI-app.exe", app.root / "versions" / OLD / "raincli.exe"]
    check(all(p.exists() for p in layout), f"missing after install: {[str(p) for p in layout if not p.exists()]}")
    first_state = app.install_json()
    check({k: first_state.get(k) for k in ("current", "previous", "probation")}
          == {"current": OLD, "previous": None, "probation": None}, f"install.json is {first_state}")
    if not args.real:
        check(first_state.get("stub") == 2 and first_state.get("install_stamp"), f"install.json is {first_state}")
    check(app.run_value() == stub_command, f"Run value is {app.run_value()!r}")
    check(reg_values(UNINSTALL_KEY) is not None, "no per-user (HKCU) uninstall key")
    import winreg
    check(reg_values(UNINSTALL_KEY, winreg.HKEY_LOCAL_MACHINE) is None, "a machine-wide uninstall key was written")
    check(user_path_entries()[:1] == [str(app.root / "bin")], f"user PATH starts {user_path_entries()[:1]}")
    check((menu / "RainCLI.lnk").is_file() and (menu / "Uninstall RainCLI.lnk").is_file(), "Start menu entries missing")
    record = (app.root / "installer-record.log").read_text("utf-8")
    check("run value" in record and ": none" in record and "scheduled tasks: could not be listed" not in record,
          "installer-record.log did not record the Run value and the Scheduled Tasks first")
    check(app.cli("--version").stdout.strip() == f"raincli {OLD}", "the PATH shim does not run the current CLI")
    say(f"PASS: A1. silent per-user install of {OLD}: onedir layout, install.json, HKCU Run value and uninstall "
        "key, shim first on PATH, Start menu, H7 record (Run value and Scheduled Tasks)")

    # A2. --quit, then sign-in through the installed CLI with the password on a pseudo console
    app.quit()
    login_through_conpty(app, server, password, MACHINE)
    agent_config = json.loads(default_agent_config().read_text("utf-8"))
    check("token_dpapi" in agent_config and "token" not in agent_config, "agent.json is not in the DPAPI form")
    runtime = json.loads((default_agent_config().parent / "runtime.json").read_text("utf-8"))
    check(runtime.get("machine_config") and not runtime.get("connectors"), "runtime.json is not machine mode")
    check(MACHINE in handles(server, observer), "the server has no signed-in machine")
    say(f"PASS: A2. RainCLI.exe --quit exited 0 with nothing left running; raincli login through a pseudo console "
        f"created {MACHINE} with a DPAPI credential and a machine-mode runtime config")

    # A3. what the Run value names, then presence
    check(app.launch_run_value() == stub_command, "the launched command is not the Run value")
    wait_for(f"presence {OLD} automatic current", lambda: shows(server, observer, MACHINE, OLD), timeout=240)
    check(app.runs(OLD), f"RainCLI-app.exe from versions\\{OLD} is not running")
    say(f"PASS: A3. launched the Run value; presence reports {OLD}, automatic, current")

    # A4. pushed upgrade through the installer assets
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
    say(f"PASS: A4. pushed {NEW} through the {'published' if args.real else 'fake'} release assets"
        f"{'' if args.real else ' (API asset URL, exact download hosts)'}: /UPDATE left the Run value and "
        "uninstall key alone, install.json swapped, relaunched from the new version")

    # A5. a version whose tray never starts is rolled back by the stub
    if rollback is not None:
        server.admin("set-client-version", "--team", TEAM, "v" + BROKEN)
        wait_for(f"the rollback from {BROKEN}", lambda: shows(server, observer, MACHINE, NEW, "rolled_back",
                                                              "first_start_failed"), timeout=WAIT)
        wait_for(f"RainCLI-app.exe from versions\\{NEW} after the rollback", lambda: app.runs(NEW), timeout=240)
        state = app.install_json()
        check(state.get("current") == NEW and not state.get("probation"), f"install.json after the rollback: {state}")
        say(f"PASS: A5. pushed {BROKEN}, whose tray exits 1: the stub rolled back, the server shows rolled_back, "
            f"and {NEW} runs again")

    # A6. downgrade refused, then allowed
    server.admin("set-client-version", "--team", TEAM, "v" + OLD)
    wait_for("the refused downgrade", lambda: shows(server, observer, MACHINE, NEW, "failed", "downgrade_not_allowed"),
             timeout=240)
    time.sleep(40)
    check(app.install_json().get("current") == NEW, "the version changed without --allow-downgrade")
    server.admin("set-client-version", "--team", TEAM, "v" + OLD, "--allow-downgrade")
    wait_for(f"the allowed downgrade to {OLD}", lambda: shows(server, observer, MACHINE, OLD))
    wait_for(f"RainCLI-app.exe from versions\\{OLD}", lambda: app.runs(OLD), timeout=240)
    server.admin("set-client-version", "--team", TEAM, "--clear")
    say(f"PASS: A6. downgrade to {OLD} refused, then allowed with --allow-downgrade; target cleared")

    # A7. uninstall that signs out
    app.uninstall(signout=True, label="a7")
    check((entry(server, observer, MACHINE) or {}).get("active") is False, "sign-out did not revoke the machine")
    check(not default_agent_config().exists(), "the uninstall's sign-out left the credential")
    gone = [app.root / "RainCLI.exe", app.root / "_internal", app.root / "bin", app.root / "versions",
            app.root / "install.json", app.root / "heartbeat.json", app.root / "app-lock", menu]
    check(not [p for p in gone if p.exists()], f"uninstall left {[str(p) for p in gone if p.exists()]}")
    check(app.run_value() is None and str(app.root / "bin") not in user_path_entries(),
          "uninstall left the Run value or the PATH entry")
    check((app.root / "installer-record.log").is_file(), "uninstall removed the installer record")
    say("PASS: A7. uninstall with /SIGNOUT=yes revoked the machine and deleted its credential, and removed the Run "
        "value, PATH entry, shortcuts, versions, stub, shim and install.json; the installer record is kept")

    # == D. the app window over CDP ==================================================================
    if args.real:
        say("SKIP: D. the published v0.4 installers have no app window")
    else:
        fresh_slate(app)
        part_d(app, server, installers, password, observer, menu, work)

    # == E. a stale or foreign saved setup ==================================================================
    if args.real:
        say("SKIP: E. the published v0.4 installers have no v0.5.1 stale-setup handling")
    else:
        part_e(app, server, installers, password, observer, work, codex=codex)

    # == B. an old pip client's foreground connector ============================================
    fresh_slate(app)
    api_url, pip_token = register(server, OLD_MACHINE)
    config_dir = default_agent_config().parent
    write_private(default_agent_config(), {"api_url": api_url, "token": pip_token})
    connector = config_dir / "connector.json"
    write_private(connector, {"herdr_agent": "e2e-inbox", "herdr_bin": "raincli-e2e-no-herdr",
                              "state_dir": "old-queue", "poll_wait": 1})  # no agent_config; a relative state_dir
    queue = config_dir / "old-queue"
    old_raincli = old_venv / "Scripts" / "raincli.exe"
    old_connector = start_console([old_raincli, "connector", "run", "--config", connector], work / "old-connector.log")
    stack.callback(end, old_connector)
    first = send(server, observer, OLD_MACHINE, "before migration")
    wait_for("the old pip connector to take a message", lambda: delivered(server, observer, first), timeout=240)
    before = handles(server, observer)
    queue_files = sorted(p.name for p in queue.rglob("*") if p.is_file())
    check(queue_files, "the old connector wrote nothing under its relative state_dir")
    app.install(NEW, installers, "pip-migration")  # its last step starts the tray, whose first run migrates
    time.sleep(45)  # the old connector still holds its queue: migration must wait, touching nothing
    untouched = json.loads(default_agent_config().read_text("utf-8"))
    check(untouched.get("token") and "token_dpapi" not in untouched and not (config_dir / "runtime.json").exists(),
          "migration changed the configs while the old connector was still running")
    end(old_connector)  # the user closes the old RainCLI window; the tray's migration then continues by itself
    wait_for("migration to finish (DPAPI token, connector-mode runtime.json)",
             lambda: migrated(config_dir), timeout=300)
    wait_for("the migrated machine's presence", lambda: shows(server, observer, OLD_MACHINE, NEW), timeout=300)
    check(handles(server, observer) == before, "migration created or removed a machine")
    check(api(server, pip_token, "/api/v1/me")["agent"]["handle"] == OLD_MACHINE, "the old credential stopped working")
    check(app.run_value() == stub_command, "the Run value does not start the app after migration")
    second = send(server, observer, OLD_MACHINE, "after migration")
    wait_for("delivery after migration", lambda: delivered(server, observer, second), timeout=240)
    check(set(queue_files) <= {p.name for p in queue.rglob("*") if p.is_file()}, "migration dropped queue files")
    logs = [p for p in [runtime_state_dir(config_dir) / "migration.log"] if p.is_file()]
    check(logs and pip_token not in logs[0].read_text("utf-8", errors="replace"),
          "no migration.log under the runtime state_dir, or it holds the token")
    say(f"PASS: B. pip {OLD_PIP} foreground connector (no runtime.json, no agent_config, relative state_dir): "
        f"migration waited for the old window, then kept {OLD_MACHINE} and its credential (DPAPI), wrote a "
        "connector-mode runtime.json, and delivery continues; no new machine")
    app.uninstall(signout=False, label="b")
    check(default_agent_config().is_file() and connector.is_file() and queue.is_dir() and logs[0].is_file(),
          "uninstall without sign-out removed agent.json, the connector config, the queue or migration.log")
    check(api(server, pip_token, "/api/v1/me")["agent"]["handle"] == OLD_MACHINE, "uninstall signed the machine out")
    say("PASS: B. uninstall with /SIGNOUT=no kept agent.json, the connector config, the queue and migration.log")

    # == C. a managed v0.3 install in the documented layout, started by its Run value ===============
    # SETUP.md step 5 and the Windows guide: the credential, the connector and runtime.json all in
    # ~/.config/raincli, logon start through `runtime startup --config ~/.config/raincli/runtime.json`.
    # The app's own default runtime config is the same file, which is review 2's R1 case.
    fresh_slate(app)
    api_url, managed_token = register(server, MANAGED_MACHINE)
    write_private(default_agent_config(), {"api_url": api_url, "token": managed_token})
    connector = config_dir / "connector.json"
    write_private(connector, {"agent_config": str(default_agent_config()), "herdr_agent": "e2e-inbox",
                              "herdr_bin": "raincli-e2e-no-herdr", "poll_wait": 1})  # the default queue directory
    runtime_json = config_dir / "runtime.json"
    check(runtime_json == default_agent_config().parent / "runtime.json", "not the default runtime.json")
    write_private(runtime_json, {"connectors": [str(connector)], "state_dir": str(profile() / ".raincli" / "runtime")})
    launcher = managed / "launch.py"
    run([sys.executable, launcher, "runtime", "startup", "--config", runtime_json], timeout=300)
    old_value = app.run_value()
    check(old_value and "launch.py" in old_value and str(runtime_json).casefold() in old_value.casefold(),
          f"the managed install's Run value does not run the launcher on the default runtime.json: {old_value!r}")
    # Logon runs exactly the Run value's command line.
    subprocess.Popen(old_value, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    wait_for("the managed launcher started from the Run value", lambda: managed_launchers(managed), timeout=120)
    wait_for(f"the managed {OLD_MANAGED} runtime's presence",
             lambda: shows(server, observer, MANAGED_MACHINE, OLD_MANAGED[1:]), timeout=300)
    before = handles(server, observer)
    app.install(NEW, installers, "managed-migration")
    record = (app.root / "installer-record.log").read_text("utf-8")
    check(old_value in record, "the installer did not record the managed install's Run value")
    wait_for("the managed launcher to stop on the app's stop request", lambda: not managed_launchers(managed),
             timeout=600)
    wait_for("migration to finish (DPAPI token, connector-mode runtime.json)",
             lambda: migrated(config_dir), timeout=300)
    wait_for("the migrated managed machine's presence", lambda: shows(server, observer, MANAGED_MACHINE, NEW),
             timeout=300)
    check((runtime_state_dir(config_dir) / "migration.log").is_file(), "no migration.log under the runtime state_dir")
    wait_for("the Run value pointing at the stub", lambda: app.run_value() == stub_command, timeout=120)
    check(handles(server, observer) == before, "migration created or removed a machine")
    check(api(server, managed_token, "/api/v1/me")["agent"]["handle"] == MANAGED_MACHINE,
          "the managed credential stopped working")
    third = send(server, observer, MANAGED_MACHINE, "after managed migration")
    wait_for("delivery after the managed migration", lambda: delivered(server, observer, third), timeout=240)
    say(f"PASS: C. managed {OLD_MANAGED} in the documented layout (default runtime.json, started by its Run value): "
        "the installer recorded the Run value, the launcher stopped on the app's stop request, the Run value now "
        f"starts the stub, and {MANAGED_MACHINE} keeps delivering; no new machine")
    app.uninstall(signout=False, label="c")
    check(app.run_value() is None, "the final uninstall left the Run value")


def diagnose(work):
    say("===== DIAGNOSTICS (secrets redacted) =====")
    root = app_root()
    logs = [p for p in sorted(root.rglob("*.log")) if "webview" not in p.relative_to(root).parts]  # not WebView2's
    config = default_agent_config().parent
    logs += sorted(p for p in config.rglob("*.log") if p.is_file()) if config.is_dir() else []
    logs += sorted(config.glob("replaced-*/set-aside.json")) if config.is_dir() else []
    logs += sorted((profile() / ".raincli").rglob("migration.log")) if (profile() / ".raincli").is_dir() else []
    for path in [root / "install.json", root / "installer-record.log", *logs,
                 *sorted((default_agent_config().parent / "runtime-state").rglob("*.log")),
                 *sorted(work.glob("install-*.log")), *sorted(work.glob("uninstall-*.log")),
                 root / "app-lock" / "quit-blockers.txt", work / "old-connector.log",
                 profile() / ".raincli" / "client" / "runtime.log",
                 work / "server" / "server.log", default_agent_config().parent / "runtime.json"]:
        if path.is_file():
            say(f"--- {path} ---\n{tail(path, 6000)}")
    with contextlib.suppress(Exception):
        say("running: " + ", ".join(running_app_exes()))
    say("===== END DIAGNOSTICS =====")


def main(argv=None):
    global OLD, NEW
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--installers",
                        help=f"directory holding locally built RainCLI-Setup-{OLD}.exe and -{NEW}.exe with .sha256 files")
    source.add_argument("--real", nargs=2, metavar=("FROM", "TO"),
                        help="published releases vX.Y.Z (v0.4.0 or later) whose installer assets to use")
    parser.add_argument("--server-python", help="an existing Python with raincli/requirements.lock installed")
    args = parser.parse_args(argv)
    if args.real:
        tags = args.real
        if (not all(updates.TAG_RE.fullmatch(t) for t in tags) or updates.version_key(tags[0]) < (0, 4, 0)
                or updates.version_key(tags[0]) >= updates.version_key(tags[1])):
            parser.error("--real takes two release tags vX.Y.Z, v0.4.0 or later, oldest first")
        OLD, NEW = tags[0][1:], tags[1][1:]
    if (os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        parser.error("this e2e changes the hosts file, the trusted roots, the Run value and the user PATH; "
                     "it runs only on a disposable GitHub-hosted Actions Windows runner")
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
