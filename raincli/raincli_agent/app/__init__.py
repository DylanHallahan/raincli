"""The Windows tray app and its stable stub (protocol 15.5, 15.8 H4).

A thin front end over the client core: sign-in, sign-out, migration, the
runtime and updates all live in ``raincli_agent``. Only ``tray`` imports
``pystray`` and ``Pillow`` (lazily); ``status`` and ``stub`` are stdlib only.
"""


def main(argv=None):
    """``RainCLI-app.exe`` (packaging entry point): the tray."""
    from .tray import main as tray_main
    return tray_main(argv)
