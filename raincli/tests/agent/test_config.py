import io
import json
import os
import stat

import pytest

from raincli_agent import cli
from raincli_agent.api import ApiClient
from raincli_agent.config import load_config, validate_api_url, write_config
from raincli_agent.errors import ConfigError

TOKEN = "rca_" + "A" * 43


@pytest.mark.parametrize("url", [
    "http://raincli.com", "http://10.0.0.5:8000", "ftp://raincli.com",
    "https://user:pw@raincli.com", "https://raincli.com/?x=1",
    "https://raincli.com/#frag", "https://raincli.com?", "https://",
    "https://raincli.com/ path", "",
])
def test_bad_api_urls_refused(url):
    with pytest.raises(ConfigError):
        validate_api_url(url)


@pytest.mark.parametrize("url,expected", [
    ("https://raincli.com/", "https://raincli.com"),
    ("https://example.com/rain/", "https://example.com/rain"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("http://localhost:9/prefix", "http://localhost:9/prefix"),
    ("http://[::1]:8000", "http://[::1]:8000"),
])
def test_good_api_urls(url, expected):
    assert validate_api_url(url) == expected


def test_http_remote_refused_by_client_too():
    with pytest.raises(ConfigError):
        ApiClient("http://raincli.com", TOKEN)


def test_group_readable_config_refused(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"api_url": "https://raincli.com", "token": TOKEN}))
    for mode in (0o640, 0o604, 0o660):
        os.chmod(path, mode)
        with pytest.raises(ConfigError, match="group or others"):
            load_config(str(path))
    os.chmod(path, 0o600)
    cfg = load_config(str(path))
    assert cfg.api_url == "https://raincli.com"
    assert TOKEN not in repr(cfg) and TOKEN not in str(cfg.token)


def test_config_with_bad_url_refused(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"api_url": "http://raincli.com", "token": TOKEN}))
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError, match="https"):
        load_config(str(path))


def test_config_init_from_stdin_writes_0600(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sub" / "agent.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    rc = cli.main(["--config", str(path), "config", "init", "--api-url",
                   "https://raincli.com", "--token-file", "-"])
    assert rc == 0
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"api_url": "https://raincli.com", "token": TOKEN}
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    # refuses to overwrite without --force
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN))
    assert cli.main(["--config", str(path), "config", "init", "--api-url",
                     "https://raincli.com", "--token-file", "-"]) == 1


def test_config_init_from_token_file(tmp_path):
    token_file = tmp_path / "tok"
    token_file.write_text(TOKEN)
    path = tmp_path / "agent.json"
    assert cli.main(["--config", str(path), "config", "init", "--api-url",
                     "http://127.0.0.1:8000", "--token-file", str(token_file)]) == 0
    assert load_config(str(path)).token.reveal() == TOKEN


def test_token_cannot_be_passed_in_argv(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", str(tmp_path / "a.json"), "config", "init", "--api-url",
                  "https://raincli.com", "--token", TOKEN])
    assert exc.value.code == 2


def test_config_init_rejects_malformed_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not-a-token"))
    rc = cli.main(["--config", str(tmp_path / "a.json"), "config", "init", "--api-url",
                   "https://raincli.com", "--token-file", "-"])
    assert rc == 1
    assert not (tmp_path / "a.json").exists()


def test_write_config_refuses_http_remote(tmp_path):
    with pytest.raises(ConfigError):
        write_config(str(tmp_path / "a.json"), "http://raincli.com", TOKEN)


def test_token_as_token_file_value_refused_without_echo(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "a.json"), "config", "init", "--api-url",
                   "https://raincli.com", "--token-file", TOKEN])
    assert rc == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


# -- the website's downloaded agent config JSON as --token-file ------------------

def _init(tmp_path, source, api_url="https://raincli.com", stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(["--config", str(tmp_path / "out" / "agent.json"), "config", "init",
                     "--api-url", api_url, "--token-file", source])


def test_config_init_accepts_downloaded_agent_json(tmp_path, capsys):
    downloaded = tmp_path / "raincli-bob.json"
    downloaded.write_text(json.dumps({"api_url": "https://raincli.com/", "token": TOKEN}, indent=2))
    assert _init(tmp_path, str(downloaded), api_url="HTTPS://raincli.com") == 0
    out = tmp_path / "out" / "agent.json"
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert json.loads(out.read_text()) == {"api_url": "https://raincli.com", "token": TOKEN}
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_config_init_downloaded_json_from_stdin(tmp_path, monkeypatch):
    blob = json.dumps({"api_url": "https://raincli.com", "token": TOKEN})
    assert _init(tmp_path, "-", stdin=blob, monkeypatch=monkeypatch) == 0
    assert load_config(str(tmp_path / "out" / "agent.json")).token.reveal() == TOKEN


@pytest.mark.parametrize("file_url", ["https://other.example", "https://raincli.com/prefix",
                                      "http://raincli.com", None])
def test_config_init_downloaded_json_api_url_mismatch(tmp_path, capsys, file_url):
    data = {"token": TOKEN} if file_url is None else {"api_url": file_url, "token": TOKEN}
    downloaded = tmp_path / "raincli-bob.json"
    downloaded.write_text(json.dumps(data))
    assert _init(tmp_path, str(downloaded)) == 1
    captured = capsys.readouterr()
    assert "api_url" in captured.err or "https" in captured.err
    assert TOKEN not in captured.out + captured.err
    assert not (tmp_path / "out" / "agent.json").exists()


def test_config_init_mismatch_message_names_both_urls(tmp_path, capsys):
    downloaded = tmp_path / "raincli-bob.json"
    downloaded.write_text(json.dumps({"api_url": "https://other.example", "token": TOKEN}))
    assert _init(tmp_path, str(downloaded)) == 1
    err = capsys.readouterr().err
    assert "https://other.example" in err and "https://raincli.com" in err and TOKEN not in err


@pytest.mark.parametrize("content", [
    '{"api_url": "https://raincli.com", "token": "' + TOKEN + '"',  # truncated
    '{"api_url": "https://raincli.com"}',
    '{"api_url": "https://raincli.com", "token": 5}',
    '{"api_url": "https://raincli.com", "token": "rca_short"}',
    '["' + TOKEN + '"]',
])
def test_config_init_malformed_agent_json(tmp_path, capsys, content):
    downloaded = tmp_path / "raincli-bob.json"
    downloaded.write_text(content)
    assert _init(tmp_path, str(downloaded)) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert not (tmp_path / "out" / "agent.json").exists()


def test_config_init_plain_token_file_still_works(tmp_path):
    plain = tmp_path / "token.txt"
    plain.write_text(TOKEN + "\n")
    assert _init(tmp_path, str(plain)) == 0
    assert load_config(str(tmp_path / "out" / "agent.json")).token.reveal() == TOKEN
