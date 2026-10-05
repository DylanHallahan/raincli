"""The Nginx config never logs the app install token (protocol §16.14 S3 addition).

Static: the config is parsed here, and the map's regex is applied with Python's engine (PCRE and
Python agree on this pattern once the named-group syntax is translated). ``nginx -t`` and a live
request check run where Nginx or Docker is available (see the p2 report)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

NGINX = Path(__file__).resolve().parents[3] / "deploy" / "raincli" / "nginx"
HTTP = (NGINX / "raincli-http.conf").read_text()
SITE = (NGINX / "raincli.com.conf").read_text()
TOKEN = "SeCrEtToKeN_abcdefghijklmnopqrstuvwxyz0123"


def _strip_comments(text):
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def redact(user_agent):
    """The map in raincli-http.conf, evaluated as Nginx does: first matching regex, else the default."""
    block = re.search(r"map \$http_user_agent \$raincli_user_agent \{(.*?)\n\}", HTTP, re.S).group(1)
    for line in _strip_comments(block).splitlines():
        line = line.strip().rstrip(";")
        if not line:
            continue
        if line.startswith("default"):
            assert line.split()[1] == "$http_user_agent"
            continue
        star, pattern, value = re.fullmatch(r'"~(\*?)(.*?)"\s+"(.*)"', line).groups()
        flags = re.IGNORECASE if star else 0
        match = re.search(pattern.replace("(?<", "(?P<"), user_agent, flags)
        if match:
            return re.sub(r"\$\{(\w+)\}", lambda m: match.group(m.group(1)), value)
    return user_agent


@pytest.mark.parametrize("user_agent,logged", [
    (f"Mozilla/5.0 Edg/131.0 RainCLIApp/{TOKEN}", "Mozilla/5.0 Edg/131.0 RainCLIApp/[redacted]"),
    (f"RainCLIApp/{TOKEN}", "RainCLIApp/[redacted]"),
    (f"x raincliapp/{TOKEN} tail RainCLIApp/{TOKEN}", "x RainCLIApp/[redacted]"),
    (f"Mozilla/5.0 RainCLIApp/{TOKEN} Edg/131.0", "Mozilla/5.0 RainCLIApp/[redacted]"),
    ("Mozilla/5.0 plain", "Mozilla/5.0 plain"),
    ("", ""),
])
def test_the_map_redacts_the_install_token(user_agent, logged):
    assert redact(user_agent) == logged and TOKEN not in redact(user_agent)


def test_every_log_format_logs_only_the_redacted_user_agent():
    formats = re.findall(r"log_format\s+(\w+)\s+(.*?);", _strip_comments(HTTP), re.S)
    assert {name for name, _ in formats} == {"raincli_main", "raincli_noquery"}
    for name, body in formats:
        assert "$raincli_user_agent" in body and "$http_user_agent" not in body, name
    assert "$http_user_agent" not in _strip_comments(SITE)


def test_every_server_and_location_logs_with_a_raincli_format_or_not_at_all():
    site = _strip_comments(SITE)
    logs = re.findall(r"access_log\s+([^;]+);", site)
    assert logs, "the site config sets its own access_log"
    for value in logs:
        parts = value.split()
        assert parts == ["off"] or (len(parts) == 2 and parts[1] in ("raincli_main", "raincli_noquery")), value
    for server in re.split(r"\nserver \{", site)[1:]:
        assert re.search(r"^    access_log ", server, re.M), "each server block sets its own access_log"
