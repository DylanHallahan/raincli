"""Windows app end to end, against a throwaway in-job server and a local fake release endpoint.

Runs ONLY on a disposable GitHub-hosted Actions Windows runner (it edits the runner's hosts
file, LocalMachine Root store, HKCU Run value and user PATH, and installs into its profile). It
takes two installers built from the same source by packaging/windows/build.py, as OLD and NEW, builds a
third, BROKEN, from a staging copy whose tray exits 1, and checks (protocol §15, §15.8, §15.9). The three
versions come from scripts/e2e_versions.py and are never written down: OLD is the larger of the source's
version and its MIN_SAFE_VERSION, NEW is OLD + 1 and BROKEN is OLD + 2 (patch numbers). So every pushed target
and every rollback stays at or above the minimum safe version (§18.2 item 1), and above v0.5.0, the floor for
a runtime whose connector has polled with routing=1 (§16.12 C1). For a 0.5.3 source: 0.5.3, 0.5.4, 0.5.5.

A. A fresh app install, signed in from the CLI:
   1. a silent per-user install with no admin: the layout, install.json, the HKCU Run value and
      uninstall key, the shim first on the user PATH, the Start menu entries, the H7 record;
   2. `RainCLI.exe --quit` stops the installer-started app (exit 0, nothing left running), then
      sign-in through the installed CLI's `raincli login`, with the password typed into a ConPTY
      prompt, never argv or the environment; DPAPI credential, machine-mode runtime config;
   3. launching exactly what the Run value names; presence reports OLD, automatic, current;
   4. a pushed upgrade to NEW through the installer-asset path (API asset URL, exact download
      hosts), /UPDATE changing neither the Run value nor the uninstall key, install.json swapped,
      the tray relaunched from versions\\NEW;
   5. a pushed BROKEN whose tray exits 1: the stub's probation rolls back, `rolled_back` is
      reported and NEW runs again;
   6. the downgrade to OLD refused, then allowed with --allow-downgrade;
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
   the handoff to the inbox (the local pages served only under a per-run path secret, their port bound
   exclusively), reading, replying and sending, local times in the thread, archiving the
   conversation (out of both sides' inboxes, under Archived) and unarchiving it, the window resized to
   its minimum (760x520, scaled for DPI) still showing the conversation list (v0.5.4 §19.7), a custom-scheme new
   window that opens nothing, a live update of the open thread (an answer from another machine appears and the
   trail moves to replied, with no reload), This computer and Settings
   with the last report in local time, v0.6 person routing (D6b: an inbox picked on This computer, a message to the
   person routed to the picked agent, a PR1 reply routed back to the asking agent, the teammate's view free of the
   machine and agent names; D6c: the CLI transcript of brief screenshot 14), the offline
   page and Retry (the server stopped and started), and sign-out (the machine revoked, the profile
   marked for reset). A full install afterwards restores the no-argument shortcut. Screenshots go to
   build/e2e-artifacts. Skipped with --real (the published v0.4 installers have no window).

v0.6 (protocol §21, not yet run on Windows): teammates address PEOPLE, never machines or agents. Every machine above
belongs to one person (EMAIL); a second person, the TEAMMATE, with one machine whose inbox is that machine
(inbox-only), sends the messages that parts B, B2, C, D and E deliver, after EMAIL's inbox is pointed at the machine
under test (the operator's set-inbox plus that machine's inbox-only routing). The observer machine still writes to
its own owner (own traffic, "From your agents"), and the window's reply to it goes back to the observer.

B2. (v0.6, C10.1) a pip v0.5.1 client from its release archive, as a foreground connector: it receives a message
   to its owner on both C3.1 paths (inbox mode, then the named path), sends to a person by email with --from-agent,
   and a PR1 reply (in_reply_to, no to) to that message is routed to (its machine, that agent) and acked by it.

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

G. Group chats (protocol §22; brief "RainCLI group chats", screenshots 1-4 of the Windows window), on a fresh
   install of this ref: the brief's screenshot seed in its own team (Maya, whose machine is the window's, signed in
   from the CLI; Jon and Priya on groups clients; Sam with no inbox; Ryan on an old client; "checkout" with Maya,
   Jon, Priya and Ryan, "infra" with Sam and Maya), made with the operator commands and the real API: Maya's post
   from her machine's CLI (`raincli group send`), Jon's and Priya's replies through the API after acking their
   copies. The window then opens the group at launch (D12) and is captured light and dark at 1180x780 and 760x520:
   the list with the Groups section, the read-only thread ("Delivered to 2 of 2", no composer), the member panel
   (Ryan and Sam "not taking part", no controls) and the launch. Screenshots go to build/e2e-artifacts
   (groups-win-*). ``--only groups-shots`` runs just the server and this part. Skipped with --real.

Release traffic goes to the REAL hostnames (api.github.com, github.com,
objects.githubusercontent.com, release-assets.githubusercontent.com): a hosts-file entry points
them at 127.0.0.1:443, where a fake release endpoint serves a certificate from a test root CA
added to the runner's LocalMachine Root store (§15.8 M11). The shipped client has no override, and
the BROKEN rollback build is patched only in this job's staging copy.
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
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
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
# Derived from the source (scripts/e2e_versions.py): never below MIN_SAFE_VERSION or v0.5.0 (§18.2, §16.12 C1).
_versions_spec = importlib.util.spec_from_file_location("e2e_versions", ROOT / "scripts" / "e2e_versions.py")
e2e_versions = importlib.util.module_from_spec(_versions_spec)
_versions_spec.loader.exec_module(e2e_versions)
OLD, NEW, BROKEN = e2e_versions.versions(ROOT)
OLD_PIP, OLD_MANAGED = "v0.2.0", "v0.3.2"
# v0.6, not yet run on Windows. The teammate (another person in TEAM, with one machine) and the v0.5.1 client (B2).
TEAMMATE_EMAIL, TEAMMATE_NAME, TEAMMATE = "e2e-teammate@example.invalid", "Taylor", "e2e-teammate"
OLD_V051, V051_MACHINE, V051_AGENT = "v0.5.1", "e2e-v051-machine", "e2e-v051-agent"
# The window machine's two local agents for D6b (brief screenshot 6: "laptop-agent" picked, "notes-agent" offered).
LAPTOP_AGENT, NOTES_AGENT = "laptop-agent", "notes-agent"
HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
# The default RainCLI service (login.DEFAULT_API_URL): mapped to this runner, with no certificate for it, so a
# fresh sign-in to the default (§16.20 F1) fails here and never reaches production.
PROTECTED = ("raincli.com", "www.raincli.com")
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
        line = "127.0.0.1 " + " ".join(HOSTS + PROTECTED) + f"  {HOSTS_MARK}\n"
        HOSTS_FILE.write_text(text.rstrip("\n") + "\n" + line, "utf-8")
        run(["ipconfig", "/flushdns"], check=False)
        for host in HOSTS + PROTECTED:
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

    def cli(self, *args, check=True, timeout=300, **kw):
        return run([self.root / "bin" / "raincli.exe", *args], check=check, timeout=timeout, **kw)

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


# -- v0.6 person routing (protocol §21), not yet run on Windows ------------------------------------------------

def message(server, token, message_id):
    """``GET /api/v1/messages/{id}`` as ``token`` sees it (§21 C5.2: a reader's OWN endpoint keeps
    ``{machine, agent}``; everyone else is ``{person, display_name}``), or None."""
    try:
        return api(server, token, f"/api/v1/messages/{message_id}")["message"]
    except (OSError, ValueError, KeyError):
        return None


def received(server, token, message_id):
    """v0.6, not yet run on Windows. Stricter than delivered(): the recipient machine acked it (received or later),
    not a derived or reported ``held``. For a message routed to a machine endpoint, so the sender sees the real state
    (C13 H4 hides the read state only for messages to a person with no inbox)."""
    state = (message(server, token, message_id) or {}).get("delivery_state")
    return state if state not in (None, "stored", "held") else None


def picked_up(server, sender, machine, message_id, what, timeout=240):
    """Wait until ``machine`` (its own token) has acked ``message_id``: its connector took the message, whatever the
    agent hand-over then reports (with no Herdr here that can be ``held``, which received() rightly doesn't count).
    A timeout names both views, so a wrong route is told apart from a connector that never polled."""
    try:
        return wait_for(what, lambda: (message(server, machine, message_id) or {}).get("acked_at"), timeout=timeout)
    except Failure:
        mine, theirs = message(server, machine, message_id) or {}, message(server, sender, message_id) or {}
        raise Failure(f"timed out after {timeout} s waiting for {what}: the machine sees "
                      f"{mine.get('to_endpoint')} {mine.get('delivery_state')}/{mine.get('hold_reason')}, the sender "
                      f"{theirs.get('delivery_state')}/{theirs.get('hold_reason')}") from None


def old_client_took(server, token, queue, message_id):
    """v0.6, not yet run on Windows. For a pre-0.5 connector (B): "acked" when it acked the message, "skipped
    (<problem>)" when it recorded it in its queue's skipped.json (it can't parse v0.6's "@email" sender), else None."""
    state = received(server, token, message_id)
    if state:
        return f"acked ({state})"
    try:
        recent = json.loads((queue / "skipped.json").read_text("utf-8")).get("recent") or []
    except (OSError, ValueError):
        return None
    hit = next((r for r in recent if isinstance(r, dict) and r.get("id") == message_id), None)
    return f"skipped ({hit.get('problem')})" if hit else None


def refusal(server, token, to):
    """v0.6, not yet run on Windows. ``(status, code, message)`` of a refused send to ``to``; None if it was taken."""
    try:
        send(server, token, to, "this must be refused")
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error") or {}
        except ValueError:
            error = {}
        return exc.code, error.get("code"), error.get("message", "")
    return None


def point_inbox_at(server, email, handle, token, agent="inbox", inbox_only=True):
    """v0.6, not yet run on Windows. Make ``handle`` (one of ``email``'s machines, ``token`` its credential) that
    person's inbox. Old clients (< v0.5) never report agents, so nothing can be picked live for them: the operator's
    set-inbox (no live check) names the agent, and the machine's own inbox-only routing makes messages to the person
    take the C3.1(a) machine-endpoint path, the one an old client receives. With ``inbox_only=False`` the routing is
    "all" and the pick is the named path, C3.1(b). --no-deliver-waiting: the messages waiting in EMAIL's app (and
    those sent back there when an earlier inbox machine was revoked) are not pushed to the machine under test, so
    every delivery checked below is exactly the one sent."""
    routing = api(server, token, "/api/v1/routing", {"routing": "inbox-only" if inbox_only else "all"}, method="PUT")
    check(routing.get("routing") == ("inbox-only" if inbox_only else "all"), f"{handle}'s routing is {routing}")
    server.admin("set-inbox", "--team", TEAM, "--email", email, "--handle", handle, "--agent", agent,
                 "--no-deliver-waiting")
    inbox = api(server, token, "/api/v1/me/inbox").get("inbox") or {}
    check((inbox.get("machine"), inbox.get("agent")) == (handle, agent),
          f"{email}'s inbox is {inbox}, not {agent} on {handle}")


def machine_token(config_path=None):
    """v0.6, not yet run on Windows. The credential of a machine signed in here (DPAPI, this Windows user), for
    the window machine's own API reads and sends in D6b. Registered in SECRETS before anything can print it."""
    from raincli_agent.config import load_config

    return SECRETS.add(load_config(str(config_path or default_agent_config())).token.reveal())


def publish_local_agents(server, token, names, source="hook"):
    """v0.6, not yet run on Windows. A presence report (``PUT /api/v1/presence``, §14.1) listing ``names`` as idle,
    deliverable (next-turn) agents of the machine, with no inbox role (so a pick of one is the named path, C3.1(b)).
    The e2e has no live local agent it can rely on (no Herdr, no running Claude Code or Codex session), so it
    publishes the directory entries a hook session would, as tests/conftest.py seeds its world inbox agent. The
    names stay known for 14 days (``known_agents``), so they remain pickable; they are LIVE (C13 M4, what a PR1
    reply needs) only until the machine's own runtime reports its directory again, so callers re-publish right
    before a step that needs them live and check with local_agent_live()."""
    agents = [{"key": hashlib.sha256(name.encode()).hexdigest()[:16], "name": name, "type": "claude",
               "status": "idle", "source": source, "reachability": "next-turn"} for name in names]
    return api(server, token, "/api/v1/presence", {"status": "ready", "agents": agents}, method="PUT")


def local_agent_live(server, token, name):
    """v0.6, not yet run on Windows. Whether ``GET /api/v1/me/inbox`` offers ``name`` on the caller's machine as
    live (the server's C13 M4 test: fresh presence and directory row, deliverable, not inbox-only)."""
    me = api(server, token, "/api/v1/me")["agent"]["handle"]
    return any(c.get("machine") == me and c.get("agent") == name and c.get("live") is True
               for c in api(server, token, "/api/v1/me/inbox").get("candidates") or [])


def pr1_reply(server, replier, parent_id, asker_token, asker, agent, body, local_agents, attempts=3):
    """v0.6, not yet run on Windows. C4.1/PR1: ``replier`` answers ``parent_id`` with ``in_reply_to`` and NO ``to``;
    the server routes it to (``asker``, ``agent``) when that agent is live at send time, else to the asking person's
    inbox. Its liveness is published just before (``publish_local_agents``), but the asking machine's own runtime may
    report its directory in between (the window app does every tick); a reply that fell back is not a routing
    failure, so it is retried, at most ``attempts`` times. ``local_agents`` is the asker's whole published directory
    (a report replaces it). Returns ``(reply id, the reply as the asker sees it)``."""
    seen = None
    for attempt in range(1, attempts + 1):
        publish_local_agents(server, asker_token, local_agents)
        if not local_agent_live(server, asker_token, agent):
            say(f"PR1 attempt {attempt}: {agent} on {asker} is not live yet; publishing again")
            time.sleep(2)
            continue
        reply = str(uuid.uuid4())
        api(server, replier, "/api/v1/messages", {"id": reply, "body": body, "in_reply_to": parent_id})
        seen = message(server, asker_token, reply) or {}
        if seen.get("to_endpoint") == {"machine": asker, "agent": agent}:
            return reply, seen
        say(f"PR1 attempt {attempt}: the reply went to {seen.get('to_endpoint')} (the inbox fallback: the agent "
            "was no longer live at send time); replying again")
    raise Failure(f"no PR1 reply reached {agent} on {asker} in {attempts} attempts; last {seen and seen.get('to_endpoint')}")


def leaks(texts, names):
    """v0.6, not yet run on Windows. Which of ``names`` (machine handles, agent names) appear in any of ``texts``."""
    return sorted({n for n in names for t in texts if n in t})


# -- the app window (part D) ------------------------------------------------------------------

ARTIFACTS = ROOT / "build" / "e2e-artifacts"
WINDOW_MACHINE = "e2e-window-machine"
# v0.5.5 (§20.6 M1): Inbox moves on to the latest conversation when there is one; either counts as "the inbox".
INBOX = ("app/inbox", "app/conversations/")


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


def app_window(pids):
    """The app's visible top-level WinForms window (its handle), owned by one of ``pids``."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        pid, cls, text = wintypes.DWORD(), ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(256)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        user32.GetClassNameW(hwnd, cls, 64)
        user32.GetWindowTextW(hwnd, text, 256)
        if pid.value in pids and user32.IsWindowVisible(hwnd) and cls.value.startswith("WindowsForms") and text.value:
            found.append(hwnd)
        return True
    user32.EnumWindows(each, 0)
    return found[0] if found else None


def window_rect(hwnd):
    import ctypes
    from ctypes import wintypes
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def resize_window(hwnd, width, height):
    """Ask for ``width`` x ``height`` physical pixels; WinForms clamps to the window's MinimumSize."""
    import ctypes
    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE: a maximised window ignores the size
    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, width, height, 0x0002 | 0x0004 | 0x0010)  # NOMOVE|NOZORDER|NOACTIVATE


def window_scale(hwnd):
    import ctypes
    return ctypes.windll.user32.GetDpiForWindow(hwnd) / 96


def themed_shot(page, scheme, name):
    """A capture in ``scheme``. The nav and list fade colours over --rc-dur (150 ms), so a shot taken right after
    the switch catches the old and new tokens mid-transition (review F11); wait for every animation to finish."""
    page.emulate_media(color_scheme=scheme)
    wait_for(f"the {scheme} theme to settle", lambda: page.evaluate("document.getAnimations().length") == 0,
             timeout=10, interval=0.1)
    shot(page, name)


def focus_window(hwnd):
    """Bring the app window to the foreground with real keyboard focus (the ALT press lets this process take it)."""
    import ctypes
    user32 = ctypes.windll.user32
    user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
    user32.keybd_event(0x12, 0, 2, 0)  # and up
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)


def direct_children(hwnd):
    """The window's direct child windows as ``[(class name, visible)]``: the WebView2 WinForms control is one."""
    import ctypes
    from ctypes import wintypes
    user32, found = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(child, _):
        if user32.GetParent(child) == hwnd:
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, name, 256)
            found.append((name.value, bool(user32.IsWindowVisible(child))))
        return True
    user32.EnumChildWindows(hwnd, each, 0)
    return found


def screen_pixels(hwnd, path):
    """v0.5.6: what the user sees. The window is put on top and its client area copied from the SCREEN (BitBlt of
    the desktop DC; PrintWindow can return blank for composited WebView2 content), moved first so that all of it
    is on screen (off-screen pixels read black). Saved as a 32-bit BMP; returns ``{"w", "h", "blank_rows",
    "white_rows"}``: a blank row is one colour across (a window showing nothing, white or the form's grey, is all
    blank rows; the page's nav, list and borders break every row), a white row 98% near-white."""
    import ctypes
    import struct
    from ctypes import wintypes

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    keep = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0004)  # to the top left (SWP_NOSIZE | SWP_NOZORDER)
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, keep)  # HWND_TOPMOST
    time.sleep(1.5)
    try:
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        check(w > 0 and h > 0, f"the window has no client area ({w}x{h})")
        check(origin.x >= 0 and origin.y >= 0 and origin.x + w <= sw and origin.y + h <= sh,
              f"the window's client area {w}x{h} at {origin.x},{origin.y} is not all on the {sw}x{sh} screen")
        screen = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, screen, origin.x, origin.y, 0x00CC0020)  # SRCCOPY
        info = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)
    finally:
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, keep)  # HWND_NOTOPMOST
    data = bytes(buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54))
        f.write(struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, len(data), 0, 0, 0, 0))
        f.write(data)
    stride, step, blank, white = w * 4, max(1, w // 400), 0, 0
    for row in range(h):
        line = data[row * stride:(row + 1) * stride]
        px = [(line[i], line[i + 1], line[i + 2]) for i in range(0, stride, 4 * step)]
        blank += all(abs(a - b) <= 6 for p in px for a, b in zip(p, px[0]))
        white += sum(1 for b, g, r in px if b > 235 and g > 235 and r > 235) / len(px) > 0.98
    return {"w": w, "h": h, "blank_rows": blank, "white_rows": white}


# Display scaling (brief v0.5.5 shot 13): a real per-monitor scale on the runner, set live the way Settings > Display
# does (DisplayConfigSetDeviceInfo, relative steps from the recommended scale), at 1920x1080 so 150% is allowed.
_DPI_STEPS = [100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500]


def _display_api():
    import ctypes
    from ctypes import wintypes

    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                    ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                    ("dmFields", wintypes.DWORD), ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                    ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                    ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                    ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                    ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                    ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                    ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                    ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                    ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]

    class HEADER(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]

    class GET_SCALE(ctypes.Structure):
        _fields_ = [("header", HEADER), ("minRel", ctypes.c_int32), ("curRel", ctypes.c_int32), ("maxRel", ctypes.c_int32)]

    class SET_SCALE(ctypes.Structure):
        _fields_ = [("header", HEADER), ("rel", ctypes.c_int32)]

    class PATH_SOURCE(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                    ("statusFlags", ctypes.c_uint32)]

    class PATH_TARGET(ctypes.Structure):
        _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                    ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                    ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32),
                    ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", ctypes.c_int32),
                    ("statusFlags", ctypes.c_uint32)]

    class PATH(ctypes.Structure):
        _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]

    return ctypes, DEVMODEW, GET_SCALE, SET_SCALE, PATH


def display_state():
    """``(width, height, scale percent)`` of the primary display now."""
    ctypes, DEVMODEW, GET_SCALE, _, PATH = _display_api()
    user32 = ctypes.windll.user32
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm))
    source = _display_source(ctypes, PATH)
    get = GET_SCALE()
    get.header.type, get.header.size, get.header.adapterId, get.header.id = -3, ctypes.sizeof(GET_SCALE), source.adapterId, source.id
    user32.DisplayConfigGetDeviceInfo(ctypes.byref(get))
    return dm.dmPelsWidth, dm.dmPelsHeight, _DPI_STEPS[get.curRel - get.minRel]


def _display_source(ctypes, PATH):
    user32 = ctypes.windll.user32
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    user32.GetDisplayConfigBufferSizes(2, ctypes.byref(n_paths), ctypes.byref(n_modes))
    paths = (PATH * n_paths.value)()
    modes = (ctypes.c_byte * (64 * max(1, n_modes.value)))()
    check(user32.QueryDisplayConfig(2, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes, None) == 0,
          "QueryDisplayConfig failed")
    return paths[0].sourceInfo


def set_display(width, height, percent):
    """Set the primary display's mode and its real scale (percent), live; returns the scale reached."""
    ctypes, DEVMODEW, GET_SCALE, SET_SCALE, PATH = _display_api()
    user32 = ctypes.windll.user32
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm))
    if (dm.dmPelsWidth, dm.dmPelsHeight) != (width, height):
        dm.dmPelsWidth, dm.dmPelsHeight, dm.dmFields = width, height, 0x80000 | 0x100000
        check(user32.ChangeDisplaySettingsExW(None, ctypes.byref(dm), None, 0, None) == 0,
              f"the display would not change to {width}x{height}")
    source = _display_source(ctypes, PATH)
    get = GET_SCALE()
    get.header.type, get.header.size, get.header.adapterId, get.header.id = -3, ctypes.sizeof(GET_SCALE), source.adapterId, source.id
    user32.DisplayConfigGetDeviceInfo(ctypes.byref(get))
    rel = _DPI_STEPS.index(percent) + get.minRel  # steps are relative to the recommended scale (minRel = -its index)
    check(get.minRel <= rel <= get.maxRel, f"{percent}% is not allowed at {width}x{height}")
    put = SET_SCALE()
    put.header.type, put.header.size, put.header.adapterId, put.header.id = -4, ctypes.sizeof(SET_SCALE), source.adapterId, source.id
    put.rel = rel
    check(user32.DisplayConfigSetDeviceInfo(ctypes.byref(put)) == 0, f"the display scale would not change to {percent}%")
    return display_state()[2]


def cdp_evaluate(port, url_fragment, expression, timeout=30):
    """One Runtime.evaluate on the page whose URL contains ``url_fragment``, over a bare DevTools WebSocket (stdlib
    only). Unlike a Playwright attachment, it turns on no emulation: Playwright enables focus emulation on every
    page it attaches to, which makes document.hasFocus() true (and lets the page read what it shows)."""
    import base64 as b64
    import struct
    deadline = time.monotonic() + timeout
    target = None
    while time.monotonic() < deadline and target is None:
        with contextlib.suppress(OSError, ValueError):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                target = next((t for t in json.loads(response.read())
                               if t.get("type") == "page" and url_fragment in (t.get("url") or "")), None)
        if target is None:
            time.sleep(0.5)
    check(target is not None, f"no page showing {url_fragment} on the DevTools port")
    ws = urlsplit(target["webSocketDebuggerUrl"])
    sock = socket.create_connection((ws.hostname, ws.port), timeout=timeout)
    try:
        key = b64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {ws.path} HTTP/1.1\r\nHost: {ws.hostname}:{ws.port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        check(b" 101 " in head.split(b"\r\n", 1)[0], f"the DevTools WebSocket refused: {head[:80]!r}")
        rest = head.split(b"\r\n\r\n", 1)[1]
        payload = json.dumps({"id": 1, "method": "Runtime.evaluate",
                              "params": {"expression": expression, "returnByValue": True}}).encode()
        mask = os.urandom(4)
        frame = bytearray([0x81])
        frame += bytes([0x80 | 126]) + struct.pack(">H", len(payload)) if len(payload) >= 126 else bytes([0x80 | len(payload)])
        frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(frame))
        buffer = rest
        while True:
            while len(buffer) < 2:
                buffer += sock.recv(65536)
            length, offset = buffer[1] & 0x7F, 2
            if length == 126:
                while len(buffer) < 4:
                    buffer += sock.recv(65536)
                length, offset = struct.unpack(">H", buffer[2:4])[0], 4
            elif length == 127:
                while len(buffer) < 10:
                    buffer += sock.recv(65536)
                length, offset = struct.unpack(">Q", buffer[2:10])[0], 10
            while len(buffer) < offset + length:
                buffer += sock.recv(65536)
            message, buffer = json.loads(buffer[offset:offset + length]), buffer[offset + length:]
            if message.get("id") == 1:
                return message["result"]["result"].get("value")
    finally:
        with contextlib.suppress(OSError):  # a clean close (a masked, empty close frame), so the session ends
            sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
            sock.settimeout(2)
            sock.recv(64)
        sock.close()


def list_fits(page):
    """The §19.7 check: the conversation list shows, inside the window, with its times and counts inside it."""
    return page.evaluate("""() => {
        const list = document.querySelector('section.rc-list');
        if (!list || getComputedStyle(list).display === 'none') return 'no list';
        const box = list.getBoundingClientRect();
        if (box.width < 200 || box.right > innerWidth) return `list ${box.left}-${box.right} of ${innerWidth}`;
        for (const el of document.querySelectorAll('.rc-conv-time, .rc-count')) {
            const b = el.getBoundingClientRect();
            if (b.right > box.right + 0.5) return `cut off: ${el.className} at ${b.right} > ${box.right}`;
        }
        if (document.documentElement.scrollWidth > innerWidth) return 'horizontal scroll';
        return '';
    }""")


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

    @staticmethod
    def _matches(fragment, url):
        """``fragment`` is a substring of the URL, or a tuple of alternatives (any one)."""
        return any(f in url for f in (fragment if isinstance(fragment, tuple) else (fragment,)))

    def _find(self, fragment):
        for context in self.browser.contexts:
            for candidate in context.pages:
                if not candidate.is_closed() and self._matches(fragment, url_of(candidate)):
                    return candidate
        return None

    def page(self, fragment, timeout=120):
        def found():
            if not any(kind == "page" and self._matches(fragment, url) for kind, url in self.targets()):
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


def _redact_local(url):
    """A local page URL with its path secret removed, for messages."""
    return re.sub(r"(http://127\.0\.0\.1:\d+/)[A-Za-z0-9_-]{16,}/", r"\1[secret]/", url or "")


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


def pick_person(page, name, email):
    """v0.6, not yet run on Windows. New message's To addresses a PERSON (§21 C7.2). The v0.6 web half turns the
    page's people <select id=to> into a combobox (app-picker.js: an <input id=to role=combobox>, options in
    #to-options, picked on mousedown, shown as a chip). Without that script the <select> is used by label; a
    server without the v0.6 web half still has the v0.5.5 text field, which takes the email (C2.1 string form)."""
    page.wait_for_load_state("load")  # app-picker.js (deferred) has replaced the <select> by then, if it will
    to = page.locator("#to")
    to.wait_for(state="attached", timeout=30000)
    if page.locator("select#to").count():
        to.select_option(label=name)
    elif (to.get_attribute("role") or "") == "combobox":
        to.click()
        to.fill(name)
        option = page.locator("#to-options .rc-picker-option", has_text=name).first
        option.wait_for(state="visible", timeout=30000)
        option.click()
        page.wait_for_selector(".rc-chip-picked:not([hidden])", timeout=30000)
        check(name in page.inner_text(".rc-chip-picked"), f"the picker's chip says {page.inner_text('.rc-chip-picked')!r}")
    else:
        to.fill(email)


def part_d(app, server, installers, password, observer, teammate, menu, work):
    """The app window over CDP; see D in the module docstring. ``teammate`` is the teammate machine's token (v0.6)."""
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
        # D4b. v0.5.3 (§18.2 item 2): the local pages are served by the app's own server, bound exclusively, and
        # only under this run's path secret.
        local = re.match(r"http://127\.0\.0\.1:(\d+)/([A-Za-z0-9_-]{32,})/sign-in\.html", url_of(page))
        check(local, f"the local page URL has no per-run path secret: {_redact_local(url_of(page))}")
        local_port, local_secret = int(local.group(1)), local.group(2)
        for path in ("/sign-in.html", "/" + "x" * len(local_secret) + "/sign-in.html"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{local_port}{path}", timeout=5)
                check(False, f"the local server answered {path} without the secret")
            except urllib.error.HTTPError as exc:
                check(exc.code == 404, f"the local server answered {path} with {exc.code}")
        with urllib.request.urlopen(f"http://127.0.0.1:{local_port}/{local_secret}/sign-in.html", timeout=5) as r:
            check(r.headers.get("Referrer-Policy") == "no-referrer", "a local page lacks Referrer-Policy: no-referrer")
        squatter = socket.socket()
        try:
            squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            squatter.bind(("127.0.0.1", local_port))
            check(False, "a second socket could bind the local page port")
        except OSError:
            pass
        finally:
            squatter.close()
        say("PASS: D4b. the local pages are served only under a per-run path secret (404 without it, "
            "Referrer-Policy no-referrer), and a second bind of their port, even with SO_REUSEADDR, is refused")
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
        # v0.6, not yet run on Windows: the observer is the owner's own machine, so its message to the owner is own
        # traffic (C13 H2 (i)) in the window's "From your agents" conversation, found by its id (not "the first row").
        # The window's REPLY goes back to the observer (H2 (ii)); the NEW message addresses a person, the teammate
        # (C2: a machine handle is refused), through the people picker, and reaches the teammate's inbox machine.
        hello = send(server, observer, {"person": EMAIL}, "Hello from the observer machine")
        own_conversation = (message(server, observer, hello) or {}).get("conversation_id")
        check(own_conversation, "the observer cannot read its own message to its owner")
        page.reload()
        page.click(f".rc-conv[href*='{own_conversation}']")
        page.wait_for_selector(".rc-thread")
        check("Hello from the observer machine" in page.inner_text(".rc-thread"), "the thread is missing the message")
        shot(page, "d5-thread")
        page.click(".rc-msg a:has-text('Reply')")
        page.fill("#body", "A reply from the app window")
        page.click(".rc-compose button[type=submit]")  # the thread header also has Archive
        # v0.5.5 R2 (§20.2): a send in a conversation stays in place; the message appears without a navigation.
        page.wait_for_selector(".rc-thread .rc-msg:has-text('A reply from the app window')", timeout=60000)
        wait_for("the observer to receive the window's reply (own traffic, back to the asking machine)",
                 lambda: "A reply from the app window" in inbox_bodies(server, observer), timeout=120)
        page.click(".rc-new")
        pick_person(page, TEAMMATE_NAME, TEAMMATE_EMAIL)  # v0.6: a person, never OBSERVER or any other handle
        page.fill("#body", "A new message from the app window")
        page.click(".rc-compose button[type=submit]")
        # New message still posts the form; app mode redirects to the conversation, with no banner (§20.2).
        page.wait_for_url("**/app/conversations/**", timeout=60000)
        page.wait_for_selector(".rc-thread .rc-msg:has-text('A new message from the app window')", timeout=60000)
        wait_for("the teammate's inbox machine to receive the new message",
                 lambda: "A new message from the app window" in inbox_bodies(server, teammate), timeout=120)
        shot(page, "d5-sent")
        say("PASS: D5. the window read the observer's message (its own machine, From your agents) and replied, and "
            f"the reply reached the observer; a new message to the person {TEAMMATE_NAME} (picked by name) reached "
            "the teammate's inbox machine")

        # D5b. v0.5.2 (§17.1, §17.2): times in the window are local, never the server's UTC text; Archive moves
        # the conversation out of the inbox for both sides and under Archived; Unarchive brings it back.
        # v0.6, not yet run on Windows: the conversation is the person pair (EMAIL, the teammate), so "the other side"
        # is the teammate's machine (C13 M1: a machine reads its owner's conversations); archived_by is a person.
        match = re.search(r"/app/conversations/([0-9a-f-]{36})", url_of(page))
        check(match, f"D5 did not end on the conversation: {url_of(page)}")
        conversation = match.group(1)
        check(conversation != own_conversation, "the message to the teammate landed in the From your agents conversation")
        stamp = page.locator(".rc-thread time[datetime]").first
        page.wait_for_selector(".rc-thread time[data-local]", state="attached", timeout=30000)
        label, title = stamp.inner_text(), stamp.get_attribute("title") or ""
        check(label and not label.endswith(" UTC") and title, f"localtime.js did not format the thread: {label!r}")
        page.click(".rc-archive button:has-text('Archive')")
        page.wait_for_url("**/app/inbox?notice=archived", timeout=60000)
        check(page.locator(f".rc-conv[href*='{conversation}']").count() == 0, "the archived conversation is in the inbox")

        def peer_list(archived):
            rows = api(server, teammate, f"/api/v1/conversations?archived={archived}")["conversations"]
            return {row["id"]: row for row in rows}
        seen = peer_list("only").get(conversation)
        check(seen and seen["archived"] and (seen.get("archived_by") or {}).get("person") == EMAIL,
              f"the teammate does not see it archived by the person: {seen}")
        check(conversation not in peer_list("exclude"), "the teammate's main list still has the archived conversation")
        page.click("nav.rc-nav a:has-text('Archived')")
        page.wait_for_url("**/app/archived", timeout=60000)
        row = page.locator(".rc-archived-row", has_text="Archived by you")
        check(row.count() == 1 and conversation in (row.locator("a").first.get_attribute("href") or ""),
              "the conversation is not under Archived")
        shot(page, "d5b-archived")
        row.locator("button:has-text('Unarchive')").click()
        page.wait_for_url("**/app/archived?notice=unarchived", timeout=60000)
        check(page.locator(".rc-archived-row").count() == 0, "Unarchive left it under Archived")
        page.click("nav.rc-nav a:has-text('Inbox')")
        page.wait_for_url(f"**/app/conversations/{conversation}*", timeout=60000)  # §20.6 M1
        page.wait_for_selector(f".rc-conv[href*='{conversation}']", timeout=60000)
        check(not peer_list("include")[conversation]["archived"], "the teammate still sees it archived")
        say(f"PASS: D5b. the thread shows local times ({label!r}, title {title!r}); Archive moved the conversation "
            "out of both sides' inboxes and under Archived (archived by you); Unarchive brought it back")

        # D5d. v0.5.4 (§19.7): at the window's minimum size (760x520 logical, scaled for DPI) the conversation
        # list still shows, on the inbox and in a conversation. Light and dark at both sizes go to the artifacts.
        hwnd = wait_for("the app window", lambda: app_window(app_pids(app.root)), timeout=60, interval=1)
        scale = window_scale(hwnd)
        restore = window_rect(hwnd)
        resize_window(hwnd, 300, 200)
        min_size = (round(760 * scale), round(520 * scale))
        wait_for("the window to clamp at its minimum size",
                 lambda: abs(window_rect(hwnd)[2] - min_size[0]) <= 2 and abs(window_rect(hwnd)[3] - min_size[1]) <= 2,
                 timeout=30, interval=0.5)
        page.click("nav.rc-nav a:has-text('Inbox')")
        page.wait_for_url(f"**/app/conversations/{conversation}*", timeout=60000)  # §20.6 M1
        page.wait_for_selector(f".rc-conv[href*='{conversation}']", timeout=60000)
        wait_for("the page to see the small window", lambda: page.evaluate("innerWidth") < 760, timeout=30, interval=0.5)
        small_width = page.evaluate("innerWidth")
        problem = list_fits(page)
        check(not problem, f"at the minimum window size the inbox list does not fit: {problem}")
        for scheme in ("light", "dark"):
            themed_shot(page, scheme, f"after-win-inbox-{scheme}-760x520")
        page.click(f".rc-conv[href*='{conversation}']")
        page.wait_for_selector(".rc-thread")
        problem = list_fits(page)
        check(not problem, f"at the minimum window size the conversation page list does not fit: {problem}")
        for scheme in ("light", "dark"):
            themed_shot(page, scheme, f"after-win-conversation-{scheme}-760x520")
        resize_window(hwnd, round(1180 * scale), round(780 * scale))
        wait_for("the page to see the default window", lambda: page.evaluate("innerWidth") > 1000, timeout=30, interval=0.5)
        check(not list_fits(page), "at the default window size the list does not fit")
        for scheme in ("light", "dark"):
            themed_shot(page, scheme, f"after-win-conversation-{scheme}-1180x780")
        page.click("nav.rc-nav a:has-text('Inbox')")
        page.wait_for_url(f"**/app/conversations/{conversation}*", timeout=60000)  # §20.6 M1
        page.wait_for_selector(f".rc-conv[href*='{conversation}']", timeout=60000)
        for scheme in ("light", "dark"):
            themed_shot(page, scheme, f"after-win-inbox-{scheme}-1180x780")
        page.emulate_media(color_scheme="null")  # back to the system theme
        resize_window(hwnd, restore[2], restore[3])
        say(f"PASS: D5d. at the minimum window size ({min_size[0]}x{min_size[1]} physical px at scale {scale:g}, "
            f"page width {small_width} px) the conversation list shows on the inbox and in a conversation, "
            "with times and counts inside it")

        # D5c. v0.5.3 (§18.2 item 3): a new-window link with another scheme opens nothing: no window, no program.
        before = [url for kind, url in window.targets() if kind == "page"]
        page.evaluate("""() => { const a = document.createElement("a"); a.id = "rc-e2e-link";
            a.href = "rc-e2e-scheme://open"; a.target = "_blank"; a.textContent = "x";
            document.body.appendChild(a); }""")
        page.click("#rc-e2e-link", no_wait_after=True)
        page.evaluate("() => { window.open('rc-e2e-scheme://again', '_blank'); }")
        time.sleep(3)
        after = [url for kind, url in window.targets() if kind == "page"]
        check(len(after) == len(before) and not any("rc-e2e-scheme" in url for url in after),
              f"a new window opened: {after}")
        app_log = (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
        check("link dropped (scheme 'rc-e2e-scheme')" in app_log, "the dropped link was not logged")
        say("PASS: D5c. target=_blank and window.open with a custom scheme opened no window and nothing else; "
            "the drop was logged by scheme only")

        # D5e. v0.5.4 (§19.1, §19.7): the open thread updates on its own. The window sends a message; the other
        # side's machine acknowledges and answers it; without a reload the answer appears and the trail moves to replied.
        # v0.6, not yet run on Windows: the other side is the teammate's inbox machine (the conversation is the person
        # pair); its answer is a reply with in_reply_to and no "to" (C4.3: to the window's person, EMAIL, who has no
        # inbox yet, so it waits in the app, which is this window).
        page.goto(server.url + f"/app/conversations/{conversation}#latest")
        page.wait_for_selector(".rc-compose textarea#body", timeout=60000)
        page.fill("#body", "Live from the window")
        page.click(".rc-compose button[type=submit]")
        page.wait_for_selector(".rc-thread .rc-msg:has-text('Live from the window')", timeout=60000)  # in place (§20.2)
        page.mouse.move(2, 2)  # no hover state in the capture
        shot(page, "after-win-4-send-in-place-light-1180x780")  # brief v0.5.5 shot 4: stored, no banner, no focus box
        mine = wait_for("the window's message in the teammate's inbox", lambda: next(
            (m for m in api(server, teammate, "/api/v1/inbox?routing=1&wait=0&limit=100")["messages"]
             if m["body"] == "Live from the window"),
            None), timeout=120)
        page.evaluate("() => { window.__rcNoReload = 'kept'; }")  # gone if the page reloads
        api(server, teammate, f"/api/v1/messages/{mine['id']}/ack", {}, method="POST")
        answer = str(uuid.uuid4())
        api(server, teammate, "/api/v1/messages", {"id": answer, "body": "Live answer", "in_reply_to": mine["id"]})
        wait_for("the answer in the open window", lambda: page.locator(f"#m-{answer}").count() == 1,
                 timeout=60, interval=1)
        trail = page.locator(f'[data-trail="{mine["id"]}"]')
        wait_for("the trail at replied", lambda: trail.count() == 1 and trail.get_attribute(
            "data-state", timeout=2000) == "replied", timeout=60, interval=1)
        check(page.evaluate("() => window.__rcNoReload") == "kept", "the window reloaded to show the update")
        shot(page, "d5e-live-replied")
        say("PASS: D5e. the teammate machine's answer (a reply with no to) appeared in the open window without a "
            "reload, and the window's own message moved to replied")

        # D5f. v0.5.5 R3 (§20.4, brief shot 6): keyboard focus on a message shows one ring in --rc-focus that follows the
        # message's radius; after a mouse click no message has an outline.
        rings = """() => [...document.querySelectorAll('.rc-msg')].filter(m => getComputedStyle(m).outlineStyle !== 'none')
            .map(m => [getComputedStyle(m).outlineStyle, getComputedStyle(m).borderTopLeftRadius])"""
        page.locator(".rc-msg .rc-msg-content").first.click()
        check(page.evaluate(rings) == [], f"a mouse click left an outline on a message: {page.evaluate(rings)}")
        page.mouse.move(2, 2)
        shot(page, "after-win-6-focus-click-light-1180x780")
        for _ in range(80):
            page.keyboard.press("Tab")
            if page.evaluate("!!document.activeElement.closest('.rc-msg')"):
                break
        ringed = page.evaluate(rings)
        check(ringed == [["solid", "10px"]], f"Tab into a message: expected one rounded ring, got {ringed}")
        shot(page, "after-win-6-focus-tab-light-1180x780")
        say("PASS: D5f. Tab into a message shows one rounded ring (10px, the message radius); a click shows none")

        # D5h. v0.5.5 (§20.3, §20.6 L6, §20.7): a hidden start reads nothing, and launch opens the latest conversation. The
        # teammate writes; the app restarts hidden (--background, as at logon), so the hosted page loads unseen: no
        # focus, nothing acknowledged after 10 s (and, while hidden, no polling). Shown and focused, the window is on
        # that conversation, selected in the list, and only then reads the message.
        # v0.6, not yet run on Windows: the writer is the teammate (not the observer, whose messages to its owner land
        # in From your agents, not in `conversation`). EMAIL has no inbox yet, so the message waits in the app (C3.2).
        # Its read state is read with the OBSERVER's credential (EMAIL's own machine: the recipient person's view);
        # the teammate, as sender, never sees it (C13 H4: no read receipt for a message to a person).
        # The app quits first: a message sent while the previous window is still open and focused would be read
        # there (its poll), before the hidden start this step is about.
        window.close()
        app.quit()
        def wait_cold(label):
            """A start as at logon (review recheck R1): the window machine's presence is offline (quitting publishes an
            offline report), so P2 can only come from the window's own User-Agent version (§20.7)."""
            def offline():
                row = entry(server, observer, WINDOW_MACHINE) or {}
                return (row.get("presence") or {}).get("status") in ("offline", "unknown")
            wait_for(f"the window machine's presence to be offline ({label})", offline, timeout=240, interval=5)

        wait_cold("before the hidden start")
        # v0.5.6: the window must fit on screen for the pixel check below (the runner starts at 1024x768).
        d5h_display = display_state()
        set_display(1920, 1080, 100)
        unseen = send(server, teammate, {"person": EMAIL}, "Read me only when you can see me")  # v0.6
        launched = time.strftime("%H:%M:%S", time.gmtime())
        port = free_port()
        env = dict(os.environ, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={port}")
        subprocess.Popen([str(app.root / "RainCLI.exe"), "--background"], env=env, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        wait_for("the hidden window's CDP endpoint", lambda: devtools_answer(f"127.0.0.1:{port}"), timeout=180)
        # While hidden, nothing attaches with Playwright (its focus emulation would make the page focused): the
        # DevTools target list and one bare Runtime.evaluate only.
        focus, visibility = cdp_evaluate(port, f"/app/conversations/{conversation}",
                                         "[document.hasFocus(), document.visibilityState]", timeout=180)
        shown = app_window(app_pids(app.root)) is not None
        time.sleep(10)
        seen = api(server, observer, f"/api/v1/messages/{unseen}")["message"]
        state = seen["delivery_state"]
        log_lines = [line.strip() for line in (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
                     .splitlines() if "page visibility" in line][-3:]
        say(f"D5h evidence: native window visible={shown}; page hasFocus={focus}, visibilityState={visibility}; "
            f"after 10 s the message is {state} (acked_at {seen.get('acked_at')}; launched {launched} UTC); "
            f"app log: {log_lines}")
        check(not shown, "--background showed the window")
        check(state == "stored", f"the hidden window acknowledged a message: {state}")
        check(focus is False and visibility == "hidden", f"the hidden window's page: hasFocus={focus}, {visibility}")
        before_show = sorted(pid for pid, _ in webview_processes(app.root))
        app.stub("--open")  # the running app's show request
        hwnd = wait_for("the shown app window", lambda: app_window(app_pids(app.root)), timeout=60, interval=1)
        focus_window(hwnd)
        try:
            wait_for("the shown, focused window to read the message",
                     lambda: api(server, observer, f"/api/v1/messages/{unseen}")["message"]["delivery_state"] != "stored",
                     timeout=60, interval=1)
        except Failure as exc:
            after = cdp_evaluate(port, f"/app/conversations/{conversation}",
                                 "[document.hasFocus(), document.visibilityState]", timeout=30)
            lines = [line.strip() for line in (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
                     .splitlines() if "page visibility" in line][-3:]
            raise Failure(f"{exc}; after show: page hasFocus={after[0]}, visibilityState={after[1]}; "
                          f"window visible={app_window(app_pids(app.root)) is not None}; app log: {lines}") from None
        after_show = sorted(pid for pid, _ in webview_processes(app.root))
        failed = [line.strip() for line in (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
                  .splitlines() if "webview2 process failed" in line]
        say(f"D5h: shown and focused, the window read the message; WebView2 processes {before_show} -> {after_show}; "
            f"DevTools answers: {devtools_answer(f'127.0.0.1:{port}')}; process failures logged: {failed}")
        # v0.5.6, the white window: the page said visible and focused, but v0.5.5 left the WebView2 WinForms control
        # hidden after a hidden start, so the window showed nothing but white. Checked natively (the control's own
        # window) and in real pixels, after the hidden start's show and after a close to the tray and a reopen.
        def painted(hwnd, label):
            time.sleep(3)
            children = direct_children(hwnd)
            pixels = screen_pixels(hwnd, ARTIFACTS / f"d5h-{label}-screen.bmp")
            lines = [line.strip() for line in (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace")
                     .splitlines() if "page visibility" in line][-2:]
            say(f"D5h {label}: child windows {children}; screen pixels {pixels['blank_rows']}/{pixels['h']} blank rows "
                f"({pixels['white_rows']} white); "
                f"app log: {lines}")
            problems = []  # both checked, so each one is seen to catch the white window on its own
            if pixels["blank_rows"] >= 0.9 * pixels["h"]:
                problems.append(f"the window shows nothing ({pixels['blank_rows']} of {pixels['h']} rows one colour, "
                                f"{pixels['white_rows']} white)")
            if not (children and all(visible for _, visible in children)):
                problems.append(f"the WebView2 control is not visible (child windows {children})")
            check(not problems, f"{label}: " + "; ".join(problems))
        painted(hwnd, "hidden-start-shown")
        import ctypes
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: closed to the tray
        wait_for("the window to close to the tray", lambda: app_window(app_pids(app.root)) is None, timeout=30, interval=1)
        app.stub("--open")
        hwnd = wait_for("the reopened app window", lambda: app_window(app_pids(app.root)), timeout=60, interval=1)
        focus_window(hwnd)
        painted(hwnd, "tray-reopen")
        set_display(d5h_display[0], d5h_display[1], d5h_display[2])
        say("PASS: D5h. after a hidden start then show, and after a close to the tray and a reopen, the WebView2 control "
            "is visible and the window shows the page (screen pixels, not blank)")
        # The relaunch below is a real launch (--open) that must open the latest conversation; a fresh CDP port
        # for the captures, independent of the hidden start's DevTools session.
        app.quit()
        wait_cold("before --open")
        unseen_open = send(server, teammate, {"person": EMAIL}, "Read me when the window opens")  # v0.6, as above
        window = WindowCDP(pw, open_window_cdp(app))
        page = window.page(INBOX, timeout=180)
        page.wait_for_url(f"**/app/conversations/{conversation}*", timeout=60000)
        wait_for("the cold --open window to read the latest conversation",
                 lambda: api(server, observer, f"/api/v1/messages/{unseen_open}")["message"]["delivery_state"] != "stored",
                 timeout=60, interval=1)
        check(conversation in url_of(page), f"launch did not open the latest conversation: {url_of(page)}")
        current = page.locator(".rc-conv[aria-current=page]")
        check(current.count() == 1 and conversation in (current.get_attribute("href") or ""),
              "the latest conversation is not selected in the list")
        for scheme in ("light", "dark"):
            themed_shot(page, scheme, f"after-win-10-opened-latest-{scheme}-1180x780")
        page.emulate_media(color_scheme="null")
        say(f"PASS: D5h. from starts with the presence offline (as at logon): started hidden ({visibility}, no focus), the latest "
            "conversation was open and nothing was read after 10 s; shown and focused, it read the message; a cold "
            "--open opened the latest conversation, selected, and read it")

        # D5i. v0.5.5 (brief shot 13): one pass at a REAL 150% display scale (1920x1080, the per-monitor scale set
        # live as Settings does). The app is restarted so its window is created at 150%: pywebview scales the window
        # and its minimum; the page sees devicePixelRatio 1.5 at the same CSS sizes. Launch opens the latest
        # conversation, the list fits at the default and the minimum size. Then 100% and the old mode come back.
        window.close()
        app.quit()
        before = display_state()
        reached = set_display(1920, 1080, 150)
        check(reached == 150, f"the display scale is {reached}%, not 150%")
        try:
            window = WindowCDP(pw, open_window_cdp(app))
            page = window.page(INBOX, timeout=180)
            page.wait_for_url(f"**/app/conversations/{conversation}*", timeout=60000)
            hwnd = wait_for("the app window at 150%", lambda: app_window(app_pids(app.root)), timeout=60, interval=1)
            check(abs(window_scale(hwnd) - 1.5) < 0.01, f"the window's scale is {window_scale(hwnd)}")
            check(page.evaluate("devicePixelRatio") == 1.5, "the page is not at devicePixelRatio 1.5")
            problem = list_fits(page)
            check(not problem, f"at 150% the list does not fit: {problem}")
            for scheme in ("light", "dark"):
                themed_shot(page, scheme, f"after-win-13-opened-latest-{scheme}-1180x780-150pct")
            resize_window(hwnd, 300, 200)
            sizes = []

            def settled():  # the same size twice in a row: the window has stopped at its minimum
                sizes.append(window_rect(hwnd)[2:])
                return len(sizes) > 1 and sizes[-1] == sizes[-2]
            wait_for("the 150% window to settle at its minimum size", settled, timeout=30, interval=0.5)
            clamped = sizes[-1]
            check(clamped[0] >= 1140 - 2 and clamped[1] >= 780 - 2,
                  f"at 150% the window went below its minimum (760x520 logical = 1140x780): {clamped}")
            wait_for("the page to see the small window", lambda: page.evaluate("innerWidth") < 760, timeout=30, interval=0.5)
            problem = list_fits(page)
            check(not problem, f"at 150% and the minimum size the list does not fit: {problem}")
            small = page.evaluate("[innerWidth, innerHeight]")
            for scheme in ("light", "dark"):
                themed_shot(page, scheme, f"after-win-13-minimum-{scheme}-760x520-150pct")
            page.emulate_media(color_scheme="null")
        finally:
            with contextlib.suppress(Exception):
                window.close()
            app.quit(required=False)
            set_display(before[0], before[1], before[2])
        window = WindowCDP(pw, open_window_cdp(app))
        page = window.page(INBOX, timeout=180)
        say(f"PASS: D5i. at a real 150% display scale (1920x1080, window scale 1.5, devicePixelRatio 1.5) launch "
            f"opened the latest conversation and the list fits, at the default size and the minimum (window "
            f"{clamped[0]}x{clamped[1]} physical, page {small[0]}x{small[1]} CSS px); the display is back at "
            f"{before[0]}x{before[1]}, {before[2]}%")

        # D6. This computer and Settings (the /app/local sentinel shows the bundled pages). The app cancels the
        # hosted navigation and loads its own page, so the click must not wait for that navigation.
        page.click("nav.rc-nav a:has-text('This computer')", no_wait_after=True)  # the app cancels it (sentinel)
        page = window.page("this-computer.html", timeout=60)  # re-attached after the swap
        wait_for("This computer to show the machine", lambda: page.inner_text("#machine") == WINDOW_MACHINE,
                 timeout=60, interval=1)
        report = page.locator("time#last-report")
        if report.get_attribute("datetime"):  # the runtime has reported: shown by localtime.js, not as UTC
            check(report.get_attribute("data-local") == "1" and not report.inner_text().endswith("UTC"),
                  f"This computer's last report is not in local time: {report.inner_text()!r}")
        last_report = report.inner_text()
        shot(page, "d6-this-computer")
        page.click("nav.rc-nav button[data-open=settings]", no_wait_after=True)  # the app navigates, not the click
        page = window.page("settings.html", timeout=60)  # re-attached after the swap
        page.wait_for_selector("#routing-all:checked", timeout=60000)
        shot(page, "d6-settings")
        say(f"PASS: D6. This computer shows the machine (last report {last_report!r}); Settings shows the routing "
            "policy")

        # D6b. v0.6 person routing (§21 C11.4, C8, C9.1, C3, C4, C5; brief "Windows e2e" and screenshot 6).
        # v0.6, NOT YET RUN ON WINDOWS. The live local agents: the e2e has no Herdr and no running Claude Code or
        # Codex session, so the window machine's directory is published with its own credential (two idle, next-turn
        # hook entries, no inbox role: publish_local_agents), as tests/conftest.py seeds its world. The pick itself is
        # made in the real window ("Use as my inbox"); the server accepts it because the names are known (C1.1).
        # A fresh app sign-in is MACHINE MODE (runtime.json machine_config, no connector): nothing on the window machine
        # polls its server queue, so a message routed to (window machine, agent) is checked as ROUTED there (the
        # machine's own view of it keeps {machine, agent}, C5.2), not as acked; the sender's state is logged only.
        window_token = machine_token()
        window_agents = [LAPTOP_AGENT, NOTES_AGENT]
        publish_local_agents(server, window_token, window_agents)
        # (a0) A message to EMAIL before any inbox: held no_inbox for its sender (C6.1), and the card's waiting tick
        # (C6.2). The window shows its local pages now, so the hosted page cannot read it in the meantime.
        waiting = send(server, teammate, {"person": EMAIL}, "Waiting for an inbox to be picked")
        held = message(server, teammate, waiting) or {}
        check((held.get("delivery_state"), held.get("hold_reason")) == ("held", "no_inbox"),
              f"the teammate sees the waiting message as {held.get('delivery_state')}/{held.get('hold_reason')}")
        # (a) This computer's "Your inbox" card: No inbox yet, both agents offered, the waiting tick on by default.
        page.click("nav.rc-nav button[data-open=this-computer]", no_wait_after=True)  # the app navigates
        page = window.page("this-computer.html", timeout=60)  # re-attached after the swap
        wait_for("This computer's inbox card", lambda: page.inner_text("#inbox-status").startswith("No inbox yet"),
                 timeout=60, interval=1)
        laptop = page.locator(f"#inbox-candidates li[data-inbox-agent='{LAPTOP_AGENT}']")
        notes = page.locator(f"#inbox-candidates li[data-inbox-agent='{NOTES_AGENT}']")
        wait_for("both local agents offered", lambda: laptop.count() == 1 and notes.count() == 1, timeout=60, interval=1)
        check(page.is_visible("#inbox-waiting-row") and page.is_checked("#inbox-waiting"),
              "the waiting messages' tick is not shown, or not ticked by default")
        waiting_label = page.inner_text("#inbox-waiting-label")
        check(waiting_label.startswith("Also deliver the"), f"the waiting tick says {waiting_label!r}")
        shot(page, "d6b-this-computer-no-inbox")
        laptop.locator("button").click(no_wait_after=True)
        expected = f"Your inbox: {LAPTOP_AGENT} on this computer"
        wait_for("the inbox status line", lambda: page.inner_text("#inbox-status") == expected, timeout=60, interval=1)
        said = page.inner_text("#inbox-message")
        check(said.startswith(f"Your inbox is now {LAPTOP_AGENT} on this computer."), f"after the pick: {said!r}")
        page.click("#inbox-change")  # Change: the picker again, the pick shown as Your inbox, the other offered
        wait_for("the picker after Change", lambda: page.is_visible("#inbox-picker"), timeout=30, interval=0.5)
        check(laptop.locator(".pill").count() == 1 and laptop.locator(".pill").inner_text() == "Your inbox",
              f"the picked agent's row says {laptop.inner_text()!r}")
        check(notes.locator("button").inner_text() == "Use as my inbox", f"the other agent's row says {notes.inner_text()!r}")
        page.mouse.move(2, 2)
        shot(page, "after-win-v06-6-this-computer-inbox")  # brief v0.6 screenshot 6 (light)
        page.emulate_media(color_scheme="dark")  # no page.evaluate on the local pages (their CSP); a short settle
        time.sleep(1)
        shot(page, "after-win-v06-6-this-computer-inbox-dark")
        page.emulate_media(color_scheme="null")
        page.click("#inbox-change")  # Cancel: back to the status line only
        mine_now = api(server, window_token, "/api/v1/me/inbox")
        check(((mine_now.get("inbox") or {}).get("machine"), (mine_now.get("inbox") or {}).get("agent"))
              == (WINDOW_MACHINE, LAPTOP_AGENT) and mine_now.get("waiting") == 0,
              f"the server's inbox after the pick: {mine_now.get('inbox')}, waiting {mine_now.get('waiting')}")
        moved = message(server, window_token, waiting) or {}
        check(moved.get("to_endpoint") == {"machine": WINDOW_MACHINE, "agent": LAPTOP_AGENT},
              f"the waiting message was not delivered to the pick: {moved.get('to_endpoint')}")
        say(f"PASS: D6b(a). This computer showed No inbox yet, offered {LAPTOP_AGENT} and {NOTES_AGENT} with "
            f"{waiting_label!r} ticked; Use as my inbox made it {expected!r} (Change shows the pick and offers the "
            "other agent); the server has the inbox and the waiting message went to it")

        # (b) Person delivery: the teammate sends to the PERSON; it is routed to the picked agent (C3.1(b), named path).
        delivery = send(server, teammate, {"person": EMAIL}, "For whoever is your inbox")
        routed = message(server, window_token, delivery) or {}
        check(routed.get("to_endpoint") == {"machine": WINDOW_MACHINE, "agent": LAPTOP_AGENT},
              f"a message to {EMAIL} was routed to {routed.get('to_endpoint')}")
        theirs = message(server, teammate, delivery) or {}
        check((theirs.get("to_endpoint") or {}).get("person") == EMAIL and theirs.get("to") == "@" + EMAIL,
              f"the sender sees the recipient as {theirs.get('to')!r} {theirs.get('to_endpoint')}")
        say(f"PASS: D6b(b). a message to the person {EMAIL} was routed to {LAPTOP_AGENT} on {WINDOW_MACHINE} (the "
            f"machine's own view); the sender sees only the person (state {theirs.get('delivery_state')}, hold "
            f"{theirs.get('hold_reason')}: machine mode has no connector to take it here)")

        # (c) PR1: the window machine's NOTES agent asks the teammate (a person); the teammate replies with
        # in_reply_to and no to; the reply goes back to (window machine, notes-agent) while it is live (C4.1, C13 M4).
        # The asking agent is deliberately NOT the inbox agent: the C4.1 fallback (the asker's inbox) would be
        # (window machine, laptop-agent), so the two outcomes are told apart.
        asked = str(uuid.uuid4())
        api(server, window_token, "/api/v1/messages", {"id": asked, "to": {"person": TEAMMATE_EMAIL},
                                                       "body": "A question from an agent on the window machine",
                                                       "from_agent": NOTES_AGENT})
        got = wait_for("the question in the teammate's inbox", lambda: next(
            (m for m in api(server, teammate, "/api/v1/inbox?routing=1&wait=0&limit=100")["messages"]
             if m["id"] == asked), None), timeout=120)
        check(got.get("from") == "@" + EMAIL and got.get("from_agent") is None and got.get("from_kind") == "agent"
              and (got.get("from_endpoint") or {}).get("person") == EMAIL,
              f"the teammate sees the question as from {got.get('from')!r} {got.get('from_endpoint')} "
              f"(from_agent {got.get('from_agent')!r}, from_kind {got.get('from_kind')!r})")
        reply, seen = pr1_reply(server, teammate, asked, window_token, WINDOW_MACHINE, NOTES_AGENT,
                                "An answer for the agent that asked", window_agents)
        check(seen.get("in_reply_to") == asked, f"the reply's in_reply_to is {seen.get('in_reply_to')}")
        say(f"PASS: D6b(c). PR1: a reply with in_reply_to and no to was routed back to {NOTES_AGENT} on "
            f"{WINDOW_MACHINE} (the asking agent, not the inbox agent)")

        # (d) Privacy (C5): nothing the teammate can fetch about these messages names the window machine, the
        # observer or either agent: the messages, its inbox, its conversations and their messages, people, agents.
        views = [message(server, teammate, m) for m in (waiting, delivery, asked, reply)]
        check(all(views), "the teammate cannot read one of its own messages")
        texts = [json.dumps(v) for v in views] + [json.dumps(api(server, teammate, p)) for p in (
            "/api/v1/inbox?routing=1&wait=0&limit=100&include_acked=true", "/api/v1/conversations?archived=include",
            f"/api/v1/conversations/{conversation}/messages", "/api/v1/people", "/api/v1/agents")]
        leaked = leaks(texts, [WINDOW_MACHINE, OBSERVER, LAPTOP_AGENT, NOTES_AGENT])
        check(not leaked, f"the teammate's view names {leaked}")
        page.click("nav.rc-nav button[data-open=settings]", no_wait_after=True)  # the app navigates
        page = window.page("settings.html", timeout=60)  # re-attached after the swap
        wait_for("Settings to show the inbox", lambda: page.inner_text("#inbox-status") == expected, timeout=60,
                 interval=1)
        say(f"PASS: D6b(d). the teammate's view of these messages, its inbox, conversations, people and agents never "
            f"names {WINDOW_MACHINE}, {OBSERVER}, {LAPTOP_AGENT} or {NOTES_AGENT}; Settings shows {expected!r}")

        # D6c. Brief v0.6 screenshot 14 (terminal text): `raincli people`, `raincli inbox show` and a refused
        # `raincli send <a teammate's machine handle>`, through the installed CLI on the window machine.
        # v0.6, not yet run on Windows.
        transcript = []

        import ctypes  # the frozen raincli.exe ignores PYTHON* variables and writes to a pipe in the ANSI code page
        ansi = f"cp{ctypes.windll.kernel32.GetACP()}"

        def text_of(raw):  # "·" in "Ready · Instant": cp1252 here, UTF-8 if a build ever writes that
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode(ansi, errors="replace")

        def cli_step(*args):
            result = subprocess.run([str(a) for a in (app.root / "bin" / "raincli.exe", *args)], capture_output=True,
                                    timeout=120)
            out, err = text_of(result.stdout), text_of(result.stderr)
            transcript.append(f"$ raincli {' '.join(str(a) for a in args)}\n{out}{err}[exit {result.returncode}]\n")
            return result, out + err
        people, people_text = cli_step("people")
        check(people.returncode == 0 and TEAMMATE_NAME in people_text and not re.search(rf"{TEAMMATE}(?!@)", people_text)
              and "\ufffd" not in people_text,
              f"raincli people: {SECRETS.scrub(people_text)[-1500:]}")
        shown, shown_text = cli_step("inbox", "show")
        check(shown.returncode == 0 and LAPTOP_AGENT in shown_text, f"raincli inbox show: {SECRETS.scrub(shown_text)[-1500:]}")
        body = work / "v06-14-body.txt"
        body.write_text("this must be refused\n", "utf-8")
        refused, refused_text = cli_step("send", TEAMMATE, "--body-file", body)
        check(refused.returncode != 0 and "Address people" in refused_text and TEAMMATE_EMAIL in refused_text,
              f"raincli send {TEAMMATE} was not refused with Address people: {SECRETS.scrub(refused_text)[-1500:]}")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / "v06-14-cli.txt").write_text(SECRETS.scrub("\n".join(transcript)), "utf-8")
        say(f"PASS: D6c. raincli people lists {TEAMMATE_NAME} (no handle), raincli inbox show names {LAPTOP_AGENT}, "
            f"raincli send {TEAMMATE} is refused with Address people naming {TEAMMATE_EMAIL}; transcript in "
            "v06-14-cli.txt")

        # D7. Offline and Retry.
        server.stop()
        page.click("nav.rc-nav button[data-open=inbox]", no_wait_after=True)  # the app navigates, not the click
        page = window.page("offline.html", timeout=120)  # re-attached after the swap
        shot(page, "d7-offline")
        server.start()
        page.click("#retry", no_wait_after=True)  # the app navigates, not the click
        page = window.page(INBOX, timeout=120)  # re-attached after the swap
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
    check(local_secret not in (app.root / "app-lock" / "app.log").read_text("utf-8", errors="replace"),
          "the app log holds the local pages' path secret")
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


# -- group chats: the brief's Windows-window screenshots (part G) ------------------------------------------------

G_TEAM = "northwind"
G_PEOPLE = {  # key: (display name, email); Dylan owns the team (the member panel's "Ask Dylan ...")
    "dylan": ("Dylan", "g-dylan@example.invalid"), "maya": ("Maya", "g-maya@example.invalid"),
    "jon": ("Jon", "g-jon@example.invalid"), "priya": ("Priya", "g-priya@example.invalid"),
    "sam": ("Sam", "g-sam@example.invalid"), "ryan": ("Ryan", "g-ryan@example.invalid")}
G_MACHINES = {"maya": "g-maya-laptop", "jon": "g-jon-laptop", "priya": "g-priya-laptop", "ryan": "g-ryan-laptop"}
G_AGENTS = {"maya": "g-maya-inbox", "jon": "g-jon-api", "priya": "g-priya-web", "ryan": "g-ryan-old"}
G_SIZES = ((1180, 780), (760, 520))
G_ASK = "@Jon @Priya the orders API renamed `total` to `total_cents`. Check your side."


def g_presence(server, token, agent, version):
    """A presence report naming ``agent`` (idle, next-turn) and the client ``version`` (§14.1)."""
    agents = [{"key": hashlib.sha256(agent.encode()).hexdigest()[:16], "name": agent, "type": "claude",
               "status": "idle", "source": "hook", "reachability": "next-turn"}]
    return api(server, token, "/api/v1/presence", {"status": "ready", "agents": agents, "client": {
        "version": version, "update_mode": "manual", "update_state": "current"}}, method="PUT")


def g_copy_of(server, token, post_id):
    """The machine's copy of ``post_id`` from a ``groups=1`` poll (which also keeps the machine group-capable)."""
    messages = api(server, token, "/api/v1/inbox?routing=1&groups=1&wait=0&limit=100").get("messages") or []
    return next((m for m in messages if m.get("post_id") == post_id), None)


def g_members(server):
    return server.admin("group-members", G_TEAM, "checkout").stdout


G_WHERE = """() => ({path: location.pathname,
    rows: Array.from(document.querySelectorAll('.rc-convs .rc-conv')).map((a) => [
        a.getAttribute('href').replace(/^.*\\/app\\//, ''), (a.querySelector('.rc-conv-name') || a).innerText,
        (a.querySelector('.rc-conv-preview') || {}).innerText || ''])})"""


def part_g(app, server, installers, work):
    """Group chats: the brief's Windows-window screenshots 1-4; see G in the module docstring."""
    from playwright.sync_api import sync_playwright

    passwords = {key: SECRETS.add(secrets.token_urlsafe(18)) for key in G_PEOPLE}
    for key, (name, email) in G_PEOPLE.items():
        server.admin("create-user", "--email", email, "--name", name, input=passwords[key] + "\n")
    server.admin("create-team", "--slug", G_TEAM, "--name", "Northwind", "--owner", G_PEOPLE["dylan"][1])
    for key in ("maya", "jon", "priya", "sam", "ryan"):
        server.admin("add-member", "--team", G_TEAM, "--email", G_PEOPLE[key][1])
    tokens = {}
    for key in ("jon", "priya", "ryan"):  # their machines are API-only here; Maya's is the window's
        staged = server.work / f"{G_MACHINES[key]}.json"
        server.admin("register-agent", "--team", G_TEAM, "--owner", G_PEOPLE[key][1], "--handle", G_MACHINES[key],
                     "--out", str(staged))
        tokens[key] = SECRETS.add(json.loads(staged.read_text())["token"])
        staged.unlink()
    # G1. Maya's machine is the window's: a fresh install of this ref, signed in from the CLI.
    app.install(NEW, installers, "groups")
    app.quit()
    login_through_conpty(app, server, passwords["maya"], G_MACHINES["maya"], email=G_PEOPLE["maya"][1])
    tokens["maya"] = machine_token()
    app.launch_run_value()  # as at logon: the app's runtime reports this machine's client (quit before the window)
    # Jon and Priya on a groups client; Ryan on an old one (0.5.6: never takes part); Sam has no inbox.
    for key, version in (("jon", "0.6.1"), ("priya", "0.6.1"), ("ryan", "0.5.6")):
        g_presence(server, tokens[key], G_AGENTS[key], version)
    for key in ("maya", "jon", "priya", "ryan"):
        server.admin("set-inbox", "--team", G_TEAM, "--email", G_PEOPLE[key][1], "--handle", G_MACHINES[key],
                     "--agent", G_AGENTS[key], "--no-deliver-waiting")
    # Maya's machine reports its own client (this ref's build) through the app's runtime; then groups=1 polls.
    wait_for("the window machine's client version", lambda: ((entry(server, tokens["maya"], G_MACHINES["maya"]) or {})
                                                            .get("machine") or {}).get("client_version"), timeout=240)
    for key in ("maya", "jon", "priya"):
        api(server, tokens[key], "/api/v1/inbox?routing=1&groups=1&wait=0&limit=1")  # groups=1: capable
    emails = {key: G_PEOPLE[key][1] for key in G_PEOPLE}
    server.admin("group-create", G_TEAM, "checkout", emails["maya"], emails["jon"], emails["priya"], emails["ryan"])
    server.admin("group-create", G_TEAM, "infra", emails["sam"], emails["maya"])
    # An older 1:1 conversation (Jon to Maya), so the list has both kinds and the group's post is the newest.
    send(server, tokens["jon"], {"person": emails["maya"]}, "Is the orders migration done on your side?")
    time.sleep(1)
    # Maya's agent posts from her machine's CLI; Jon's and Priya's machines ack their copies and reply.
    ask_id = str(uuid.uuid4())
    app.cli("group", "send", "checkout", "--from-agent", G_AGENTS["maya"], "--body", G_ASK, "--id", ask_id)
    replies = {"jon": "Done on my side: the client reads `total_cents`.", "priya": "Updated the web totals too."}
    for key in ("jon", "priya"):
        copy = wait_for(f"{key}'s copy of Maya's post", lambda: g_copy_of(server, tokens[key], ask_id), timeout=60)
        api(server, tokens[key], f"/api/v1/messages/{copy['id']}/ack", {}, method="POST")
        api(server, tokens[key], "/api/v1/messages", {"id": str(uuid.uuid4()), "in_reply_to": copy["id"],
                                                     "body": replies[key], "from_agent": G_AGENTS[key]})
        time.sleep(1)
    for key in ("jon", "priya", "maya"):  # keep the groups clients capable for the member panel
        api(server, tokens[key], "/api/v1/inbox?routing=1&groups=1&wait=0&limit=1")
    members = g_members(server)
    say("G seed, raincli-admin group-members:\n" + SECRETS.scrub(members))
    for line in ("Maya\t" + emails["maya"] + "\ttaking part",
                 "Jon\t" + emails["jon"] + "\ttaking part", "Priya\t" + emails["priya"] + "\ttaking part",
                 "Ryan\t" + emails["ryan"] + "\tnot taking part"):
        check(line in members, f"the seed's member list lacks {line!r}")
    app.quit()  # the window is opened below with the CDP switch for that launch only
    say("PASS: G1. the screenshot seed: northwind with Maya (the window's machine), Jon and Priya taking part, Ryan "
        "on 0.5.6 and Sam with no inbox not taking part; checkout and infra by the operator; Maya's post from her "
        "CLI, Jon's and Priya's replies after acking their copies; an older 1:1 message from Jon")

    # G2. The window: it opens the group at launch (D12); then the four views, light and dark, at both sizes.
    hidden = [G_MACHINES[k] for k in ("jon", "priya", "ryan")] + [G_AGENTS[k] for k in ("jon", "priya", "ryan")]
    # The runner starts at 1024x768: 1920x1080 at 100% so a 1180x780 window fits (as D5h); restored after the shots.
    display = display_state()
    set_display(1920, 1080, 100)
    port = open_window_cdp(app)
    with sync_playwright() as pw:
        window = WindowCDP(pw, port)
        page = window.page(("app/groups/", "app/inbox", "app/conversations/"), timeout=180)
        try:
            wait_for("the launch to open the group", lambda: "/app/groups/" in url_of(page), timeout=60, interval=1)
        except Failure:
            shot(page, "groups-win-debug-launch")
            seen = page.evaluate("""() => { const newest = document.querySelector('[data-newest]'),
                list = document.querySelector('section.rc-list'), main = document.querySelector('.rc-main');
                return {path: location.pathname, body: document.body.className,
                        open_latest: !!document.querySelector('[data-open-latest]'),
                        newest: newest ? newest.getAttribute('href') : null,
                        groups: document.querySelectorAll('.rc-groups .rc-conv').length,
                        list: list ? getComputedStyle(list).display : null,
                        main: main ? getComputedStyle(main).display : null, size: [innerWidth, innerHeight],
                        ua_version: (navigator.userAgent.match(/RainCLIAppVersion\\/[0-9.]+/) || [null])[0]}; }""")
            raise Failure(f"the launch did not open the group: {seen}") from None
        page.wait_for_selector(".rc-post")
        group_path = urlsplit(url_of(page)).path
        try:
            hwnd = wait_for("the app window", lambda: app_window(app_pids(app.root)), timeout=60, interval=1)
            scale = window_scale(hwnd)
            for width, height in G_SIZES:
                resize_window(hwnd, round(width * scale), round(height * scale))
                wait_for(f"the page at about {width} px", lambda: abs(page.evaluate("innerWidth") - width) < 40,
                         timeout=30, interval=0.5)
                size = f"{width}x{height}"
                for scheme in ("light", "dark"):
                    shot4 = f"groups-win-4-launch-opens-the-group-{size}-{scheme}"
                    if (width, height) == G_SIZES[0]:
                        if scheme == "light":
                            themed_shot(page, "light", shot4)  # the launch itself, in both themes
                            themed_shot(page, "dark", shot4.replace("-light", "-dark"))
                        page.goto(server.url + group_path + "#latest")
                    else:
                        page.click("nav.rc-nav a:has-text('Inbox')")  # Inbox opens the newest activity (D12)
                        try:
                            wait_for("Inbox to open the group", lambda: "/app/groups/" in url_of(page), timeout=60,
                                     interval=1)
                        except Failure:
                            shot(page, f"groups-win-debug-inbox-{size}-{scheme}")
                            raise Failure(f"Inbox did not open the group: {page.evaluate(G_WHERE)}") from None
                        page.wait_for_selector(".rc-post")
                        themed_shot(page, scheme, shot4)
                    page.wait_for_selector(".rc-post")
                    check(page.locator(".rc-post").count() == 3 and page.locator("textarea").count() == 0,
                          "the group page has not three posts, or has a composer")
                    check(page.inner_text(".rc-delivered") == "Delivered to 2 of 2",
                          f"Maya's post shows {page.inner_text('.rc-delivered')!r}")
                    themed_shot(page, scheme, f"groups-win-2-checkout-read-only-{size}-{scheme}")
                    page.click(".rc-members > summary")
                    panel = page.inner_text(".rc-members-panel")
                    check("Ryan" in panel and "not taking part" in panel and "Ask Dylan" in panel
                          and page.locator(".rc-members-panel button, .rc-members-panel a, .rc-members-panel input").count() == 0,
                          f"the member panel: {panel!r}")
                    themed_shot(page, scheme, f"groups-win-3-member-panel-{size}-{scheme}")
                    page.click(".rc-members > summary")
                    page.click(".rc-convs:not(.rc-groups) .rc-conv:has-text('Jon')")
                    page.wait_for_selector(".rc-thread")
                    check(page.locator(".rc-groups .rc-conv").count() == 2, "the list's Groups section lacks a group")
                    check(not list_fits(page), f"at {size} the list does not fit: {list_fits(page)}")
                    themed_shot(page, scheme, f"groups-win-1-list-with-groups-{size}-{scheme}")
                    found = [h for h in hidden if h in page.content()]
                    check(not found, f"teammates' machine or agent names on Maya's pages: {found}")
        finally:
            set_display(display[0], display[1], display[2])
        page.emulate_media(color_scheme="null")
        page.goto(server.url + group_path)
        window.close()
    say("PASS: G2. the window opened checkout at launch (D12); the list with the Groups section, the read-only "
        "thread (three posts, Delivered to 2 of 2, no composer), the member panel (Ryan not taking part, no "
        "controls) and the launch, light and dark at 1180x780 and 760x520, in build/e2e-artifacts (groups-win-*)")
    app.quit()
    app.uninstall(signout=True, label="g")


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


CODEX_PROBE = """
import sys, time
sys.path.insert(0, sys.argv[1])
from raincli_agent.login import runtime_config_path
from raincli_agent.config import default_config_path
from raincli_agent.runtime import hooks_install
t = time.monotonic()
print(hooks_install.status("codex", runtime_config_path(str(default_config_path()))), round(time.monotonic() - t, 1))
"""


def codex_status_probe(codex):
    """hooks_install.status("codex") in a fresh process with the pinned Codex first on PATH, bounded."""
    env = dict(os.environ, PATH=str(codex.parent) + os.pathsep + os.environ.get("PATH", ""))
    try:
        out = subprocess.run([sys.executable, "-c", CODEX_PROBE, str(PKG)], env=env, capture_output=True, text=True,
                             timeout=90)
        return (out.stdout + out.stderr).strip()[-600:]
    except subprocess.TimeoutExpired:
        return "hooks_install.status did not return within 90 s"


def inbox_after_sign_in(window, page, timeout=180):
    """The inbox after a sign-in, or a failure naming what the sign-in page said."""
    try:
        return window.page(INBOX, timeout=timeout)
    except Failure as exc:
        try:
            said = page.inner_text("#message")
        except Exception:  # noqa: BLE001 - the page went away
            said = "(the sign-in page is gone)"
        raise Failure(f"{exc}; the sign-in page says {said!r}") from None


def signed_in_fresh(server, observer, handle, config_dir):
    e = entry(server, observer, handle) or {}
    return (e.get("active") is not False and e.get("handle") == handle
            and (config_dir / "person.json").is_file() and (config_dir / "agent.json").is_file())


def part_e(app, server, installers, password, observer, teammate, work, codex=None):
    """Stale or foreign saved setups; see E in the module docstring. ``teammate``: the teammate machine's token (v0.6)."""
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
        # §16.20 F1: the backup's server is never read, so nothing asks "Sign in to <host>?"; the fresh
        # sign-in goes to the default service, which this runner maps to itself (no production traffic).
        check(not page.is_visible("#service-field"), "the window offered the backup's server")
        before = handles(server, observer)
        window_sign_in(page, password, "e2e-fresh-one")
        wait_for("the default service's refusal",
                 lambda: "error" in (page.get_attribute("#message", "class") or ""), timeout=120, interval=1)
        check(handles(server, observer) == before, "a sign-in reached the throwaway server without a choice")
        shot(page, "e1-default-service")
        window.close()
    app.quit()
    login_through_conpty(app, server, password, "e2e-fresh-one")  # a non-default server: --api-url
    port = open_window_cdp(app)
    with sync_playwright() as pw:
        window = WindowCDP(pw, port)
        page = window.page(INBOX, timeout=180)
        check(EMAIL in page.inner_text(".rc-whoami"), "the fresh machine did not open the owner's inbox")
        shot(page, "e1-inbox")
        window.close()
    wait_for("the fresh machine and its person session",
             lambda: signed_in_fresh(server, observer, "e2e-fresh-one", config_dir), timeout=120)
    check("e2e-other-revoked" not in handles(server, observer), "the other account's machine is in this team")
    say("PASS: E1. a revoked credential of another account: migration set it aside (agent.json and the connector "
        "config in replaced-<stamp>, the queue left in place); the window ignored the backup's server (no choice; "
        "its fresh sign-in went to the default service, mapped away from production) and "
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
        check(page.inner_text("#service-legend") == f"Sign in to {urlsplit(server.url).netloc}?",
              f"the offer asks {page.inner_text('#service-legend')!r}")  # §16.20 F1: an explicit choice
        page.check("#service-other")
        page.fill("#password", password)
        page.click("#submit")
        page = inbox_after_sign_in(window, page)
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
    # v0.6, not yet run on Windows: the teammate writes to the PERSON, whose inbox is now this machine (inbox-only).
    point_inbox_at(server, EMAIL, "e2e-two-good", good_token)
    sent = send(server, teammate, {"person": EMAIL}, "after the stale one was set aside")
    picked_up(server, teammate, good_token, sent, "delivery to the valid credential")
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
        check(page.inner_text("#service-legend") == f"Sign in to {urlsplit(server.url).netloc}?",
              f"the offer asks {page.inner_text('#service-legend')!r}")  # §16.20 F1: an explicit choice
        page.check("#service-other")
        page.fill("#password", password)
        page.click("#submit")
        page = inbox_after_sign_in(window, page)
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
            page = inbox_after_sign_in(window, page)
            page.click("nav.rc-nav a:has-text('This computer')", no_wait_after=True)  # the app cancels it
            page = window.page("this-computer.html", timeout=60)
            row = "#hooks li[data-agent=codex]"
            try:
                wait_for("Codex's state", lambda: page.locator(f"{row} [data-state]").count() == 1, timeout=120,
                         interval=1)
            except Failure:
                raise Failure(f"no Codex state on This computer; the card says {page.inner_text('#hooks')!r}, "
                              f"message {page.inner_text('#hooks-message')!r}; the core directly: "
                              f"{codex_status_probe(codex)}") from None
            state = page.get_attribute(f"{row} [data-state]", "data-state")
            check(state == "not_connected", f"Codex shows {state!r}: {page.inner_text(row)!r}; "
                                            f"codex at {codex} (on the app's PATH)")
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


def prepare_old_pip_client(work, version=OLD_PIP):
    """``version`` (0.2.0 by default; v0.6: also 0.5.1 for B2) from its release archive, in its own venv (done
    before the hosts file changes)."""
    venv = work / f"old-pip-{version}"
    run([sys.executable, "-m", "venv", venv], timeout=180)
    python = venv / "Scripts" / "python.exe"
    archive = f"https://github.com/{updates.REPO}/archive/refs/tags/{version}.zip"
    run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", f"raincli @ {archive}#subdirectory=raincli"],
        timeout=900)
    reported = run([venv / "Scripts" / "raincli.exe", "--version"]).stdout.strip()
    check(reported == "raincli " + version[1:], f"the old pip client {version} reports {reported!r}")
    return venv


# -- the job token for this e2e's own GitHub API lookups -------------------------------------------
# Unauthenticated REST calls are limited to 60 an hour per IP, and hosted runners share IPs, so the setup lookups
# below failed with HTTP 403 on some first attempts. The workflow passes the job token as RAINCLI_E2E_GITHUB_TOKEN;
# main() takes it out of the environment before anything starts, so nothing launched inherits it. It is sent only
# on https requests to exactly api.github.com, never to codeload or any redirect target, and only inside
# github_api_auth(). The shipped updater is unchanged: it sends no credentials.
GITHUB_API_HOST = "api.github.com"
_GITHUB_TOKEN = None


def take_github_token(environ=None):
    """Remove RAINCLI_E2E_GITHUB_TOKEN from the environment and keep it in this process only."""
    global _GITHUB_TOKEN
    _GITHUB_TOKEN = (os.environ if environ is None else environ).pop("RAINCLI_E2E_GITHUB_TOKEN", "") or None
    return _GITHUB_TOKEN is not None


class GitHubApiAuth(urllib.request.BaseHandler):
    """At request time, for every request the opener sends (a redirect goes through the processors again): the
    token on an https request to exactly api.github.com, and no Authorization at all on any other request."""
    handler_order = 1000  # after the other processors, so nothing adds a header after this check

    def __init__(self, token):
        self._token = token

    def _check(self, req):
        parts = urlsplit(req.full_url)
        for headers in (req.headers, req.unredirected_hdrs):
            headers.pop("Authorization", None)
        if parts.scheme == "https" and parts.hostname == GITHUB_API_HOST and parts.port in (None, 443):
            req.add_unredirected_header("Authorization", "Bearer " + self._token)
        return req

    http_request = https_request = _check


@contextlib.contextmanager
def github_api_auth():
    """The token on this e2e's own api.github.com lookups, through the updater's opener (its redirect allowlist
    kept). The opener is swapped for one with GitHubApiAuth and restored, the same object, on exit."""
    if _GITHUB_TOKEN is None:
        yield False
        return
    saved = updates._OPENER
    updates._OPENER = urllib.request.build_opener(updates._Redirects, GitHubApiAuth(_GITHUB_TOKEN))
    try:
        yield True
    finally:
        updates._OPENER = saved


def report_rate_limit():
    """One GET /rate_limit (it doesn't count against the limit): the core budget of whoever is asking."""
    try:
        core = json.loads(updates.fetch(f"https://{GITHUB_API_HOST}/rate_limit", 64 * 1024))["resources"]["core"]
    except (updates.NetworkError, KeyError, TypeError, ValueError) as exc:
        say(f"rate_limit: unavailable ({type(exc).__name__})")
        return
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(core.get("reset", 0))))
    say(f"rate_limit: core.remaining={core.get('remaining')} core.limit={core.get('limit')} reset={reset}")


def github_lookup(what, call):
    """``call()`` under the job token when there is one; says only whether it was authenticated."""
    with github_api_auth() as authenticated:
        say(f"GitHub API lookup ({what}): {'authenticated' if authenticated else 'unauthenticated'}")
        try:
            return call()
        except updates.ReleaseNotFound:
            raise
        except updates.NetworkError as exc:
            raise Failure(f"{exc} ({what} on {GITHUB_API_HOST}, "
                          f"{'authenticated' if authenticated else 'unauthenticated'})") from None


def download_release_installers(work):
    """The published RainCLI-Setup assets of OLD and NEW, by exact name, checksum-verified."""
    target = work / "installers"
    target.mkdir()
    for version in (OLD, NEW):
        release = json.loads(github_lookup(f"release v{version}", lambda: updates.fetch(
            f"{updates.API}/releases/tags/v{version}", 1024 * 1024)))
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
        with github_api_auth():
            report_rate_limit()
        # resolve()'s two api.github.com calls (the release by tag, then its commit). The archive comes from
        # codeload in install(), outside the token's scope.
        release = github_lookup(f"release {OLD_MANAGED}", lambda: updates.resolve(OLD_MANAGED))
    except updates.ReleaseNotFound:
        raise Failure(f"release {OLD_MANAGED} not found in {updates.REPO}") from None
    result = updates.install(root, release)
    check(result.get("status") == "installed", f"managed install of {OLD_MANAGED} returned {result.get('status')}")
    return root


def build_rollback_installer(work):
    """BROKEN from a STAGING COPY of the client whose tray exits 1 when started in the background, so
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


def register(server, name, owner=EMAIL):
    staged = server.work / f"{name}.json"
    server.admin("register-agent", "--team", TEAM, "--owner", owner, "--handle", name, "--out", str(staged))
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
    if args.only:  # part G alone needs none of the old clients, Codex or the rollback build
        old_venv = v051_venv = managed = codex = rollback = None
    else:
        old_venv = prepare_old_pip_client(work)
        say(f"PASS: old pip client {OLD_PIP} installed from its release archive")
        v051_venv = prepare_old_pip_client(work, OLD_V051)  # v0.6, not yet run on Windows (B2, C10.1)
        say(f"PASS: pip client {OLD_V051} installed from its release archive")
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
    # v0.6, not yet run on Windows: a second person in the team, with one machine that is their inbox (inbox-only,
    # so messages to them land in that machine's own queue, which inbox_bodies(server, teammate) reads).
    teammate_password = SECRETS.add(secrets.token_urlsafe(18))
    server.admin("create-user", "--email", TEAMMATE_EMAIL, "--name", TEAMMATE_NAME, input=teammate_password + "\n")
    server.admin("add-member", "--team", TEAM, "--email", TEAMMATE_EMAIL)
    teammate = register(server, TEAMMATE, owner=TEAMMATE_EMAIL)[1]
    point_inbox_at(server, TEAMMATE_EMAIL, TEAMMATE, teammate)
    # §21 C13 M2: /agents lists the caller's own machines only. C2.2: machine addresses are refused for everyone, a
    # teammate's handle mapped to its person, your own handle with the generic text (C13 L3).
    check(handles(server, observer) == [OBSERVER] and handles(server, teammate) == [TEAMMATE],
          f"/agents shows {handles(server, observer)} to the observer, {handles(server, teammate)} to the teammate")
    mapped = [(observer, TEAMMATE, TEAMMATE_EMAIL), (teammate, OBSERVER, EMAIL), (teammate, {"machine": OBSERVER}, EMAIL),
              (teammate, {"machine": OBSERVER, "agent": "inbox"}, EMAIL)]
    for token, to, person in mapped:  # a teammate's machine: "Address people: send to <Name> (<email>)"
        refused = refusal(server, token, to)
        check(refused is not None and refused[:2] == (400, "address_people") and person in refused[2],
              f"a send to the teammate machine {to}: {refused}")
    own, unknown = refusal(server, observer, OBSERVER), refusal(server, observer, "e2e-no-such-machine")
    check(own is not None and own[:2] == (400, "address_people") and own == unknown and EMAIL not in own[2],
          f"your own handle and an unknown one must get the same generic refusal: {own} / {unknown}")
    say(f"PASS: throwaway server on {server.url}; user, team {TEAM} and observer {OBSERVER}; v0.6: teammate "
        f"{TEAMMATE_NAME} with {TEAMMATE} as their inbox; /agents shows own machines only; machine addresses are "
        "refused with address_people (a teammate's handle mapped to the person)")

    if args.real:
        say(f"PASS: published installers of v{OLD} and v{NEW} downloaded and checksum-verified")
    else:
        if rollback is not None:
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
    if args.only == "groups-shots":
        part_g(app, server, installers, work)
        return

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
        part_d(app, server, installers, password, observer, teammate, menu, work)

    # == E. a stale or foreign saved setup ==================================================================
    if args.real:
        say("SKIP: E. the published v0.4 installers have no v0.5.1 stale-setup handling")
    else:
        part_e(app, server, installers, password, observer, teammate, work, codex=codex)

    # == G. group chats: the brief's Windows-window screenshots ===========================================
    if args.real:
        say("SKIP: G. the published v0.4 installers have no group pages")
    else:
        fresh_slate(app)
        part_g(app, server, installers, work)

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
    # v0.6, not yet run on Windows: the teammate writes to the PERSON; EMAIL's inbox is this machine, inbox-only, so
    # the message takes the machine-endpoint path a 0.2.0 client polls. KNOWN v0.6 LIMIT (C10.3): v0.6 shows every
    # sender but the reader's own machine as "@email" (C5.2), and a pre-0.5 connector skips a sender that is not a
    # handle ("bad sender"), unacked; so here "took" is an ack OR that recorded skip (the queue's skipped.json), and
    # the server must have routed it to this machine either way. Migration below needs only the old queue.
    point_inbox_at(server, EMAIL, OLD_MACHINE, pip_token)
    first = send(server, teammate, {"person": EMAIL}, "before migration")
    took = wait_for("the old pip connector to take a message", lambda: old_client_took(server, teammate, queue, first),
                    timeout=240)
    check((message(server, pip_token, first) or {}).get("to_endpoint") == {"machine": OLD_MACHINE},
          "the message to the person was not routed to the old machine's endpoint")
    say(f"B: the {OLD_PIP} connector {took} the teammate's message to {EMAIL} (routed to {OLD_MACHINE})")
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
    second = send(server, teammate, {"person": EMAIL}, "after migration")  # v0.6: to the person, as above
    picked_up(server, teammate, pip_token, second, "delivery after migration")
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

    # == B2. a v0.5.1 client still receives (v0.6 C10.1; brief "Windows e2e") ==================================
    # v0.6, NOT YET RUN ON WINDOWS. A pip v0.5.1 client as a foreground connector (as in B; Herdr is the e2e's
    # missing "raincli-e2e-no-herdr", so it stores and acks, then holds what it can't hand to an agent). It never
    # reports agents or presence, so its inbox is set by the operator and its agent's liveness is published here.
    fresh_slate(app)
    api_url, v051_token = register(server, V051_MACHINE)
    write_private(default_agent_config(), {"api_url": api_url, "token": v051_token})
    connector = config_dir / "connector.json"
    write_private(connector, {"herdr_agent": "e2e-inbox", "herdr_bin": "raincli-e2e-no-herdr",
                              "state_dir": "v051-queue", "poll_wait": 1})
    v051_raincli = v051_venv / "Scripts" / "raincli.exe"
    v051_connector = start_console([v051_raincli, "connector", "run", "--config", connector], work / "v051-connector.log")
    stack.callback(end, v051_connector)
    # (a) C3.1(a), inbox mode: EMAIL's inbox is this machine, inbox-only; the teammate writes to the person.
    point_inbox_at(server, EMAIL, V051_MACHINE, v051_token)
    inbox_mode = send(server, teammate, {"person": EMAIL}, "to a v0.5.1 inbox, inbox mode")
    picked_up(server, teammate, v051_token, inbox_mode, "the v0.5.1 connector to ack (inbox mode)")
    check((message(server, v051_token, inbox_mode) or {}).get("to_endpoint") == {"machine": V051_MACHINE},
          "the inbox-mode message was not routed to the v0.5.1 machine's endpoint")
    say(f"PASS: B2(a). a message to the person {EMAIL} reached the {OLD_V051} client's inbox (C3.1(a), acked)")
    # (b) C3.1(b), the named path: routing "all", EMAIL's inbox is (this machine, its agent).
    point_inbox_at(server, EMAIL, V051_MACHINE, v051_token, agent=V051_AGENT, inbox_only=False)
    publish_local_agents(server, v051_token, [V051_AGENT], source="herdr")
    named = send(server, teammate, {"person": EMAIL}, "to a v0.5.1 inbox, named path")
    check((message(server, v051_token, named) or {}).get("to_endpoint") == {"machine": V051_MACHINE, "agent": V051_AGENT},
          "the named-path message was not routed to the v0.5.1 machine's agent")
    wait_for("the v0.5.1 connector to ack (named path)",
             lambda: (message(server, v051_token, named) or {}).get("acked_at"), timeout=240)
    say(f"PASS: B2(b). a message to the person reached {V051_AGENT} on the {OLD_V051} client (C3.1(b)), acked by it")
    # (c) C10.1 "send to a person by email": the old client's own CLI, from its agent; then a PR1 reply (in_reply_to,
    # no to) is routed back to (this machine, that agent), and the old connector acks it (its hand-off to the agent
    # then holds offline here: the e2e has no Herdr; the server's routing and the client's ack are what is checked).
    question = work / "v051-question.txt"
    question.write_text("a question from a v0.5.1 agent\n", "utf-8")
    asked = str(uuid.uuid4())
    sent = run([v051_raincli, "send", "@" + TEAMMATE_EMAIL, "--from-agent", V051_AGENT, "--body-file", question,
                "--id", asked], check=False, timeout=120)
    check(sent.returncode == 0, f"the {OLD_V051} CLI could not send to a person: "
                                f"{SECRETS.scrub(sent.stdout + sent.stderr)[-1500:]}")
    got = wait_for("the v0.5.1 question at the teammate", lambda: message(server, teammate, asked), timeout=120)
    check(got.get("from") == "@" + EMAIL and got.get("from_agent") is None,
          f"the teammate sees the v0.5.1 question as from {got.get('from')!r} (from_agent {got.get('from_agent')!r})")
    # EMAIL's inbox back to another name, so the C4.1 fallback (the asker's inbox) is told apart from the PR1 target.
    point_inbox_at(server, EMAIL, V051_MACHINE, v051_token, agent="inbox", inbox_only=False)
    reply, _ = pr1_reply(server, teammate, asked, v051_token, V051_MACHINE, V051_AGENT, "an answer for the v0.5.1 agent",
                         [V051_AGENT])
    wait_for("the v0.5.1 connector to ack the PR1 reply",
             lambda: (message(server, v051_token, reply) or {}).get("acked_at"), timeout=240)
    end(v051_connector)
    said = (work / "v051-connector.log").read_text("utf-8", errors="replace")
    check("malformed" not in said, f"the {OLD_V051} connector skipped a v0.6 message: {SECRETS.scrub(said)[-1500:]}")
    say(f"PASS: B2(c). the {OLD_V051} CLI sent to the person {TEAMMATE_EMAIL} with --from-agent; the teammate's reply "
        f"(in_reply_to, no to) was routed to {V051_AGENT} on {V051_MACHINE} and the {OLD_V051} connector acked it")

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
    # v0.6, not yet run on Windows: to the person, whose inbox is now this machine (inbox-only).
    point_inbox_at(server, EMAIL, MANAGED_MACHINE, managed_token)
    third = send(server, teammate, {"person": EMAIL}, "after managed migration")
    picked_up(server, teammate, managed_token, third, "delivery after the managed migration")
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
    take_github_token()  # first: nothing this script launches may inherit the job token
    if sys.platform == "win32":
        # Physical px in window measurements (D5d, D5i): a DPI-unaware process gets logical (virtualized) window
        # coordinates, so at 150% the app's 1140x780 window would read as 760x520.
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--installers",
                        help=f"directory holding locally built RainCLI-Setup-{OLD}.exe and -{NEW}.exe with .sha256 files")
    source.add_argument("--real", nargs=2, metavar=("FROM", "TO"),
                        help="published releases vX.Y.Z (v0.4.0 or later) whose installer assets to use")
    parser.add_argument("--server-python", help="an existing Python with raincli/requirements.lock installed")
    parser.add_argument("--only", choices=("groups-shots",),
                        help="run only the server and part G (the group chats screenshots)")
    args = parser.parse_args(argv)
    if args.only and args.real:
        parser.error("--only needs this ref's installers (--installers)")
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
