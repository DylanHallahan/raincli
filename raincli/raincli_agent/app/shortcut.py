"""The Start menu shortcut on installs updated in place from v0.4 (protocol §16.15).

The v0.4 stub rejects the shortcut's empty arguments, and an update in place never replaces the stub. Kept
out of ``runtime.winapp``, which never reads the environment."""
import os
from pathlib import Path
import subprocess

from ..runtime.winapp import STUB, app_log, hidden, read_install_meta

SHORTCUT = Path("Microsoft") / "Windows" / "Start Menu" / "Programs" / "RainCLI" / "RainCLI.lnk"
# Arguments arrive in the environment, never in the script text.
_SHORTCUT_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:RAINCLI_LNK); "
    "if ($s.TargetPath -ine $env:RAINCLI_STUB) { 'foreign' } "
    "elseif ($s.Arguments -eq '--background') { 'unchanged' } "
    "else { $s.Arguments = '--background'; $s.Save(); 'rewritten' }")


def fix_v04_shortcut(root, *, run=subprocess.run, appdata=None, log=app_log):
    """§16.15: an install updated in place keeps its v0.4 stub, which rejects the Start menu shortcut's
    empty arguments. Without ``"stub"`` in install.json, point the shortcut at ``--background`` (the tray
    icon opens the window); the next full install restores it. Returns what happened, or None."""
    if os.name != "nt" and run is subprocess.run:
        return None
    if read_install_meta(root)["stub"] is not None:
        return None
    appdata = appdata or os.environ.get("APPDATA")
    if not appdata:
        return None
    link = Path(appdata) / SHORTCUT
    if not link.is_file():
        return None
    env = dict(os.environ, RAINCLI_LNK=str(link), RAINCLI_STUB=str(Path(root) / STUB))
    try:
        result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                      _SHORTCUT_SCRIPT], env=env, capture_output=True, text=True, timeout=60, **hidden())
        outcome = (result.stdout or "").strip().splitlines()[-1:] or ["failed"]
        outcome = outcome[0] if result.returncode == 0 else "failed"
    except (OSError, subprocess.SubprocessError):
        outcome = "failed"
    if outcome != "unchanged":
        log(root, f"Start menu shortcut for the v0.4 stub: {outcome} ({link.name} --background)")
    return outcome
