"""Opt-in per-user login startup. No elevation, system service or credential argv."""
import os
from pathlib import Path
import subprocess
import sys

from ..errors import ConfigError
from ..fsutil import atomic_write_bytes, ensure_private_dir
from .service import load_runtime

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


def systemd_quote(value):
    if any(ord(ch) < 32 for ch in value):
        raise ConfigError("startup paths/environment cannot contain control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def systemd_unit(config):
    argv = " ".join(systemd_quote(a) for a in command(config))
    # Keep the operator's PATH so an explicitly configured Herdr executable
    # installed under ~/.local/bin is found after user login.
    path = systemd_quote("PATH=" + os.environ.get("PATH", "/usr/bin:/bin")).replace("$$", "$")
    return ("[Unit]\nDescription=RainCLI mapped agent runtime\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\n" + f"ExecStart={argv}\nEnvironment={path}\n"
            "Restart=on-failure\nRestartSec=10\nTimeoutStopSec=60\n\n[Install]\nWantedBy=default.target\n")


def install(config):
    load_runtime(config)  # fail before modifying startup if mappings are invalid
    if os.name == "nt":
        import winreg
        value = subprocess.list2cmdline(command(config))
        if len(value) > 260:
            raise ConfigError("Windows login command exceeds 260 characters; use shorter install/config paths")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, value)
        return "Installed current-user Windows logon startup; run runtime run to start now."
    if sys.platform != "linux":
        raise ConfigError("startup installation supports Linux and Windows")
    directory = Path.home() / ".config/systemd/user"
    ensure_private_dir(directory)
    atomic_write_bytes(directory / NAME, systemd_unit(config).encode("utf-8"))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", NAME], check=True)
    return "Installed and started Linux user service; starts at user login."


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
    subprocess.run(["systemctl", "--user", "disable", "--now", NAME], check=True)
    (Path.home() / ".config/systemd/user" / NAME).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return "Stopped and removed the Linux user service."
