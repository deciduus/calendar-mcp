"""Google OAuth credential handling for the calendar-mcp server.

The MCP server must never block on an interactive browser flow: a stdio server
is launched by an MCP client that has no terminal to show a consent screen in,
and an unexpected `run_local_server()` there just hangs the handshake. So the
default behaviour of :func:`get_credentials` is *non-interactive* -- it loads a
saved token and refreshes it, and raises :class:`AuthError` telling the user to
run ``calendar-mcp auth`` if that is not possible.

Set ``CALENDAR_MCP_ALLOW_BROWSER_AUTH=1`` (or pass ``allow_browser=True``, which
the ``calendar-mcp auth`` subcommand does) to permit the interactive flow.
"""

import logging
import os
from typing import List, Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Load environment variables from a .env file if one is present. The CLI does
# this too; doing it here keeps `import calendar_mcp.auth` self-sufficient.
load_dotenv()

DEFAULT_TOKEN_FILE = ".gcp-saved-tokens.json"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/calendar"
DEFAULT_CALLBACK_PORT = 8080

ALLOW_BROWSER_AUTH_ENV = "CALENDAR_MCP_ALLOW_BROWSER_AUTH"


class AuthError(RuntimeError):
    """Raised when usable Google credentials cannot be obtained."""


# --- Configuration accessors (read at call time so .env / tests apply) ---


def client_id() -> Optional[str]:
    """The configured Google OAuth client ID, if any."""
    return os.getenv("GOOGLE_CLIENT_ID")


def client_secret() -> Optional[str]:
    """The configured Google OAuth client secret, if any."""
    return os.getenv("GOOGLE_CLIENT_SECRET")


def token_file_path() -> str:
    """Absolute-or-relative path of the file the OAuth token is cached in."""
    return os.getenv("TOKEN_FILE_PATH", DEFAULT_TOKEN_FILE)


def get_scopes() -> List[str]:
    """The OAuth scopes requested from Google."""
    return [os.getenv("CALENDAR_SCOPES", DEFAULT_SCOPE)]


def callback_port() -> int:
    """Local port the interactive OAuth callback server listens on."""
    try:
        return int(os.getenv("OAUTH_CALLBACK_PORT", str(DEFAULT_CALLBACK_PORT)))
    except ValueError:
        logger.warning(
            "OAUTH_CALLBACK_PORT is not an integer; falling back to %s",
            DEFAULT_CALLBACK_PORT,
        )
        return DEFAULT_CALLBACK_PORT


def redirect_uri() -> str:
    """Redirect URI registered with the Google Cloud OAuth client."""
    return f"http://localhost:{callback_port()}/oauth2callback"


def browser_auth_allowed() -> bool:
    """Whether the interactive browser flow may be started automatically."""
    return os.getenv(ALLOW_BROWSER_AUTH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _client_config() -> dict:
    cid, secret = client_id(), client_secret()
    if not cid or not secret:
        raise AuthError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set. Create an OAuth "
            "'Desktop app' client in the Google Cloud console and put both values in "
            "your environment or a .env file (see example.env)."
        )
    return {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost", redirect_uri()],
        }
    }


# --- Token load / save ---


def save_credentials(creds: Credentials, path: Optional[str] = None) -> str:
    """Writes ``creds`` to the token file and returns the path written to."""
    target = path or token_file_path()
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())
    try:
        os.chmod(target, 0o600)
    except OSError:  # best effort; Windows ACLs may refuse
        pass
    logger.info("Saved Google credentials to %s", target)
    return target


def load_credentials(refresh: bool = True) -> Optional[Credentials]:
    """Loads the saved token, refreshing it when expired. Never opens a browser.

    Returns valid :class:`Credentials`, or ``None`` when there is no usable
    saved token.
    """
    path = token_file_path()
    if not os.path.exists(path):
        logger.info("No saved token at %s", path)
        return None

    try:
        creds = Credentials.from_authorized_user_file(path, get_scopes())
    except Exception as exc:  # malformed / truncated token file
        logger.warning("Could not read the token file %s: %s", path, exc)
        return None

    if creds and creds.valid:
        return creds

    if creds and refresh and creds.expired and creds.refresh_token:
        logger.info("Saved credentials expired; refreshing.")
        try:
            creds.refresh(Request())
        except Exception as exc:
            logger.warning("Refreshing the saved credentials failed: %s", exc)
            return None
        try:
            save_credentials(creds, path)
        except OSError as exc:
            logger.warning("Refreshed credentials could not be re-saved: %s", exc)
        return creds if creds.valid else None

    return None


def run_oauth_flow(open_browser: bool = True) -> Credentials:
    """Runs the interactive installed-app OAuth flow and saves the token.

    Raises:
        AuthError: If the client is not configured or the flow does not complete.
    """
    config = _client_config()
    port = callback_port()
    logger.info("Starting the interactive Google OAuth flow on port %s.", port)
    try:
        flow = InstalledAppFlow.from_client_config(
            client_config=config,
            scopes=get_scopes(),
            redirect_uri=redirect_uri(),
        )
        creds = flow.run_local_server(
            port=port,
            authorization_prompt_message="Please visit this URL to authorize calendar-mcp:\n{url}",
            success_message="Authentication successful. You can close this window.",
            open_browser=open_browser,
        )
    except Exception as exc:
        raise AuthError(f"The Google OAuth flow failed: {exc}") from exc

    if not creds or not creds.valid:
        raise AuthError("The Google OAuth flow did not produce valid credentials.")

    save_credentials(creds)
    return creds


def get_credentials(allow_browser: Optional[bool] = None) -> Credentials:
    """Returns valid Google credentials.

    Args:
        allow_browser: Permit the interactive OAuth flow. ``None`` (the default)
            means "consult the ``CALENDAR_MCP_ALLOW_BROWSER_AUTH`` env var",
            which is off by default so an MCP server never blocks on a browser.

    Raises:
        AuthError: If no valid token exists and the browser flow is not allowed
            (or fails). The message tells the user how to fix it.
    """
    creds = load_credentials()
    if creds is not None:
        return creds

    if allow_browser is None:
        allow_browser = browser_auth_allowed()

    if not allow_browser:
        raise AuthError(
            "No valid Google Calendar token was found at "
            f"'{token_file_path()}'. Run 'calendar-mcp auth' once in a terminal to "
            f"sign in, or set {ALLOW_BROWSER_AUTH_ENV}=1 to let the server open a "
            "browser window itself."
        )

    return run_oauth_flow()


def has_valid_token() -> bool:
    """True when a saved (or refreshable) token is currently usable."""
    try:
        return load_credentials() is not None
    except Exception:  # pragma: no cover - defensive
        return False
