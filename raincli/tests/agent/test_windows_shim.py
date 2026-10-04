"""packaging/windows/shim.py: bin\\raincli.exe forwards to install.json's current version (§15.8 L2, M6)."""
import importlib.util
import json
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[3] / "packaging" / "windows" / "shim.py"
spec = importlib.util.spec_from_file_location("raincli_shim", SHIM)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


def layout(tmp_path, install, versions=("0.4.0",)):
    for v in versions:
        (tmp_path / "versions" / v).mkdir(parents=True)
        (tmp_path / "versions" / v / "raincli.exe").write_bytes(b"")
    if install is not None:
        (tmp_path / "install.json").write_text(install if isinstance(install, str) else json.dumps(install))
    return tmp_path


def test_forwards_arguments_and_exit_code(tmp_path):
    root = layout(tmp_path, {"current": "0.4.1", "previous": "0.4.0", "probation": None}, ("0.4.0", "0.4.1"))
    calls = []
    code = shim.main(["login", "--email", "a@example.test"], root=root, run=lambda cmd: calls.append(cmd) or 7)
    assert code == 7
    assert calls == [[str(root / "versions" / "0.4.1" / "raincli.exe"), "login", "--email", "a@example.test"]]


def test_install_root_is_the_parent_of_bin(tmp_path):
    assert shim.install_root(tmp_path / "bin" / "raincli.exe") == tmp_path.resolve()


@pytest.mark.parametrize("install", [None, "not json", "[]", {"current": "v0.4.0"}, {"current": "0.4"},
                                     {"current": "../0.4.0"}, {"current": "0.4.9"}, {"previous": "0.4.0"}])
def test_damaged_installs_are_refused(tmp_path, install, capsys):
    root = layout(tmp_path, install)
    assert shim.main([], root=root, run=lambda cmd: pytest.fail("must not run")) == 1
    assert "Reinstall RainCLI" in capsys.readouterr().err


def test_retries_a_sharing_violation(tmp_path, monkeypatch):
    root = layout(tmp_path, {"current": "0.4.0"})
    real = Path.read_bytes
    attempts = []

    def flaky(self):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(32, "sharing violation")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert shim.current_cli(root, sleep=lambda s: None) == root / "versions" / "0.4.0" / "raincli.exe"
    assert len(attempts) == 3
