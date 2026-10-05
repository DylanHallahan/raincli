"""The packaged skill and SETUP.md must only use commands and flags the real CLI accepts."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

from raincli_agent.cli import build_parser

PKG = Path(__file__).resolve().parents[2]
SKILL = PKG / "raincli_agent" / "skill" / "SKILL.md"
SETUP = PKG.parent / "SETUP.md"

PLACEHOLDERS = {
    "HANDLE": "bob-agent", "<teammate-handle>": "bob-agent", "TEXT": "hello", "PATH|-": "-", "PATH": "/tmp/x",
    "UUID4": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "MSG_ID": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f",
    "CONV_ID": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "ESC_ID": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f",
    "ID": "0b7f3c1e-2a4d-4c6e-9f10-1a2b3c4d5e6f", "FILE.md": "report.md", "DIR": "/tmp/d", "FILENAME": "report.md",
    "S": "30", "URL": "https://raincli.com", "CONNECTOR.json": "/tmp/c.json", "C": "/tmp/c.json",
    "RUNTIME.json": "/tmp/r.json", "ENDPOINT": "bob-agent/reviewer", "NAME": "planner", "N": "1", "SLUG": "acme",
    "MACHINE|@EMAIL": "bob-agent",
}


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    found = set()
    for action in parser._actions:
        found.update(o for o in action.option_strings if o.startswith("--"))
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                found |= _all_option_strings(sub)
    return found


def _expand(template: str) -> list[list[str]]:
    """Turn a usage-style line into concrete argv variants (first alternative, optional parts on)."""
    line = template.split("#", 1)[0].strip()
    line = re.sub(r"\((--[a-z-]+ \S+) \| [^)]*\)", r"\1", line)        # (--body TEXT | --body-file ...) -> first
    line = line.replace("]...", "]")
    variants = [re.sub(r"[\[\]]", "", line), re.sub(r"\s*\[[^\]]*\]", "", line)]
    out = []
    for v in variants:
        for key in sorted(PLACEHOLDERS, key=len, reverse=True):
            v = re.sub(rf"(?<![\w-]){re.escape(key)}(?![\w-])", PLACEHOLDERS[key], v)
        v = v.replace("…", "").strip()
        v = re.sub(r'"\$\(.*?\)"', PLACEHOLDERS["UUID4"], v)  # a quoted command substitution is one argument
        out.append(shlex.split(v)[1:])  # drop leading "raincli"
    return out


def _examples(text: str) -> list[tuple[str, bool]]:
    """(example, inline). Code-block lines are full invocations; inline mentions may name a command only."""
    found = []
    # Pair every fence (```json blocks included) so bash blocks after them are still checked.
    for lang, block in re.findall(r"^```(\w*)\n(.*?)^```", text, re.S | re.M):
        if lang in ("", "bash"):
            found += [(ln.strip(), False) for ln in block.splitlines() if ln.strip().startswith("raincli ")]
    found += [(ref, True) for ref in re.findall(r"`(raincli [^`]+)`", text)]
    return [(e, i) for e, i in found if "--help" not in e and "never `" not in e and not e.rstrip().endswith("…")]


def test_every_flag_mentioned_in_skill_exists():
    known = _all_option_strings(build_parser()) | {"--help", "--skill", "--version", "--config"}
    mentioned = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", SKILL.read_text(encoding="utf-8")))
    assert mentioned - known == set()


@pytest.mark.parametrize("doc", [SKILL, SETUP], ids=["SKILL.md", "SETUP.md"])
def test_documented_raincli_invocations_parse(doc):
    parser = build_parser()
    text = doc.read_text(encoding="utf-8")
    examples = [(e, i) for e, i in _examples(text) if "whoami --config" not in e]  # documented as the wrong form
    assert examples, f"no raincli examples found in {doc.name}"
    for example, inline in examples:
        for argv in _expand(example):
            argv = [a for a in argv if not a.startswith("$(") and a != ">"]
            try:
                parser.parse_args(argv)
            except SystemExit as exc:
                if exc.code == 0:  # --version / --skill style actions
                    continue
                if inline:  # a prose mention of a command: the command path must exist
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            build_parser().parse_args(argv + ["--help"])
                    except SystemExit as help_exit:
                        if help_exit.code == 0:
                            continue
                pytest.fail(f"{doc.name}: `{example}` -> argv {argv} rejected (exit {exc.code})")


def test_wrong_config_position_is_rejected_as_documented():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["whoami", "--config", "/tmp/x"])
    assert exc.value.code == 2


def _subcommands(parser: argparse.ArgumentParser, *path: str) -> set[str]:
    for name in path:
        action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        parser = action.choices[name]
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(action.choices)


@pytest.mark.parametrize("doc", [SKILL, SETUP], ids=["SKILL.md", "SETUP.md"])
def test_runtime_commands_and_presence_boundary_are_documented(doc):
    text = doc.read_text(encoding="utf-8")
    for command in _subcommands(build_parser(), "runtime"):
        assert f"raincli runtime {command}" in text, f"{doc.name} does not document `runtime {command}`"
    for status in ("ready", "busy", "blocked", "offline", "unknown"):
        assert status in text
    assert re.search(r"not\*{0,2} (delivery|one of these states)", text) or "doesn't mean a message was received" in text
    assert "stable" in text and "signature" in text  # stable releases only; unsigned, and saying so
