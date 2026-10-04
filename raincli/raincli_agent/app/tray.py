"""``RainCLI-app.exe --background``: the tray (15.5, 15.8 H4). A thin front end.

- the icon (ready, offline, updating, error) and a status window, read from the
  runtime's local status record (``app.status``);
- the menu: Status, Open log, Pause/Resume, Sign out, Quit;
- a first-run sign-in dialog calling ``login.login``; migration (``migrate``)
  runs first and a machine with a credential never signs in again;
- the runtime runs as this process's child (``winapp.AppHost``); when another
  version becomes current the tray exits with ``SWITCH_EXIT`` for the stub.

``pystray`` and ``Pillow`` are imported here only. Tk runs on the main thread;
the icon menu and network work post callbacks to it through a queue.
"""
import os
from pathlib import Path
import queue
import sys
import threading

from .. import __version__
from ..config import Secret, default_config_path
from ..runtime import winapp
from . import status as model

COLOURS = {"ready": "#2e9d5b", "offline": "#8a8f98", "updating": "#d29a1e", "error": "#c23b3b"}
TITLE = "RainCLI"


def icon_image(state):
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=COLOURS[state])
    draw.ellipse((22, 22, 42, 42), fill="#ffffff")
    return image


class Tray:
    def __init__(self, root_dir):
        import tkinter as tk
        self.tk = tk
        self.root_dir = root_dir
        self.agent_config, self.runtime_config = winapp.paths(root_dir)
        self.state_dir = Path(root_dir or Path(self.agent_config).parent) / "state"
        self.host = winapp.AppHost(root_dir, __version__, self.runtime_config,
                                   log_path=self.state_dir / "runtime.log")
        self.calls = queue.Queue()
        self.exit_code = 0
        self.handle = None
        self.agents = None
        self.window = self.dialog = None
        self.migrating = False
        self.ui = tk.Tk()
        self.ui.withdraw()
        self.ui.title(TITLE)
        self.icon = None
        self.icon_state = None

    # -- plumbing ---------------------------------------------------------------------------

    def post(self, fn, *args):
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
        while True:
            try:
                fn, args = self.calls.get_nowait()
            except queue.Empty:
                break
            fn(*args)
        self.ui.after(200, self.pump)

    def supervise(self):
        if not self.migrating:
            code = self.host.step() if self.signed_in() else None
            if code is not None:
                return self.quit(code)
        self.refresh_icon()
        self.ui.after(2000, self.supervise)

    def signed_in(self):
        return os.path.exists(self.agent_config) and os.path.exists(self.runtime_config)

    def status(self):
        from ..runtime.service import status
        try:
            return status(self.runtime_config)
        except Exception:  # noqa: BLE001 - no runtime config yet
            return {"status": "not_observed"}

    def refresh_icon(self):
        state = "offline" if self.host.paused else model.icon_state(self.status())
        if self.icon is not None and state != self.icon_state:
            self.icon_state = state
            self.icon.icon = icon_image(state)
            self.icon.title = f"{TITLE}: {state}"
        if self.window is not None:
            self.window_text.set("\n".join(self.describe()))

    def describe(self):
        return model.describe(self.status(), handle=self.handle, version=__version__,
                              update_mode=winapp.update_mode(self.root_dir) if self.root_dir else None,
                              agents=self.agents, paused=self.host.paused)

    # -- menu -----------------------------------------------------------------------------------

    def menu(self):
        import pystray
        item = pystray.MenuItem
        return pystray.Menu(
            item("Status", lambda: self.post(self.show_status), default=True),
            item("Open log", lambda: self.post(self.open_log)),
            item(lambda _: "Resume" if self.host.paused else "Pause", lambda: self.post(self.toggle_pause)),
            item("Sign out", lambda: self.post(self.sign_out)),
            item("Quit", lambda: self.post(self.quit, 0)))

    def show_status(self):
        tk = self.tk
        if self.window is None:
            self.window = tk.Toplevel(self.ui)
            self.window.title(f"{TITLE} status")
            self.window_text = tk.StringVar(value="\n".join(self.describe()))
            tk.Label(self.window, textvariable=self.window_text, justify="left", anchor="w",
                     padx=16, pady=12, font=("Segoe UI", 10)).pack(fill="both")
            self.window.protocol("WM_DELETE_WINDOW", self.close_status)
            self.fetch_directory()
        self.window.deiconify()
        self.window.lift()

    def close_status(self):
        self.window.destroy()
        self.window = None

    def fetch_directory(self):
        def fetch():
            from ..api import ApiClient
            from ..config import load_config
            client = ApiClient.from_config(load_config(self.agent_config), timeout=10, max_attempts=1)
            handle = client.me()["agent"]["handle"]
            mine = [m for m in client.agents() if m.get("handle") == handle]
            return handle, (mine[0].get("agents") or []) if mine else []

        def done(result, error):
            if result:
                self.handle, self.agents = result
            self.refresh_icon()
        if self.signed_in():
            self.background(fetch, done=done)

    def open_log(self):
        log = self.state_dir / "runtime.log"
        if hasattr(os, "startfile") and log.exists():
            os.startfile(str(log))  # noqa: S606 - the user's own private log

    def toggle_pause(self):
        if self.host.paused:
            self.host.resume()
        else:
            self.background(self.host.pause)

    def sign_out(self):
        from tkinter import messagebox
        from .. import login
        try:
            _, handle = login.describe(self.agent_config)
        except Exception:  # noqa: BLE001
            return
        name = handle or "this machine"
        if not messagebox.askyesno(TITLE, f"Sign out {name}?\n\nIt is revoked on the server and its credential "
                                          "is deleted from this computer. Queues are kept."):
            return

        def done(result, error):
            if error is not None:
                if messagebox.askyesno(TITLE, f"Signing out failed: {error}\n\nThe credential was kept. Delete it "
                                              "from this computer only? (Then revoke the machine on the website.)"):
                    self.migrating = True
                    self.background(lambda: (self.host.stop(), login.logout(self.agent_config, local_only=True)),
                                    done=after)
                return
            after(result, None)

        def after(result, error):
            self.migrating = False
            self.handle = self.agents = None
            self.sign_in()
        self.migrating = True
        self.background(lambda: (self.host.stop(), login.logout(self.agent_config))[1], done=done)

    def quit(self, code=0):
        self.exit_code = code
        if code != winapp.SWITCH_EXIT:
            self.host.stop()
        if self.icon is not None:
            self.icon.stop()
        self.ui.quit()

    # -- first run: migration, then sign-in -----------------------------------------------------

    def first_run(self):
        from ..migrate import Migration
        self.migrating = True
        stop = threading.Event()

        def start(runtime_config):
            self.runtime_config = runtime_config
            self.host.config = str(runtime_config)
            self.host.start()
            for _ in range(60):
                if stop.wait(1):
                    return False
                if model.ready(self.status()):
                    return True
            return False

        def notify(message):
            self.post(self.notice, message, stop)

        def migrate():
            return Migration(app_root=self.root_dir).run(start, notify=notify, cancelled=stop.is_set)

        def done(result, error):
            self.migrating = False
            self.agent_config, self.runtime_config = winapp.paths(self.root_dir)
            self.host.config = str(self.runtime_config)
            if error is not None:
                self.notice(f"Moving this machine to the app failed: {error}", None)
            elif result["status"] in ("busy", "waiting"):
                self.ui.after(10000, self.first_run)  # an old window still runs: try again
            elif not self.signed_in():
                self.sign_in()
        self.background(migrate, done=done)

    def notice(self, message, cancel_event):
        from tkinter import messagebox
        if cancel_event is None:
            messagebox.showinfo(TITLE, message)
        elif not messagebox.askokcancel(TITLE, message + "\n\nOK keeps waiting; Cancel stops."):
            cancel_event.set()

    def sign_in(self):
        if self.dialog is None:
            self.dialog = SignInDialog(self)
        self.dialog.show()

    # -- run -----------------------------------------------------------------------------------------

    def run(self):
        import pystray
        self.icon_state = "offline"
        self.icon = pystray.Icon("RainCLI", icon_image("offline"), f"{TITLE}: offline", self.menu())
        self.icon.run_detached()
        self.ui.after(0, self.pump)
        self.ui.after(0, self.first_run)
        self.ui.after(2000, self.supervise)
        self.ui.mainloop()
        return self.exit_code


class SignInDialog:
    """Email, machine name and password; calls ``login.login``. After any refusal
    the password field is cleared: the tray never retries with a remembered
    password (15.8 M2), and nothing about the request is logged (M3)."""

    def __init__(self, tray):
        from .. import login
        tk = tray.tk
        self.tray, self.login, self.team, self.replace = tray, login, None, False
        self.top = tk.Toplevel(tray.ui)
        self.top.title("Sign in to RainCLI")
        self.top.protocol("WM_DELETE_WINDOW", self.top.withdraw)
        frame = tk.Frame(self.top, padx=16, pady=12)
        frame.pack(fill="both")
        self.email, self.name, self.password = tk.StringVar(), tk.StringVar(value=login.default_machine_name()), \
            tk.StringVar()
        self.message = tk.StringVar(value="Sign in with your RainCLI account to add this computer to your team.")
        tk.Label(frame, textvariable=self.message, wraplength=360, justify="left").grid(row=0, columnspan=2, sticky="w")
        for row, (label, var, show) in enumerate((("Email", self.email, None), ("Machine name", self.name, None),
                                                  ("Password", self.password, "•")), 1):
            tk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            tk.Entry(frame, textvariable=var, show=show or "", width=36).grid(row=row, column=1, pady=4)
        self.teams = tk.StringVar()
        self.team_menu = None
        self.frame = frame
        self.button = tk.Button(frame, text="Sign in", command=self.submit)
        self.button.grid(row=5, column=1, sticky="e", pady=8)

    def show(self):
        self.top.deiconify()
        self.top.lift()

    def submit(self):
        password = Secret(self.password.get())
        self.password.set("")
        if not password.reveal():
            self.message.set("Enter your password.")
            return
        email, name = self.email.get().strip(), self.name.get().strip()
        team = self.teams.get() or self.team
        replace = self.replace
        self.button.config(state="disabled")
        self.message.set("Signing in…")
        root = self.tray.root_dir
        config_path = winapp.read_settings(root).get("agent_config") if root else None

        def work():
            plan = self.login.prepare(config_path or default_config_path(), force=False)
            return self.login.login(email, password, plan=plan, machine_name=name, team=team or None,
                                    replace=replace)
        self.tray.background(work, done=self.done)

    def done(self, result, error):
        from tkinter import messagebox
        login = self.login
        self.button.config(state="normal")
        self.replace = False
        if error is None:
            self.top.withdraw()
            self.tray.agent_config, self.tray.runtime_config = winapp.paths(self.tray.root_dir)
            self.tray.host.config = str(self.tray.runtime_config)
            self.tray.handle = result["handle"]
            self.tray.host.resume()
            return
        if isinstance(error, login.TeamChoiceRequired) and error.teams:
            slugs = [t["slug"] for t in error.teams]
            self.teams.set(slugs[0])
            if self.team_menu is not None:
                self.team_menu.destroy()
            self.tray.tk.Label(self.frame, text="Team").grid(row=4, column=0, sticky="w")
            self.team_menu = self.tray.tk.OptionMenu(self.frame, self.teams, *slugs)
            self.team_menu.grid(row=4, column=1, sticky="w")
            self.message.set("Choose a team, enter your password again and sign in.")
        elif isinstance(error, login.NameInUse):
            name = self.name.get().strip()
            if messagebox.askyesno("Replace machine", f"Replace machine {name}?\n\nIts current credential is "
                                                      "revoked. Choose No to pick another name."):
                self.replace = True
                self.message.set(f"Enter your password again to replace machine {name}.")
            else:
                self.message.set("Choose another machine name.")
        elif isinstance(error, login.LoginError):
            self.message.set(str(error))
        else:
            self.message.set(f"Sign-in failed: {error}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["--background"]:
        print("usage: RainCLI-app.exe --background", file=sys.stderr)
        return 2
    return Tray(winapp.app_root()).run()


if __name__ == "__main__":
    sys.exit(main())
