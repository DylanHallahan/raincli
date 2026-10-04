# PyInstaller spec for the Windows app (protocol §15.5, §15.8). Run through build.py, which sets
# RAINCLI_BUILD_SRC (a copy of raincli/ with the version applied) and RAINCLI_BUILD_VERSION.
#
#   dist/RainCLI-<X.Y.Z>/    the onedir version folder: RainCLI-app.exe (tray) and raincli.exe (CLI)
#   dist/RainCLI.exe          the stable stub (onefile, windowed)
#   dist/raincli-shim.exe     the PATH shim (onefile, console); build.py installs it as bin\raincli.exe
import os
from pathlib import Path

HERE = Path(SPECPATH)
SRC = Path(os.environ["RAINCLI_BUILD_SRC"])
VERSION = os.environ["RAINCLI_BUILD_VERSION"]
SKILL = [(str(p), str(p.parent.relative_to(SRC))) for p in (SRC / "raincli_agent/skill").rglob("*")
         if p.is_file() and p.suffix in (".md", ".yaml")]
# The CLI is standard-library-only; the server and the build tools never enter a bundle.
NEVER = ["raincli_server", "fastapi", "starlette", "uvicorn", "sqlalchemy", "psycopg", "alembic", "jinja2",
         "pytest", "PyInstaller", "setuptools", "pip"]


def analysis(script, excludes=()):
    return Analysis([str(HERE / script)], pathex=[str(SRC)], datas=SKILL, excludes=NEVER + list(excludes),
                    noarchive=False, optimize=0)


app = analysis("entry_app.py")
cli = analysis("entry_cli.py", excludes=["pystray", "PIL", "tkinter", "_tkinter"])
stub = analysis("entry_stub.py", excludes=["pystray", "PIL"])
shim = Analysis([str(HERE / "shim.py")], excludes=NEVER + ["raincli_agent", "pystray", "PIL", "tkinter"])

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="RainCLI-app", console=False,
              upx=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="raincli", console=True, upx=False)
COLLECT(app_exe, app.binaries, app.datas, cli_exe, cli.binaries, cli.datas, upx=False,
        name=f"RainCLI-{VERSION}")

EXE(PYZ(stub.pure), stub.scripts, stub.binaries, stub.datas, [], name="RainCLI", console=False, upx=False,
    runtime_tmpdir=None)
EXE(PYZ(shim.pure), shim.scripts, shim.binaries, shim.datas, [], name="raincli-shim", console=True, upx=False,
    runtime_tmpdir=None)
