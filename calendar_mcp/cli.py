"""Command line entry point for calendar-mcp.

    calendar-mcp                       # serve MCP over stdio (the default)
    calendar-mcp --transport http      # serve MCP over streamable HTTP
    calendar-mcp auth                  # sign in to Google in a browser, once
    calendar-mcp auth --account work   # sign in a second, named account
    calendar-mcp accounts              # list the accounts and their token files
    calendar-mcp check                 # verify the saved token works

Logging never goes to stdout: in stdio mode stdout is the MCP protocol channel.
Diagnostics go to stderr, and additionally to ``$CALENDAR_MCP_LOG_FILE`` if set.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Sequence

from dotenv import load_dotenv

PROG = "calendar-mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"
LOG_FILE_ENV = "CALENDAR_MCP_LOG_FILE"
LOG_LEVEL_ENV = "CALENDAR_MCP_LOG_LEVEL"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

logger = logging.getLogger(__name__)


def configure_logging(level: str = "INFO") -> None:
    """Sends logs to stderr (and a file if CALENDAR_MCP_LOG_FILE is set).

    Any pre-existing handlers are dropped: a stray stdout handler would corrupt
    the JSON-RPC stream in stdio mode.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best effort
            pass

    resolved = (os.getenv(LOG_LEVEL_ENV) or level or "INFO").upper()
    root.setLevel(getattr(logging, resolved, logging.INFO))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(stderr_handler)

    log_file = os.getenv(LOG_FILE_ENV)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:
            print(f"{PROG}: could not open log file {log_file!r}: {exc}", file=sys.stderr)


def _env_port() -> int:
    raw = os.getenv("PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"{PROG}: PORT={raw!r} is not a number; using {DEFAULT_PORT}.", file=sys.stderr)
        return DEFAULT_PORT


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to serve on. 'stdio' (default) for local MCP clients, "
        "'http' for streamable HTTP.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", DEFAULT_HOST),
        help=f"Interface to bind when --transport http (default: {DEFAULT_HOST}, env HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_port(),
        help=f"Port to listen on when --transport http (default: {DEFAULT_PORT}, env PORT).",
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_HTTP_PATH,
        help=f"URL path the MCP endpoint is served at (default: {DEFAULT_HTTP_PATH}).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity on stderr (default: INFO, env CALENDAR_MCP_LOG_LEVEL).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Builds the argparse parser for the calendar-mcp command."""
    from . import __version__

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Google Calendar as an MCP server. With no subcommand, serves MCP over stdio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  calendar-mcp                      serve over stdio (what MCP clients launch)\n"
            "  calendar-mcp --transport http --port 8000\n"
            "  calendar-mcp auth                 one-time Google sign-in in a browser\n"
            "  calendar-mcp auth --account work  sign in an additional named account\n"
            "  calendar-mcp accounts             list the known accounts\n"
            "  calendar-mcp check                verify the saved token still works\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_serve_arguments(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="{serve,auth,accounts,check}")

    serve = subparsers.add_parser("serve", help="Serve MCP (the default when no subcommand is given).")
    _add_serve_arguments(serve)

    auth_cmd = subparsers.add_parser(
        "auth",
        help="Run the interactive Google OAuth flow and save the token.",
    )
    auth_cmd.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser.",
    )
    auth_cmd.add_argument(
        "--account",
        default=None,
        metavar="NAME",
        help="Account to sign in, e.g. 'work'. Omit for the default account. Each "
        "account keeps its own token, so several Google accounts can be signed "
        "in at once.",
    )
    auth_cmd.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity on stderr (default: WARNING).",
    )

    accounts_cmd = subparsers.add_parser(
        "accounts",
        help="List the known accounts, their token files and whether each is signed in.",
    )
    accounts_cmd.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity on stderr (default: WARNING).",
    )

    check = subparsers.add_parser(
        "check",
        help="Report whether a valid Google token exists, and list the calendars.",
    )
    check.add_argument(
        "--account",
        default=None,
        metavar="NAME",
        help="Account to check. Omit for the default account.",
    )
    check.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity on stderr (default: WARNING).",
    )

    return parser


# --- Subcommands ---------------------------------------------------------


def command_serve(args: argparse.Namespace) -> int:
    """Runs the MCP server on the requested transport."""
    from . import server as server_module

    if args.transport == "http":
        server_module.run_http(host=args.host, port=args.port, path=args.path)
    else:
        server_module.run_stdio()
    return 0


def _resolve_account(args: argparse.Namespace) -> Optional[str]:
    """The account named on the command line, validated. ``None`` for the default."""
    from . import accounts

    requested = getattr(args, "account", None)
    if requested is None:
        return None
    return accounts.validate_account_name(requested)


def command_auth(args: argparse.Namespace) -> int:
    """Runs the interactive OAuth flow and reports where the token landed."""
    from . import accounts, auth

    if not auth.client_id() or not auth.client_secret():
        print(
            f"{PROG}: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set.\n"
            "Create an OAuth 'Desktop app' client in the Google Cloud console, then put\n"
            "both values in your environment or a .env file (see example.env).",
            file=sys.stderr,
        )
        return 1

    try:
        account = accounts.resolve_account(_resolve_account(args))
    except accounts.AccountError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1

    token_path = accounts.token_path_for(account)
    # A named account's token lands under the config directory, which may not
    # exist yet; the legacy single-token path is created by auth.save_credentials.
    if account != accounts.DEFAULT_ACCOUNT or not accounts.legacy_token_path():
        accounts.ensure_accounts_dir()

    print(f"Opening a browser to authorize {PROG} against your Google Calendar...")
    print(f"Account: {account}")
    print(f"Token will be saved to: {os.path.abspath(token_path)}")
    print(f"Redirect URI in use: {auth.redirect_uri()}")
    print(
        "A 'Desktop app' OAuth client accepts http://localhost on any port, so there\n"
        "is nothing to register; set OAUTH_CALLBACK_PORT if this port is taken.\n"
    )
    try:
        auth.run_oauth_flow(open_browser=not args.no_browser, path=token_path)
    except auth.AuthError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1

    print(f"\nAuthorized. Token saved to {os.path.abspath(token_path)}")
    print(f"You can now run '{PROG}' (or point your MCP client at it).")
    if account != accounts.DEFAULT_ACCOUNT:
        print(
            f"Pass account='{account}' to any tool, or set "
            f"CALENDAR_MCP_DEFAULT_ACCOUNT={account} to make it the default."
        )
    return 0


def command_accounts(args: argparse.Namespace) -> int:
    """Lists the known accounts, their token files and their sign-in state."""
    from . import accounts

    print(f"Config directory: {accounts.config_dir()}")
    infos = accounts.list_accounts()
    print(f"\nAccounts ({len(infos)}):")
    for info in infos:
        marker = " (default)" if info.is_default else ""
        state = "signed in" if info.valid else "not signed in"
        who = f" <{info.email}>" if info.email else ""
        print(f"  {info.name}{marker}{who} -- {state}")
        print(f"      token: {info.token_path}")

    if not any(info.valid for info in infos):
        print(
            f"\nNo account is signed in yet. Run '{PROG} auth' "
            "(add --account NAME for an additional one).",
            file=sys.stderr,
        )
        return 1
    return 0


def command_check(args: argparse.Namespace) -> int:
    """Reports token status and, when possible, lists the user's calendars."""
    from . import accounts, auth, calendar_actions

    try:
        account = accounts.resolve_account(_resolve_account(args))
    except accounts.AccountError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1

    token_path = os.path.abspath(accounts.token_path_for(account))
    print(f"Account: {account}")
    print(f"Token file: {token_path}")
    print(f"Client ID configured: {'yes' if auth.client_id() else 'no'}")
    print(f"Client secret configured: {'yes' if auth.client_secret() else 'no'}")

    creds = auth.load_credentials(path=token_path)
    if creds is None:
        suffix = "" if account == accounts.DEFAULT_ACCOUNT else f" --account {account}"
        print(
            f"\nNo valid Google Calendar token.\nRun '{PROG} auth{suffix}' to sign in.",
            file=sys.stderr,
        )
        return 1

    print("Token: valid\n")
    try:
        calendars = calendar_actions.find_calendars(creds)
    except Exception as exc:
        print(f"{PROG}: the token loaded but the Calendar API call failed: {exc}", file=sys.stderr)
        return 1

    if calendars is None:
        print(f"{PROG}: could not read the calendar list.", file=sys.stderr)
        return 1

    print(f"Calendars ({len(calendars.items)}):")
    for entry in calendars.items:
        marker = " (primary)" if entry.primary else ""
        print(f"  {entry.summaryOverride or entry.summary or '(untitled)'} [{entry.id}]{marker}")
    return 0


# --- Entry point ---------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parses arguments and dispatches. Returns the process exit code."""
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    configure_logging(getattr(args, "log_level", "INFO"))

    command = getattr(args, "command", None) or "serve"
    try:
        if command == "auth":
            return command_auth(args)
        if command == "accounts":
            return command_accounts(args)
        if command == "check":
            return command_check(args)
        return command_serve(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.info("Interrupted; shutting down.")
        return 130


def run() -> None:
    """Console-script wrapper: exits the process with main()'s status."""
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    run()
