"""Tests for the calendar-mcp command line (calendar_mcp/cli.py) and auth.py.

These call ``cli.main()`` in-process with explicit argv. Nothing here reaches
Google: the token file path points at a file that does not exist, and where a
token is needed it is faked.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from calendar_mcp import auth, cli
from calendar_mcp.models import CalendarListEntry, CalendarListResponse


@pytest.fixture
def no_token(tmp_path, monkeypatch):
    """Points the token file at a path that does not exist."""
    monkeypatch.setenv("TOKEN_FILE_PATH", str(tmp_path / "absent-token.json"))
    monkeypatch.delenv("CALENDAR_MCP_LOG_FILE", raising=False)
    return tmp_path


# --- calendar-mcp check -------------------------------------------------------


def test_check_without_a_token_exits_non_zero_with_a_helpful_message(no_token, capsys):
    exit_code = cli.main(["check"])
    captured = capsys.readouterr()

    assert exit_code != 0
    # The remedy names the exact command to run, and goes to stderr so stdout
    # stays machine-readable.
    assert "calendar-mcp auth" in captured.err
    assert "No valid Google Calendar token" in captured.err
    # The config summary is still printed, so the user can see what was checked.
    assert "Token file:" in captured.out
    assert "calendar-mcp auth" not in captured.out


def test_check_reports_the_token_path_it_looked_at(no_token, capsys):
    cli.main(["check"])
    out = capsys.readouterr().out

    assert str(no_token / "absent-token.json") in out
    assert "Client ID configured: yes" in out


def test_check_with_a_valid_token_lists_calendars(no_token, capsys):
    creds = MagicMock(name="credentials")
    entry = CalendarListEntry(
        etag="e", id="primary", summary="My calendar", accessRole="owner", primary=True
    )
    with patch.object(auth, "load_credentials", return_value=creds), \
            patch("calendar_mcp.calendar_actions.find_calendars",
                  return_value=CalendarListResponse(items=[entry])):
        exit_code = cli.main(["check"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Token: valid" in out
    assert "My calendar [primary] (primary)" in out


def test_check_reports_an_api_failure(no_token, capsys):
    creds = MagicMock(name="credentials")
    with patch.object(auth, "load_credentials", return_value=creds), \
            patch("calendar_mcp.calendar_actions.find_calendars",
                  side_effect=RuntimeError("network is down")):
        exit_code = cli.main(["check"])

    assert exit_code == 1
    assert "network is down" in capsys.readouterr().err


# --- calendar-mcp auth --------------------------------------------------------


def test_auth_without_client_credentials_exits_non_zero(no_token, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")

    exit_code = cli.main(["auth"])

    assert exit_code == 1
    assert "GOOGLE_CLIENT_ID" in capsys.readouterr().err


def test_auth_reports_a_failed_oauth_flow(no_token, capsys):
    with patch.object(auth, "run_oauth_flow", side_effect=auth.AuthError("consent denied")):
        exit_code = cli.main(["auth"])

    assert exit_code == 1
    assert "consent denied" in capsys.readouterr().err


def test_auth_success_prints_the_token_location(no_token, capsys):
    with patch.object(auth, "run_oauth_flow", return_value=MagicMock(name="creds")):
        exit_code = cli.main(["auth"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Authorized" in out
    assert "absent-token.json" in out


def test_auth_no_browser_flag_is_passed_through(no_token):
    with patch.object(auth, "run_oauth_flow", return_value=MagicMock()) as flow:
        cli.main(["auth", "--no-browser"])
    assert flow.call_args.kwargs["open_browser"] is False


# --- argument parsing ---------------------------------------------------------


def test_version_flag_prints_the_package_version(capsys):
    from calendar_mcp import __version__

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_subcommand_serves_over_stdio():
    with patch("calendar_mcp.server.run_stdio") as run_stdio:
        assert cli.main([]) == 0
    run_stdio.assert_called_once_with()


def test_http_transport_flags_are_forwarded():
    with patch("calendar_mcp.server.run_http") as run_http:
        assert cli.main([
            "serve", "--transport", "http", "--host", "0.0.0.0",
            "--port", "8931", "--path", "/rpc",
        ]) == 0
    run_http.assert_called_once_with(host="0.0.0.0", port=8931, path="/rpc")


def test_unknown_subcommand_is_rejected():
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code != 0


def test_configure_logging_never_writes_to_stdout():
    import logging
    import sys

    cli.configure_logging("DEBUG")
    handlers = logging.getLogger().handlers
    assert handlers
    for handler in handlers:
        assert getattr(handler, "stream", None) is not sys.stdout


def test_configure_logging_writes_to_the_log_file_env_var(tmp_path, monkeypatch):
    import logging

    log_file = tmp_path / "calendar-mcp.log"
    monkeypatch.setenv(cli.LOG_FILE_ENV, str(log_file))
    try:
        cli.configure_logging("INFO")
        logging.getLogger("calendar_mcp.test").info("hello from the test")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "hello from the test" in log_file.read_text(encoding="utf-8")
    finally:
        monkeypatch.delenv(cli.LOG_FILE_ENV, raising=False)
        cli.configure_logging("WARNING")


# --- auth module --------------------------------------------------------------


def test_get_credentials_refuses_to_open_a_browser_by_default(no_token, monkeypatch):
    monkeypatch.delenv(auth.ALLOW_BROWSER_AUTH_ENV, raising=False)

    with patch.object(auth, "run_oauth_flow") as flow:
        with pytest.raises(auth.AuthError, match="calendar-mcp auth"):
            auth.get_credentials()
    flow.assert_not_called()


def test_get_credentials_opens_a_browser_when_explicitly_allowed(no_token):
    creds = MagicMock(name="creds")
    with patch.object(auth, "run_oauth_flow", return_value=creds) as flow:
        assert auth.get_credentials(allow_browser=True) is creds
    flow.assert_called_once()


def test_browser_auth_allowed_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(auth.ALLOW_BROWSER_AUTH_ENV, "1")
    assert auth.browser_auth_allowed() is True
    monkeypatch.setenv(auth.ALLOW_BROWSER_AUTH_ENV, "0")
    assert auth.browser_auth_allowed() is False


def test_load_credentials_returns_none_without_a_token_file(no_token):
    assert auth.load_credentials() is None
    assert auth.has_valid_token() is False


def test_load_credentials_returns_none_for_a_malformed_token_file(tmp_path, monkeypatch):
    bad = tmp_path / "token.json"
    bad.write_text("not json at all", encoding="utf-8")
    monkeypatch.setenv("TOKEN_FILE_PATH", str(bad))

    assert auth.load_credentials() is None


def test_configuration_accessors_read_the_environment_at_call_time(monkeypatch):
    monkeypatch.setenv("OAUTH_CALLBACK_PORT", "9999")
    assert auth.callback_port() == 9999
    assert auth.redirect_uri() == "http://localhost:9999/oauth2callback"

    monkeypatch.setenv("OAUTH_CALLBACK_PORT", "not-a-port")
    assert auth.callback_port() == auth.DEFAULT_CALLBACK_PORT

    monkeypatch.setenv("CALENDAR_SCOPES", "https://example.test/scope")
    assert auth.get_scopes() == ["https://example.test/scope"]


def test_save_credentials_writes_the_token(tmp_path):
    creds = MagicMock(name="creds")
    creds.to_json.return_value = '{"token": "abc"}'
    target = tmp_path / "nested" / "token.json"

    written = auth.save_credentials(creds, str(target))

    assert Path(written) == target
    assert target.read_text(encoding="utf-8") == '{"token": "abc"}'


def test_credential_provider_caches_and_resets(no_token):
    from calendar_mcp.server import CredentialProvider

    creds = MagicMock(name="creds")
    creds.valid = True
    provider = CredentialProvider()

    with patch.object(auth, "get_credentials", return_value=creds) as get:
        assert provider.get() is creds
        assert provider.get() is creds
        assert get.call_count == 1

        provider.reset()
        assert provider.get() is creds
        assert get.call_count == 2


def test_module_runs_as_python_dash_m(tmp_path):
    """`python -m calendar_mcp --help` works (the __main__ shim is wired up)."""
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "calendar_mcp", "--help"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    assert "calendar-mcp" in result.stdout
