"""The CLI never crashes on a name its output encoding can't show (Windows: a pipe or console in cp1252)."""
import io
import sys

from raincli_agent import cli


def test_people_with_names_outside_the_console_code_page(fake_api, as_agent, monkeypatch):
    fake_api.state.set_person_name("bob@example.com", "Bob 🚀 鮑勃")
    as_agent(fake_api.alice)
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")
    monkeypatch.setattr(sys, "stdout", stdout)
    assert cli.main(["people"]) == 0
    stdout.flush()
    text = raw.getvalue().decode("cp1252")
    assert "Bob ? ??" in text  # unshowable characters become "?"
