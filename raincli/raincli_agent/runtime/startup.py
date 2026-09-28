"""Opt-in per-user login startup. No elevation, system service or credential argv."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from ..errors import ConfigError
from ..fsutil import atomic_write_bytes, ensure_private_dir
from .service import file_sha256, load_runtime

NAME = "raincli-runtime.service"
REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRY_VALUE = "RainCLI"


def command(config):
    from .updates import default_root
    managed = default_root() / "launch.py"
    executable = Path(getattr(sys, "_base_executable", sys.executable) if managed.exists() else sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    prefix = [str(executable), str(managed)] if managed.exists() else [str(executable), "-m", "raincli_agent"]
    return [*prefix, "runtime", "run", "--config", str(Path(config).resolve())]


def windows_command_line(argv):
    """Quote every argument, as CommandLineToArgvW parses it back.

    Always quoting (unlike subprocess.list2cmdline) keeps each absolute path a
    single argument whatever it contains, and makes the Run value predictable."""
    parts = []
    for arg in argv:
        if '"' in arg or any(ord(ch) < 32 for ch in arg):
            raise ConfigError("startup paths cannot contain quotes or control characters")
        # Backslashes are literal except before the closing quote, where they double.
        trailing = len(arg) - len(arg.rstrip("\\"))
        parts.append('"' + arg + "\\" * trailing + '"')
    return " ".join(parts)


def systemd_quote(value):
    if any(ord(ch) < 32 for ch in value):
        raise ConfigError("startup paths/environment cannot contain control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def config_digest(config):
    """Digest of the runtime config and its connector configs, for the unit file.

    A changed mapping then changes the unit, so reinstalling restarts the service."""
    path, _, configs = load_runtime(config)
    digest = hashlib.sha256()
    for part in [file_sha256(path)] + [binding["config_sha256"] for _, _, _, binding in configs]:
        digest.update(part.encode())
    return digest.hexdigest()


def systemd_unit(config, digest=None):
    argv = " ".join(systemd_quote(a) for a in command(config))
    # Keep the operator's PATH so an explicitly configured Herdr executable
    # installed under ~/.local/bin is found after user login.
    path = systemd_quote("PATH=" + os.environ.get("PATH", "/usr/bin:/bin")).replace("$$", "$")
    stamp = f"# config-sha256: {digest}\n" if digest else ""
    # KillMode=mixed: SIGTERM goes to the launcher/runtime only, which stops
    # each connector between iterations; stragglers get SIGKILL at the timeout.
    return ("[Unit]\nDescription=RainCLI mapped agent runtime\n\n"
            "[Service]\nType=simple\n" + f"ExecStart={argv}\nEnvironment={path}\n"
            "Restart=on-failure\nRestartSec=10\nKillMode=mixed\nTimeoutStopSec=150\n" + stamp + "\n[Install]\nWantedBy=default.target\n")


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", "--user", *args], check=check, stdin=subprocess.DEVNULL)


def install(config):
    digest = config_digest(config)  # fail before modifying startup if mappings are invalid
    if os.name == "nt":
        import winreg
        value = windows_command_line(command(config))
        if len(value) > 260:
            raise ConfigError("Windows login command exceeds 260 characters; use shorter install/config paths")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, value)
        return "Installed current-user Windows logon startup; run runtime run to start now."
    if sys.platform != "linux":
        raise ConfigError("startup installation supports Linux and Windows")
    directory = Path.home() / ".config/systemd/user"
    ensure_private_dir(directory)
    unit = systemd_unit(config, digest).encode("utf-8")
    try:
        changed = (directory / NAME).read_bytes() != unit
    except FileNotFoundError:
        changed = True
    if changed:
        atomic_write_bytes(directory / NAME, unit)
        systemctl("daemon-reload")
    systemctl("enable", NAME)
    if systemctl("is-active", "--quiet", NAME, check=False).returncode != 0:
        systemctl("start", NAME)
        return "Installed and started Linux user service; starts at user login."
    if changed:
        # The running runtime stops its connectors gracefully before exiting.
        systemctl("restart", NAME)
        return "Updated and restarted the Linux user service; starts at user login."
    return "Linux user service already installed and running; unchanged."


def remove():
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, REGISTRY_VALUE)
        except FileNotFoundError:
            pass
        return "Removed Windows logon startup; an already running runtime continues until stopped."
    if sys.platform != "linux":
        raise ConfigError("startup removal supports Linux and Windows")
    systemctl("disable", "--now", NAME, check=False)  # tolerate an already removed unit
    (Path.home() / ".config/systemd/user" / NAME).unlink(missing_ok=True)
    systemctl("daemon-reload")
    return "Stopped and removed the Linux user service."
