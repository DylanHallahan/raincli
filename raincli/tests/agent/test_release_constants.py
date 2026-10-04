"""Protocol §15.8 M11: no release-host override in shipped code.

``HOSTS``/``ALLOWED_HOSTS``, ``API`` and ``REPO`` are module constants built only from
literals, never reassigned or mutated anywhere in the client, and no test hook
(``_build_test``, ``TEST_RELEASE_BASE``, ``TEST_CERT_SHA256``) exists in the package.
The Windows e2e reaches its fake release endpoint through a hosts-file entry and a
test root CA on the real hostnames instead. ``packaging/windows/verify_bundle.py``
checks the same in the built bundle.
"""
import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "raincli_agent"
NAMES = {"HOSTS", "ALLOWED_HOSTS", "API", "REPO"}
HOOKS = ("_build_test", "TEST_RELEASE_BASE", "TEST_CERT_SHA256")
MUTATORS = {"add", "update", "discard", "remove", "pop", "clear", "__setitem__", "__ior__", "append", "extend"}
CANONICAL_REPO = "DylanHallahan/raincli"
SOURCES = sorted(PKG.rglob("*.py"))


def _constant(node, known):
    """True when ``node`` is built only from literals and earlier constants of the same module."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id in known
    if isinstance(node, ast.Attribute):  # e.g. updates.REPO in another module
        return node.attr in NAMES and isinstance(node.value, ast.Name)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _constant(node.left, known) and _constant(node.right, known)
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return all(_constant(e, known) for e in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set"):
        return not node.keywords and all(_constant(a, known) for a in node.args)
    return False


def _targets(node):
    if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            yield from (t for t in ast.walk(target) if isinstance(t, (ast.Name, ast.Attribute)))


def test_the_package_was_found():
    assert (PKG / "runtime" / "updates.py").is_file() and len(SOURCES) > 20


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PKG)))
def test_release_constants_are_literal_and_never_rebound(path):
    tree = ast.parse(path.read_text("utf-8"), str(path))
    module_level = {id(n) for n in tree.body}
    known = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            assert not NAMES & set(node.names), f"{path.name}: rebinds {NAMES & set(node.names)}"
        for target in _targets(node):
            name = target.id if isinstance(target, ast.Name) else target.attr
            if name not in NAMES:
                continue
            assert id(node) in module_level and isinstance(target, ast.Name) and not isinstance(node, ast.AugAssign), (
                f"{path.name}:{node.lineno}: {name} may only be bound once, at module level")
            assert name not in known, f"{path.name}:{node.lineno}: {name} is bound twice"
            assert _constant(node.value, known), f"{path.name}:{node.lineno}: {name} is not built from literals"
            known.add(name)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS
                and isinstance(node.func.value, (ast.Name, ast.Attribute))):
            owner = node.func.value.id if isinstance(node.func.value, ast.Name) else node.func.value.attr
            assert owner not in NAMES, f"{path.name}:{node.lineno}: mutates {owner}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            assert not any("_build_test" in n for n in names), f"{path.name}:{node.lineno}: imports a test hook"


def test_no_test_hooks_in_the_client():
    assert not [p for p in PKG.rglob("*") if "_build_test" in p.name]
    for path in SOURCES:
        text = path.read_text("utf-8")
        for hook in HOOKS:
            assert hook not in text, f"{path.relative_to(PKG)} mentions {hook}"


def test_release_values():
    from raincli_agent.runtime import updates

    assert updates.REPO == CANONICAL_REPO
    assert updates.API == "https://api.github.com/repos/" + CANONICAL_REPO
    assert set(updates.HOSTS) <= {"api.github.com", "codeload.github.com", "github.com",
                                  "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
    assert not any("*" in h or h.startswith(".") for h in updates.HOSTS)


def test_app_release_hosts_are_exact_when_present():
    try:
        from raincli_agent.runtime import winapp
    except ImportError:
        pytest.skip("the Windows app updater is not in this checkout")
    hosts = getattr(winapp, "ALLOWED_HOSTS", None) or getattr(winapp, "HOSTS")
    assert set(hosts) == {"api.github.com", "github.com", "objects.githubusercontent.com",
                          "release-assets.githubusercontent.com"}
    assert getattr(winapp, "REPO", CANONICAL_REPO) == CANONICAL_REPO
