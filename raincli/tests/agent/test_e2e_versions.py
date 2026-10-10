"""The Windows app e2e's versions come from the source (scripts/e2e_versions.py), never below the minimum safe
version, so the client never refuses the targets the test pushes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from raincli_agent import __version__, security

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("e2e_versions", ROOT / "scripts" / "e2e_versions.py")
e2e_versions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_versions)


def parse(version):
    return tuple(int(part) for part in version.split("."))


def test_versions_start_at_the_source_or_the_minimum_safe_version_and_step_by_one():
    old, new, broken = e2e_versions.versions(ROOT)
    assert parse(old) == max(parse(__version__), parse(security.MIN_SAFE_VERSION))
    assert parse(new) == parse(old)[:2] + (parse(old)[2] + 1,)
    assert parse(broken) == parse(old)[:2] + (parse(old)[2] + 2,)
    assert parse(old) >= (0, 5, 0)  # the routing floor (§16.12 C1)


def test_a_minimum_above_the_source_version_wins(tmp_path):
    agent = tmp_path / "raincli" / "raincli_agent"
    agent.mkdir(parents=True)
    (agent / "__init__.py").write_text('__version__ = "0.5.3"\n')
    (agent / "security.py").write_text('MIN_SAFE_VERSION = "0.6.0"\n')
    assert e2e_versions.versions(tmp_path) == ("0.6.0", "0.6.1", "0.6.2")


def test_no_test_version_is_hard_coded_in_the_e2e_or_its_workflow():
    import re

    script = (ROOT / "scripts" / "windows-app-e2e.py").read_text("utf-8")
    workflow = (ROOT / ".github" / "workflows" / "windows-app-e2e.yml").read_text("utf-8")
    assert "OLD, NEW, BROKEN = e2e_versions.versions(ROOT)" in script
    assert not re.search(r"--version [0-9]+\.[0-9]+\.[0-9]+", workflow)
    assert "scripts/e2e_versions.py --github-output" in workflow
