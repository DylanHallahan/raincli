"""``RainCLI-app.exe``: the app (protocol §15.5, §16.10). One process holds:

- the window (``window.AppWindow``, pywebview/WebView2) on the main thread: the local pages
  (sign-in, This computer, Settings, offline) and the hosted inbox. Closing it hides it;
- the tray icon (pystray, detached): Open, Pause/Resume, Open log, Sign in again, Quit;
- the runtime as this process's child (``winapp.AppHost``); when another version becomes current
  the app exits with ``SWITCH_EXIT`` for the stub;
- a supervisor thread: the runtime's step, ``--quit`` and open requests (a second launch focuses
  the window), the icon state, and toasts from the notification queue (§16.12 C14).

``--background`` starts with the window hidden unless there is no credential yet. ``pywebview``,
``pystray`` and ``Pillow`` are imported lazily, and only here and in ``window``.
"""
import os
from pathlib import Path
import queue
import sys
import threading
import webbrowser

from .. import __version__
from ..runtime import winapp
from . import policy
from . import status as model

ICONS = Path(__file__).resolve().parent / "icons"  # tray-<state>-64.png, bundled with the app
TITLE = "RainCLI"
NIN_BALLOONUSERCLICK = 0x0405
TICK = 1.0  # seconds: open and quit requests are answered within this
STEP_EVERY = 2  # ticks between runtime steps, icon refreshes and notification reads


def icon_path(state):
    """The bundled tray icon for one of ``status.ICON_STATES``; pystray scales it for the DPI."""
    if state not in model.ICON_STATES:
        raise ValueError(f"no tray icon for {state!r}")
    return ICONS / f"tray-{state}-64.png"


def icon_image(state):
    from PIL import Image
    with Image.open(icon_path(state)) as image:
        return image.convert("RGBA")


def icon_view(paused, state):
    """``(icon state, tooltip)``: paused shows the offline icon and says "Paused"."""
    if paused:
        return "offline", f"{TITLE}: Paused"
    return state, f"{TITLE}: {state}"


class LockedHost:
    """``AppHost`` shared by the supervisor, the tray menu and the window's js_api threads."""

    def __init__(self, host):
        self._host = host
        self._lock = threading.RLock()

    def __getattr__(self, name):
        value = getattr(self._host, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            with self._lock:
                return value(*args, **kwargs)
        return call

    @property
    def paused(self):
        return self._host.paused

    @property
    def config(self):
        return self._host.config

    @config.setter
    def config(self, value):
        with self._lock:
            self._host.config = value


def hook_toast_click(icon, callback):
    """pystray shows a toast with ``notify``; on Windows, also call ``callback`` when it is clicked
    (``NIN_BALLOONUSERCLICK``). False where that is not available."""
    try:
        from pystray._util import win32
        handlers = icon._message_handlers
        original = handlers[win32.WM_NOTIFY]
    except (ImportError, AttributeError, KeyError):
        return False

    def on_notify(wparam, lparam):
        if lparam == NIN_BALLOONUSERCLICK:
            callback()
            return 0
        return original(wparam, lparam)
    handlers[win32.WM_NOTIFY] = on_notify
    return True


class NoWindow:
    """The WebView2 Runtime is missing: the app never falls back to MSHTML. The tray and the runtime keep
    running; opening the window explains why and opens Microsoft's download page."""

    def __init__(self, tray):
        self.tray = tray

    def show(self):
        from .window import WEBVIEW2_DOWNLOAD
        if self.tray.icon is not None:
            self.tray.icon.notify("Install the Microsoft Edge WebView2 Runtime to open the window. "
                                  "Messages are still delivered.", TITLE)
        webbrowser.open(WEBVIEW2_DOWNLOAD)

    def open_hosted(self, path=None, thread_id=None):
        self.show()

    def load(self, url):
        self.show()

    def local_url(self, page, query=""):
        return page

    def home(self):
        pass

    def destroy(self):
        self.tray.stopping.set()


class Tray:
    def __init__(self, root_dir, *, background=True, webview=None):
        from .services import Services
        from .window import AppWindow
        self.root_dir = root_dir
        self.agent_config, self.runtime_config = winapp.paths(root_dir)
        self.state_dir = Path(root_dir or Path(self.agent_config).parent) / "state"
        self.host = LockedHost(winapp.AppHost(root_dir, __version__, self.runtime_config,
                                              log_path=self.state_dir / "runtime.log"))
        self.start_hidden = background
        self.services = Services(root_dir, self.host, paths=lambda: (self.agent_config, self.runtime_config))
        self.webview = webview
        self.window = AppWindow(self.services, profile_dir=self.state_dir / "webview", webview=webview,
                                browser_open=webbrowser.open, confirm=self.confirm,
                                log=(lambda text: winapp.app_log(root_dir, text)) if root_dir else None,
                                on_fresh_sign_in=lambda: self.offer_connect())
        self.calls = queue.Queue()
        self.stopping = threading.Event()
        self._started = threading.Event()
        self.exit_code = 0
        self.migrating = False
        self.icon = None
        self.icon_state = None
        self.toast_thread = None  # what clicking the last toast opens

    # -- plumbing ---------------------------------------------------------------------------

    def post(self, fn, *args):
        """Run ``fn`` on the UI-action worker, one at a time (pywebview's window calls are thread-safe)."""
        self.calls.put((fn, args))

    def background(self, fn, *args, done=None):
        def work():
            try:
                result, error = fn(*args), None
            except Exception as exc:  # noqa: BLE001 - shown to the user, never the request
                result, error = None, exc
            if done is not None:
                self.post(done, result, error)
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        while not self.stopping.is_set():
            try:
                fn, args = self.calls.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 - one failed action never stops the app
                print(f"RainCLI app: {fn.__name__} failed: {type(exc).__name__}", file=sys.stderr)

    def supervise(self):
        tick = 0
        while not self.stopping.wait(TICK):
            tick += 1
            if self.root_dir is not None and winapp.quit_requested(self.root_dir):
                return self.quit(0)  # RainCLI.exe --quit (the uninstaller)
            if self.root_dir is not None and winapp.take_show_request(self.root_dir):
                self.post(self.window.show)  # a second launch: focus this window
            if tick % STEP_EVERY:
                continue
            if not self.migrating:
                self.agent_config, self.runtime_config = winapp.paths(self.root_dir)
                if self.host.config != str(self.runtime_config):
                    self.host.config = str(self.runtime_config)
                code = self.host.step() if self.signed_in() else None
                if code is not None:
                    return self.quit(code)
            self.refresh_icon()
            self.toasts()

    def signed_in(self):
        return os.path.exists(self.agent_config) and os.path.exists(self.runtime_config)

    def status(self):
        from ..runtime.service import status
        try:
            return status(self.runtime_config)
        except Exception:  # noqa: BLE001 - no runtime config yet
            return {"status": "not_observed"}

    def refresh_icon(self):
        state, title = icon_view(self.host.paused, None if self.host.paused else model.icon_state(self.status()))
        if self.icon is not None and (state, title) != (self.icon_state, getattr(self, "icon_title", None)):
            self.icon_state, self.icon_title = state, title
            self.icon.icon = icon_image(state)
            self.icon.title = title

    # -- toasts (§16.10, §16.12 C14) ------------------------------------------------------------

    def toasts(self):
        """One toast per new notification: the sender's name only, never body text."""
        if not self.signed_in():
            return
        try:
            entries = self.services.notifications()
        except Exception:  # noqa: BLE001 - no person session or queue yet
            return
        for entry in entries or ():
            text, thread = self.toast_for(entry)
            self.toast_thread, self.toast_opens = thread, None  # a message toast opens its thread
            if self.icon is not None:
                self.icon.notify(text, TITLE)

    def toast_for(self, entry):
        """``(text, conversation id or None)`` for a queue entry ``{id, kind, at}``."""
        kind, sender, thread = entry.get("kind"), None, None
        try:
            summary = self.services.message_summary(entry["id"])
            kind, sender, thread = summary["kind"] or kind, summary["sender"], summary.get("conversation_id")
        except Exception:  # noqa: BLE001 - offline: a toast without the name
            pass
        return policy.toast_text(kind, sender), thread

    def toast_clicked(self):
        if getattr(self, "toast_opens", None) == "this-computer":
            self.toast_opens = None
            self.post(self.open_this_computer)
            return
        thread = self.toast_thread
        self.post(self.open_thread, thread)

    def open_this_computer(self):
        self.window.show()
        self.window.load(self.window.local_url("this-computer"))

    def show_hooks_notice(self):
        """The client core's one-time notice (for example, Codex hooks repaired after an update: trust them
        again in /hooks). Shown once in the tray, then dismissed; clicking it opens This computer."""
        try:
            text = self.services.pending_hooks_notice()
        except Exception:  # noqa: BLE001 - not signed in yet, or no runtime config
            return
        if text and self.icon is not None:
            self.toast_opens = "this-computer"
            self.icon.notify(text[:240], TITLE)

    def offer_connect(self):
        """§16.19 item 3: after a fresh sign-in, ONE dismissible notice when Codex or Claude Code is installed
        and not connected, pointing to This computer. It installs nothing: connecting is the user's click."""
        if getattr(self, "connect_offered", False):
            return
        try:
            names = self.services.unconnected_agents()
        except Exception:  # noqa: BLE001 - no notice rather than a wrong one
            return
        if not names or self.icon is None:
            return
        self.connect_offered = True
        self.toast_opens = "this-computer"
        verb = "is" if len(names) == 1 else "are"
        self.icon.notify(f"{' and '.join(names)} {verb} installed but not connected to {TITLE}. "
                         "Open This computer to connect.", TITLE)

    def open_thread(self, thread):
        self.window.show()
        if thread:
            self.window.open_hosted(thread_id=thread)
        else:
            self.window.open_hosted("/app/inbox")

    # -- menu -----------------------------------------------------------------------------------

    def menu(self):
        import pystray
        item = pystray.MenuItem
        return pystray.Menu(
            item(f"Open {TITLE}", lambda: self.post(self.window.show), default=True),
            item(lambda _: "Resume" if self.host.paused else "Pause", lambda: self.post(self.services.toggle_pause)),
            item("Open log", lambda: self.post(self.services.open_log)),
            item("Sign in again", lambda: self.post(self.sign_in, True)),
            item("Quit", lambda: self.post(self.quit, 0)))

    def sign_in(self, again=False):
        """The local sign-in page. ``again`` replaces this machine's own credential (review 1a F9)."""
        self.window.show()
        self.window.load(self.window.local_url("sign-in", "again=1" if again else ""))

    def confirm(self, title, message):
        if isinstance(self.window, NoWindow):  # no dialogs without the window: say it, take the safe answer
            if self.icon is not None:
                self.icon.notify(message[:240], title)
            return False
        return bool(self.window.window.create_confirmation_dialog(title, message))

    def quit(self, code=0):
        self.exit_code = code
        if code != winapp.SWITCH_EXIT:
            self.host.stop()
        self.stopping.set()
        if self.icon is not None:
            self.icon.stop()
        self.window.destroy()

    # -- first run: migration, then sign-in -----------------------------------------------------

    def first_run(self, connector_configs=()):
        """Migration only when something is left to do and no new version is on
        probation; with a credential the runtime starts first (review 1a F5). The
        sign-in page appears only when there is no credential."""
        from ..migrate import Migration
        stop = threading.Event()

        def start(runtime_config):
            self.runtime_config = runtime_config
            self.host.config = str(runtime_config)
            self.host.resume()
            for _ in range(60):
                if stop.wait(1):
                    return False
                if model.ready(self.status()):
                    return True
            return False

        def pause():
            self.host.pause()

        migration = Migration(app_root=self.root_dir, connector_configs=connector_configs,
                              own_runtime=self.runtime_config, stop_own=pause, restart_own=self.host.resume,
                              own_running=self.host.running)
        on_probation = self.root_dir is not None and winapp.read_install(self.root_dir)["probation"]
        pending = not on_probation and migration.pending()
        # An old install's runtime may run this very config: never compete with it for
        # its locks; migration stops it and starts ours at step 4 (review 2 R1).
        if self.signed_in() and not (pending and migration.old_run_value()):
            self.host.start()
        if not pending:
            if not self.signed_in():
                self.post(self.sign_in)
            return
        self.migrating = True

        def notify(message):
            self.post(self.notice, message, stop)

        def done(result, error):
            self.migrating = False
            self.agent_config, self.runtime_config = winapp.paths(self.root_dir)
            self.host.config = str(self.runtime_config)
            if error is not None:
                self.notice(f"Moving this machine to the app failed: {error}", None)
            elif result["status"] == "connector_config_required":
                self.ask_connector_config(result["message"])
            elif result["status"] in ("busy", "waiting"):
                threading.Timer(10, self.post, (self.first_run,)).start()  # an old window still runs: try again
            elif result["status"] == "migrated_with_stale_set_aside" and self.root_dir is not None:
                # §16.18 V2: another credential stays valid and runs; the revoked one was set aside
                winapp.app_log(self.root_dir, "migration set a revoked credential aside; another one runs")
                if not self.host.paused and not self.host.running():
                    self.host.resume()
            elif result["status"] == "fresh_sign_in_needed":
                # §16.17 item 3: a revoked credential was set aside, not adopted; this computer signs in fresh
                if self.root_dir is not None:
                    winapp.app_log(self.root_dir, "migration set a revoked credential aside: showing sign-in")
                self.host.stop()
                self.sign_in()
            elif not self.signed_in():
                self.sign_in()
            elif not self.host.paused and not self.host.running():
                self.host.resume()
        self.background(lambda: migration.run(start, notify=notify, cancelled=stop.is_set), done=done)

    def ask_connector_config(self, message):
        """Review 1a F11: the handle has served as an inbox, so ask where its connector config is."""
        self.window.show()
        if not self.confirm(TITLE, message.split(" Run:")[0] + "\n\nChoose the connector config file?"):
            return
        dialog = getattr(getattr(self.webview, "FileDialog", None), "OPEN", 10)
        paths = self.window.window.create_file_dialog(dialog, file_types=("JSON (*.json)",))
        if paths:
            self.first_run(connector_configs=(paths[0],))

    def notice(self, message, cancel_event):
        if isinstance(self.window, NoWindow):
            if self.icon is not None:
                self.icon.notify(message[:240], TITLE)
            return  # migration keeps waiting; Quit stops it
        self.window.show()
        if cancel_event is None:
            self.window.window.create_confirmation_dialog(TITLE, message)
        elif not self.confirm(TITLE, message + "\n\nOK keeps waiting; Cancel stops."):
            cancel_event.set()

    # -- run -----------------------------------------------------------------------------------------

    def run(self):
        lock = winapp.tray_lock(self.root_dir) if self.root_dir is not None else None
        if self.root_dir is not None and lock is None:
            # Another app runs this install: never exit 0, which the stub reads as a quit (review 3 N1).
            # The stub's open request (a second launch) reaches that app.
            return winapp.ALREADY_RUNNING_EXIT
        import pystray
        from .window import webview2_version
        if self.webview is None and os.name == "nt" and webview2_version() is None:
            self.window = NoWindow(self)
        elif self.webview is None:
            import webview
            self.webview = self.window._webview = webview
        try:
            return self._run(pystray)
        finally:
            if lock is not None:
                os.close(lock)

    def _run(self, pystray):
        self.icon_state = "offline"
        self.icon = pystray.Icon(TITLE, icon_image("offline"), f"{TITLE}: offline", self.menu())
        hook_toast_click(self.icon, self.toast_clicked)
        self.icon.run_detached()
        if not isinstance(self.window, NoWindow):
            try:
                self.window.create()
                self.window.start(self.started)  # blocks on this (the main) thread until the window is destroyed
            except Exception as exc:  # noqa: BLE001 - the window failed: delivery must go on without it
                if self.root_dir is not None:
                    winapp.app_log(self.root_dir, f"the window could not start ({type(exc).__name__}); "
                                                  "running in the tray only")
                if self._started.is_set():
                    self.stopping.wait()
                else:
                    self.window = NoWindow(self)
        if isinstance(self.window, NoWindow) and not self._started.is_set():
            self.started()  # no GUI loop: the supervisor runs here until Quit
        self.stopping.set()
        if self.icon is not None:
            self.icon.stop()
        return self.exit_code

    def started(self):
        """pywebview's GUI loop is running (this is its worker thread), or the tray runs without a window."""
        self._started.set()
        threading.Thread(target=self.pump, daemon=True).start()
        if self.root_dir is not None:  # §16.15: a v0.4 stub can't take the shortcut's empty arguments
            from .shortcut import fix_v04_shortcut
            threading.Thread(target=fix_v04_shortcut, args=(self.root_dir,), daemon=True).start()
        self.window.home()
        opened = self.root_dir is not None and winapp.take_show_request(self.root_dir)
        if opened or not self.start_hidden or not self.signed_in() or not self.services.has_person_session():
            self.post(self.window.show)
        self.post(self.first_run)
        self.post(self.show_hooks_notice)
        self.supervise()


def self_check():
    """``RainCLI-app.exe --self-check`` (15.9): import the app and its GUI modules, with no desktop,
    so a build proves the frozen app can start. 0 on success."""
    import importlib
    modules = ("pystray", "PIL.Image", "PIL.PngImagePlugin", "webview", "raincli_agent.app.window",
               "raincli_agent.app.services", "raincli_agent.app.status", "raincli_agent.login",
               "raincli_agent.migrate", "raincli_agent.runtime.service", "raincli_agent.runtime.winapp")
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            print(f"RainCLI-app self-check: cannot import {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    from .window import LOCAL_DIR
    missing = [p for p in policy.LOCAL_PAGES if not (LOCAL_DIR / f"{p}.html").is_file()]
    missing += [icon_path(state).name for state in model.ICON_STATES if not icon_path(state).is_file()]
    if missing:
        print(f"RainCLI-app self-check: missing bundled files: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"RainCLI-app self-check: ok ({__version__})")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--self-check"]:
        return self_check()
    if argv not in (["--background"], []):
        print("usage: RainCLI-app.exe [--background] | --self-check", file=sys.stderr)
        return 2
    return Tray(winapp.app_root(), background=bool(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
