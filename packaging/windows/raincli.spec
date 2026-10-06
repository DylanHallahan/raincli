# PyInstaller spec for the Windows app (protocol §15.5, §15.8). Run through build.py, which sets
# RAINCLI_BUILD_SRC (a copy of raincli/ with the version applied) and RAINCLI_BUILD_VERSION.
#
#   dist/RainCLI-<X.Y.Z>/    the onedir version folder: RainCLI-app.exe (window and tray) and raincli.exe (CLI)
#   dist/RainCLI-stub/        the stable stub, onedir: RainCLI.exe and _internal (installed at the root)
#   dist/RainCLI-bin/         the PATH shim, onedir: raincli-shim.exe and _internal; build.py installs
#                             the folder as bin\ and the exe as bin\raincli.exe
# Nothing is onefile, so nothing runs from %TEMP% (protocol §15.9).
import os
from pathlib import Path

HERE = Path(SPECPATH)
SRC = Path(os.environ["RAINCLI_BUILD_SRC"])
VERSION = os.environ["RAINCLI_BUILD_VERSION"]
SKILL = [(str(p), str(p.parent.relative_to(SRC))) for p in (SRC / "raincli_agent/skill").rglob("*")
         if p.is_file() and p.suffix in (".md", ".yaml")]
# The app window's bundled local pages (protocol §16.10): RainCLI-app.exe only.
LOCAL_PAGES = [(str(p), str(p.parent.relative_to(SRC))) for p in (SRC / "raincli_agent/app/local").iterdir()
               if p.is_file() and p.suffix in (".html", ".css", ".js")]
# The tray icons, one per state (raincli_agent.app.status.ICON_STATES).
TRAY_ICONS = [(str(p), str(p.parent.relative_to(SRC))) for p in (SRC / "raincli_agent/app/icons").glob("tray-*-64.png")]
ICON = str(HERE / "app.ico")  # RainCLI-app.exe, the RainCLI.exe stub and raincli.exe
GUI = ["pystray", "PIL", "webview", "clr_loader", "pythonnet", "clr", "bottle", "proxy_tools"]
# The CLI is standard-library-only; the server and the build tools never enter a bundle.
NEVER = ["raincli_server", "fastapi", "starlette", "uvicorn", "sqlalchemy", "psycopg", "alembic", "jinja2",
         "pytest", "PyInstaller", "setuptools", "pip"]


def analysis(script, excludes=(), hidden=(), datas=()):
    return Analysis([str(HERE / script)], pathex=[str(SRC)], datas=SKILL + list(datas), excludes=NEVER + list(excludes),
                    hiddenimports=list(hidden), noarchive=False, optimize=0)


# The app imports its GUI modules lazily; pystray and pywebview pick their backends at run time
# (pyinstaller-hooks-contrib collects pywebview's scripts and WebView2 loader). No tkinter (§16.10).
app = analysis("entry_app.py", excludes=["tkinter", "_tkinter"], datas=LOCAL_PAGES + TRAY_ICONS,
               hidden=["raincli_agent.app.tray", "raincli_agent.app.window", "raincli_agent.app.services",
                       "raincli_agent.app.policy", "raincli_agent.app.shortcut", "pystray", "pystray._win32", "PIL.Image", "PIL.PngImagePlugin",
                       "webview", "webview.platforms.winforms", "webview.platforms.edgechromium"])
cli = analysis("entry_cli.py", excludes=GUI + ["tkinter", "_tkinter"])
stub = analysis("entry_stub.py", excludes=GUI + ["tkinter", "_tkinter"])
shim = Analysis([str(HERE / "shim.py")], excludes=NEVER + GUI + ["raincli_agent", "tkinter"])

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="RainCLI-app", console=False,
              upx=False, icon=ICON)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="raincli", console=True, upx=False,
              icon=ICON)
COLLECT(app_exe, app.binaries, app.datas, cli_exe, cli.binaries, cli.datas, upx=False,
        name=f"RainCLI-{VERSION}")

stub_exe = EXE(PYZ(stub.pure), stub.scripts, [], exclude_binaries=True, name="RainCLI", console=False, upx=False,
               icon=ICON)
COLLECT(stub_exe, stub.binaries, stub.datas, upx=False, name="RainCLI-stub")
# A different EXE name from the CLI's raincli.exe, so the two never collide in the work path.
shim_exe = EXE(PYZ(shim.pure), shim.scripts, [], exclude_binaries=True, name="raincli-shim", console=True, upx=False)
COLLECT(shim_exe, shim.binaries, shim.datas, upx=False, name="RainCLI-bin")
