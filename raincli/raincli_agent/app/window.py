"""The app window (protocol §16.10, §16.12 C4, C14, C16): one pywebview (WebView2) window.

- **Local pages** (sign-in, This computer, Settings, offline) are bundled in ``app/local`` and served
  by pywebview's built-in server on ``http://127.0.0.1:<port>``. They talk to the app only through
  ``Api`` (js_api), with a nonce passed per load to that exact origin (``policy.NonceGate``).
- **Hosted pages** (inbox, threads, agents) come from the service in app mode, signed in through a
  single-use handoff. The window shows only the service origin and the local origin; anything else
  opens in the default browser, and ``/app/local/<page>`` shows the bundled page (``policy.Navigation``).
- **Offline:** a failed or timed-out load shows the local offline page, with Retry.
- **No debugging**, ever: ``webview.start(debug=False)`` and no debugging-related settings (C16).
  WebView2 debugging can only come from the test environment's own variable.

``pywebview`` is imported here only, and only by the Windows app.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import threading
import time
from pathlib import Path

from ..config import Secret
from . import policy

LOCAL_DIR = Path(__file__).resolve().parent / "local"
TITLE = "RainCLI"
LOAD_TIMEOUT = 20.0  # seconds before a hosted load counts as failed
PROFILE_RESET_MARKER = "reset-profile"
WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"  # Microsoft's Evergreen bootstrapper
log = logging.getLogger("raincli.app")


def webview2_version(winreg=None):
    """The installed WebView2 Runtime's version, or None. Where Microsoft documents it: the per-machine
    key (32-bit view) and the per-user key. Without it pywebview would fall back to MSHTML, which the
    app never uses."""
    if winreg is None:
        if os.name != "nt":
            return None
        import winreg
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
                       (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT),
                       (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT)):
        try:
            with winreg.OpenKey(hive, path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0])
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return version
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pywebview_settings():
    """The only pywebview settings the app changes (§16.12 C16 forbids every debugging one)."""
    return {"ALLOW_DOWNLOADS": True, "ALLOW_FILE_URLS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}


def reset_profile_if_marked(profile_dir):
    """Sign-out marks the private WebView2 profile for deletion; it is removed before the next window."""
    profile_dir = Path(profile_dir)
    marker = profile_dir.parent / PROFILE_RESET_MARKER
    if marker.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
        marker.unlink(missing_ok=True)


def mark_profile_for_reset(profile_dir):
    marker = Path(profile_dir).parent / PROFILE_RESET_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")


def _navigation_id(args):
    try:
        return int(args.NavigationId)
    except (AttributeError, TypeError, ValueError):
        return None


def _loggable(url):
    """Origin and path only: never a query (the handoff code) or a fragment."""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url or "")
        return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else "(none)"
    except ValueError:
        return "(unparsable)"


def _in_thread(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def _result(ok=True, message="", **extra):
    return {"ok": ok, "message": message, **extra}


class Api:
    """The js_api. Every method takes the current nonce first and refuses without it (C4). No method
    returns a credential: only status, names and messages."""

    def __init__(self, app):
        self._app = app

    def _guard(self, nonce):
        if not self._app.gate.check(nonce, self._app.current_url()):
            log.warning("js_api call refused: stale or missing nonce, or not the local origin")
            raise PermissionError("refused")

    def sign_in_defaults(self, nonce):
        self._guard(nonce)
        return self._app.services.sign_in_defaults()

    def sign_in(self, nonce, request, password):
        self._guard(nonce)
        return self._app.sign_in(request if isinstance(request, dict) else {}, Secret(password or ""))

    def status(self, nonce):
        self._guard(nonce)
        return self._app.services.status()

    def settings(self, nonce):
        self._guard(nonce)
        return self._app.services.settings()

    def save_settings(self, nonce, change):
        self._guard(nonce)
        try:
            return _result(message=self._app.services.save_settings(change))
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return _result(False, f"Not saved: {exc}")

    def toggle_pause(self, nonce):
        self._guard(nonce)
        return {"paused": self._app.services.toggle_pause()}

    def hooks(self, nonce):
        self._guard(nonce)
        return self._app.services.hooks()

    def connect_hooks(self, nonce, kind, connect=True):
        """§16.19 item 3: only ever from the user's click on This computer."""
        self._guard(nonce)
        connecting = connect is not False
        try:
            state, note = self._app.services.connect_hooks(str(kind), connecting)
        except Exception as exc:  # noqa: BLE001 - shown on the page (the Codex version gate, a refused path)
            return _result(False, f"Could not {'connect' if connecting else 'disconnect'}: {exc}")
        return _result(message=note or ("Connected." if connecting else "Disconnected."), state=state)

    def open_log(self, nonce):
        self._guard(nonce)
        return {"opened": self._app.services.open_log()}

    def sign_out(self, nonce):
        self._guard(nonce)
        return self._app.sign_out()

    def open(self, nonce, section):
        self._guard(nonce)
        return self._app.open_section(section)

    def retry(self, nonce):
        self._guard(nonce)
        return self._app.retry()


class AppWindow:
    """The window's behaviour, independent of pywebview (``webview`` is injected) so it is unit-tested."""

    def __init__(self, services, *, profile_dir, webview=None, browser_open=None, confirm=None,
                 local_port=None, timer=threading.Timer, later=_in_thread, log=None, on_fresh_sign_in=None):
        self.services = services
        self._log = log or (lambda text: None)  # app-lock\\app.log: origins and paths only, never a query
        self._on_fresh_sign_in = on_fresh_sign_in  # the tray's one notice about unconnected agents (§16.19)
        self._cancelled = set()  # NavigationIds this window cancelled (sentinel, other origins)
        self._timer_lock = threading.Lock()  # arm and disarm run on the UI thread and js_api threads
        self._loads = 0  # successful page loads so far; a timeout armed before one of them is moot
        self.profile_dir = Path(profile_dir)
        self._webview = webview
        self._browser_open = browser_open
        self._confirm = confirm
        self._timer = timer
        self._later = later  # WebView2 raises NavigationStarting on the UI thread: act after it returns
        self.local_base = f"http://127.0.0.1:{local_port or free_port()}/"
        self.window = None
        self.navigation = None
        self.gate = None
        self.last_hosted = None
        self.pending_path = None
        self.handed_off = False  # an app-mode session exists in the profile (this run)
        self.load_timer = None
        self.quitting = False
        self.install_token = None  # §16.14 S3: sent as "RainCLIApp/<token>" to the service origin only
        self._ua_base = None
        self._core = None
        self.api = Api(self)

    # -- set-up ---------------------------------------------------------------------------------------

    def local_url(self, page, query=""):
        if page not in policy.LOCAL_PAGES:
            page = "this-computer"
        return f"{self.local_base}{page}.html{('?' + query) if query else ''}"

    def service_url(self):
        try:
            return self.services.service_url()
        except Exception:  # noqa: BLE001 - not signed in: no hosted origin yet
            return "https://raincli.com"

    def refresh_navigation(self):
        self.navigation = policy.Navigation(self.service_url(), self.local_base)
        if self.gate is None:
            self.gate = policy.NonceGate(self.navigation)
        else:
            self.gate.navigation = self.navigation

    def refresh_install_token(self):
        self.install_token = self.services.app_install_token()

    def user_agent(self, base):
        """The webview's own User-Agent plus ``RainCLIApp/<app_install_token>`` (§16.14 S3)."""
        return f"{base} RainCLIApp/{self.install_token}" if self.install_token else base

    def create(self):
        """The window, hidden until ``show``; the first page is a local one."""
        wv = self._webview
        for key, value in pywebview_settings().items():
            wv.settings[key] = value
        self.refresh_navigation()
        self.refresh_install_token()
        self.window = wv.create_window(TITLE, url=str(LOCAL_DIR / "sign-in.html"), js_api=self.api,
                                       width=1180, height=780, min_size=(760, 520), hidden=True,
                                       text_select=True)
        self.window.events.loaded += self.on_loaded
        self.window.events.closing += self.on_closing
        self.window.events.before_show += self.attach_native
        return self.window

    def start(self, func):
        """pywebview's loop on this (the main) thread: never debug, a private profile on disk (C16)."""
        reset_profile_if_marked(self.profile_dir)
        self._webview.start(func, debug=False, http_server=True, http_port=int(self.local_base.rsplit(":", 1)[1].strip("/")),
                            private_mode=False, storage_path=str(self.profile_dir))

    def attach_native(self):
        """WebView2's own NavigationStarting/Completed, to block before a page loads (Windows only).
        Attached at ``before_show``: the control exists then, before the first navigation."""
        try:
            control = self.window.native.browser.webview
        except AttributeError:
            return False
        control.NavigationStarting += lambda sender, args: self._native_starting(args)
        control.NavigationCompleted += lambda sender, args: self._native_completed(args, sender)
        control.CoreWebView2InitializationCompleted += lambda sender, args: self._core_ready(sender.CoreWebView2)
        return True

    def _core_ready(self, core, context_all=None):
        """WebView2 is up (UI thread). The token never goes into the global User-Agent (§16.14 S3, review
        4 A2): our own request filter for the service origin, and a hook that adds the CURRENT token to
        requests for exactly that origin, so sign-in and sign-out rotations apply at once."""
        if context_all is None:
            from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
            context_all = CoreWebView2WebResourceContext.All
        self._core, self._context_all, self._filtered = core, context_all, set()
        self._ua_base = str(core.Settings.UserAgent)
        self._ensure_filter()
        core.WebResourceRequested += lambda sender, args: self._native_request(args.Request)

    def _ensure_filter(self):
        """UI thread only (core ready, or NavigationStarting): filter the current service origin."""
        if getattr(self, "_core", None) is None:
            return
        base = self.service_base()
        if policy.origin(base) is not None and base not in self._filtered:
            self._core.AddWebResourceRequestedFilter(base + "/*", self._context_all)
            self._filtered.add(base)

    def _native_request(self, request):
        if self._ua_base is None or not self.install_token:
            return
        if policy.same_origin(str(request.Uri), self.service_url()):
            request.Headers.SetHeader("User-Agent", self.user_agent(self._ua_base))

    def _native_starting(self, args):
        self._ensure_filter()  # the service origin can change at sign-in
        if self.before_navigate(str(args.Uri)):
            self._cancelled.add(_navigation_id(args))
            args.Cancel = True

    def _native_completed(self, args, sender=None):
        cancelled = _navigation_id(args) in self._cancelled
        self._cancelled.discard(_navigation_id(args))
        status = str(getattr(args, "WebErrorStatus", "") or "")
        # The control's own Source: window.get_current_url() can't answer on this (the UI) thread.
        source = getattr(sender, "Source", None) if sender is not None else None
        self.on_navigation_completed(bool(args.IsSuccess), int(getattr(args, "HttpStatusCode", 0) or 0),
                                     cancelled=cancelled or status.endswith("OperationCanceled"), status=status,
                                     url=str(source) if source is not None else None)

    # -- navigation -----------------------------------------------------------------------------------------

    def current_url(self):
        try:
            return self.window.get_current_url()
        except Exception:  # noqa: BLE001
            return None

    def before_navigate(self, url):
        """True to cancel ``url``: another origin opens in the browser, a sentinel shows the local page."""
        decision, target = self.navigation.decide(url)
        if decision == "allow":
            if not self.navigation.is_local(url):
                self.last_hosted = url
                self._arm_timeout()
            return False
        if decision == "local":
            self._log(f"sentinel {_loggable(url)} -> local page {target}")
            self._later(self.load, self.local_url(target))
        elif decision == "external" and self._browser_open is not None:
            self._later(self._browser_open, url)
        return True

    def on_loaded(self):
        """A new nonce per load; only the local origin receives it (C4). A page from anywhere else that
        slipped past ``before_navigate`` is replaced at once."""
        self._loaded_ok()
        url = self.current_url() or ""
        nonce = self.gate.on_loaded(url)  # every load retires the previous nonce
        decision, target = self.navigation.decide(url)
        if decision == "local":
            self.load(self.local_url(target))
            return
        if decision != "allow":  # slipped past before_navigate: never leave it on screen
            if decision == "external" and self._browser_open is not None:
                self._browser_open(url)
            self.show_offline()
            return
        if url and not self.navigation.is_local(url):
            path = policy.path_of(url)
            if path == "/login":  # the app-mode session ended: hand off again, then go where we were
                self.handed_off = False
                self.open_hosted(self.pending_path or "/app/inbox")
                return
            if self.pending_path and path == "/app/inbox":
                target, self.pending_path = self.pending_path, None
                self.load(self.service_base() + target)
                return
        if nonce is not None:
            self.window.evaluate_js(
                f"window.__rcNonce = {json.dumps(nonce)}; window.dispatchEvent(new Event('rc-nonce'));")

    def on_navigation_completed(self, success, http_status=0, *, cancelled=False, status="", url=None):
        """A network failure shows the offline page. An HTTP error page is the service's own answer
        (for example "Open on the website"), so it stays. A navigation this window cancelled (the
        /app/local sentinel, another origin) or one replaced by a newer load (OperationCanceled) is not a
        failure: WebView2 still reports it as unsuccessful, and treating it as offline would replace the
        page the app is loading instead."""
        url = url if url is not None else (self.current_url() or "")
        self._log(f"load {'ok' if success else 'failed'} {_loggable(url)}"
                  + (f" http {http_status}" if http_status else "") + (f" ({status})" if status and not success else "")
                  + (" cancelled by the app" if cancelled else ""))
        if cancelled:
            return
        if success or http_status:
            self._loaded_ok()
            return
        self._disarm_timeout()
        if not self.navigation.is_local(url):
            self.show_offline()

    def _arm_timeout(self):
        with self._timer_lock:
            if self.load_timer is not None:
                self.load_timer.cancel()
            armed_at = self._loads
            self.load_timer = self._timer(LOAD_TIMEOUT, lambda: self._timed_out(armed_at))
            self.load_timer.daemon = True
            self.load_timer.start()

    def _disarm_timeout(self):
        with self._timer_lock:
            if self.load_timer is not None:
                self.load_timer.cancel()
                self.load_timer = None

    def _loaded_ok(self):
        with self._timer_lock:
            self._loads += 1
        self._disarm_timeout()

    def _timed_out(self, armed_at):
        """The 20 s load timeout: offline only when no page has loaded since it was armed."""
        with self._timer_lock:
            moot = self._loads > armed_at
        if moot:
            return
        self._log("load timed out")
        self.show_offline()

    def show_offline(self):
        self._log("offline page shown")
        self.load(self.local_url("offline"))

    def load(self, url):
        self.window.load_url(url)

    # -- what the window shows --------------------------------------------------------------------------------

    def home(self):
        """The first page: sign-in without a credential or person session, else the hosted inbox."""
        if not self.services.signed_in() or not self.services.has_person_session():
            query = "person=1" if self.services.signed_in() else ""
            self.load(self.local_url("sign-in", query))
            return
        self.open_hosted("/app/inbox")

    def service_base(self):
        return self.service_url().rstrip("/")

    def open_hosted(self, path="/app/inbox", thread_id=None):
        """A hosted page: directly while this run's app-mode session lasts, else through a fresh
        single-use handoff (§16.10), then on to ``path``. A failure shows the offline page."""
        self.refresh_navigation()
        if thread_id is not None:
            path = f"/app/conversations/{thread_id}"
        self.refresh_install_token()
        if self.handed_off:
            url = self.service_base() + path
            self.last_hosted = url
            self._arm_timeout()
            self.load(url)
            return True
        try:
            url = self.services.handoff_url()
        except Exception as exc:  # noqa: BLE001 - offline, or the session ended
            log.warning("handoff failed: %s", type(exc).__name__)
            if not self.services.has_person_session():
                self.load(self.local_url("sign-in", "person=1"))
            else:
                self.show_offline()
            return False
        self.handed_off = True
        self.pending_path = path if path != "/app/inbox" else None
        self.last_hosted = url
        self._arm_timeout()
        self.load(url)
        return True

    def open_section(self, section):
        if section in ("this-computer", "settings"):
            self.load(self.local_url(section))
            return _result()
        if section == "agents":
            return _result(self.open_hosted("/app/agents"))
        return _result(self.open_hosted("/app/inbox"))

    def retry(self):
        if self.services.signed_in() and self.services.has_person_session():
            return _result(self.open_hosted("/app/inbox"), "Still can't reach RainCLI.")
        self.load(self.local_url("sign-in"))
        return _result()

    def show(self):
        if self.window is not None:
            self.window.show()
            self.window.restore()

    def on_closing(self):
        """Closing the window hides it; Quit (the tray) exits."""
        if self.quitting:
            return True
        self.window.hide()
        return False

    def destroy(self):
        self.quitting = True
        if self.window is not None:
            self.window.destroy()

    # -- sign-in and sign-out --------------------------------------------------------------------------------

    def sign_in(self, request, password):
        """The local sign-in page's form. The password goes to ``services.sign_in`` (``login.login``) only.
        ``request["new_machine"]`` is the user's press of "Set up this computer as a new machine"
        (§16.17): the password is asked for again, never kept from the refused attempt."""
        from .. import login
        from .services import StaleCredential
        was_signed_in = self.services.signed_in()
        try:
            self.services.sign_in(str(request.get("email") or "").strip(), password,
                                  machine_name=str(request.get("machine_name") or "").strip(),
                                  team=request.get("team") or None, replace=bool(request.get("replace")),
                                  again=bool(request.get("again")), new_machine=request.get("new_machine") is True)
        except StaleCredential as exc:  # §16.17 item 4: say why, and offer a new machine; never automatic
            return _result(False, str(exc), code="stale_credential", reason=exc.reason, offer_new_machine=True,
                           offer=login.OFFER, machine_name=self.services.offer_machine_name())
        except login.SetAsideRefused as exc:  # §16.18 V3: an old RainCLI window still holds a queue
            return _result(False, str(exc), code="set_aside_refused", offer_new_machine=True,
                           offer=login.OFFER, machine_name=str(request.get("machine_name") or ""))
        except login.TeamChoiceRequired as exc:
            return _result(False, "Choose a team, enter your password again and sign in.", code="team_choice_required",
                           teams=[{"slug": t["slug"], "name": t.get("name", t["slug"])} for t in exc.teams or []])
        except login.NameInUse:
            return _result(False, "You already have a machine with that name. Tick Replace to replace it, "
                                  "or choose another name.", code="name_in_use")
        except login.LoginError as exc:
            return _result(False, str(exc), code=getattr(exc, "code", "error"))
        except Exception as exc:  # noqa: BLE001 - shown, never the request
            return _result(False, f"Sign-in failed: {type(exc).__name__}: {exc}", code="error")
        finally:
            password = None  # noqa: F841 - drop the reference
        fresh = not was_signed_in or bool(request.get("again")) or request.get("new_machine") is True
        self.services.host.resume()
        self.refresh_navigation()
        self.handed_off = False  # sign-in rotates the install token: the old app session is gone
        if fresh and self._on_fresh_sign_in is not None:
            self._later(self._on_fresh_sign_in)
        self.open_hosted("/app/inbox")
        return _result(message="Signed in.")

    def sign_out(self):
        handle = self.services.machine_handle() or "this computer"
        if self._confirm is not None and not self._confirm(
                "Sign out", f"Sign out {handle}?\n\nIt is revoked on the server, and its credential and messaging "
                            "session are deleted from this computer. Queues are kept."):
            return _result(False, "")
        try:
            self.services.sign_out()
        except Exception as exc:  # noqa: BLE001 - the credential is kept (§15.8 M9)
            return _result(False, f"Signing out failed, so the credential was kept: {exc}")
        self.clear_profile()
        self.handed_off = False
        self.refresh_install_token()  # rotated by sign-out (§16.14 S3)
        self.load(self.local_url("sign-in"))
        return _result(message="Signed out.")

    def clear_profile(self):
        """Sign-out clears the private WebView2 profile: cookies now, the folder before the next window."""
        try:
            self.window.clear_cookies()
        except Exception:  # noqa: BLE001
            pass
        mark_profile_for_reset(self.profile_dir)

