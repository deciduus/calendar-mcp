"""Named Google accounts and where their OAuth tokens live.

calendar-mcp can be signed in to several Google accounts at once. Each one has
a short name -- ``default``, ``work``, ``personal`` -- and its own cached OAuth
token::

    <config dir>/accounts/<name>.json

``<config dir>`` is ``$CALENDAR_MCP_CONFIG_DIR`` when set, and otherwise the
platform's user config directory (``platformdirs.user_config_dir`` under the
app name ``calendar-mcp``): ``%APPDATA%\\calendar-mcp`` on Windows,
``~/.config/calendar-mcp`` on Linux, ``~/Library/Application Support/calendar-mcp``
on macOS.

Back-compatibility: v1.0 kept a single token at ``$TOKEN_FILE_PATH`` (default
``./.gcp-saved-tokens.json``). If either is present it *is* the ``default``
account, so an existing install keeps working with no migration and no re-auth.

Nothing here touches the network. Reading an account's token to report whether
it is usable is a local file parse.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .models import AccountInfo

logger = logging.getLogger(__name__)

APP_NAME = "calendar-mcp"

CONFIG_DIR_ENV = "CALENDAR_MCP_CONFIG_DIR"
DEFAULT_ACCOUNT_ENV = "CALENDAR_MCP_DEFAULT_ACCOUNT"
TOKEN_FILE_ENV = "TOKEN_FILE_PATH"

DEFAULT_ACCOUNT = "default"
LEGACY_TOKEN_FILE = ".gcp-saved-tokens.json"
ACCOUNTS_SUBDIR = "accounts"

# Account names become file names, so keep them boring: no separators, no dots
# leading the name, nothing that could escape the accounts directory.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class AccountError(ValueError):
    """Raised for a malformed or unusable account name."""


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """The directory holding account tokens and ``preferences.json``.

    ``$CALENDAR_MCP_CONFIG_DIR`` wins when set; otherwise the platform's user
    config directory for the app. The directory is not created here -- the
    writers (:func:`ensure_config_dir`) do that.
    """
    override = os.getenv(CONFIG_DIR_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    try:
        from platformdirs import user_config_dir

        return Path(user_config_dir(APP_NAME, appauthor=False))
    except Exception:  # pragma: no cover - platformdirs missing or unhappy
        logger.warning("platformdirs is unavailable; falling back to ~/.calendar-mcp")
        return Path.home() / ".calendar-mcp"


def ensure_config_dir() -> Path:
    """Creates and returns the config directory."""
    target = config_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def accounts_dir() -> Path:
    """The ``<config dir>/accounts`` directory holding one token file per account."""
    return config_dir() / ACCOUNTS_SUBDIR


def ensure_accounts_dir() -> Path:
    """Creates and returns the accounts directory."""
    target = accounts_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def validate_account_name(name: str) -> str:
    """Normalises and checks an account name.

    Returns:
        The trimmed name.

    Raises:
        AccountError: If the name is empty or contains anything but letters,
            digits, ``-`` and ``_``.
    """
    text = (name or "").strip()
    if not _NAME_PATTERN.match(text):
        raise AccountError(
            f"Invalid account name {name!r}. Use letters, digits, '-' and '_' "
            "(starting with a letter or digit), e.g. 'work' or 'personal'."
        )
    return text


# ---------------------------------------------------------------------------
# Token paths
# ---------------------------------------------------------------------------


def legacy_token_path() -> Optional[str]:
    """The pre-multi-account token location, when one is in play.

    Returns ``$TOKEN_FILE_PATH`` if that variable is set (whether or not the
    file exists yet -- an explicit setting is an instruction, not a guess), else
    ``./.gcp-saved-tokens.json`` if that file actually exists, else ``None``.
    """
    configured = os.getenv(TOKEN_FILE_ENV)
    if configured and configured.strip():
        return os.path.abspath(configured.strip())
    legacy = Path.cwd() / LEGACY_TOKEN_FILE
    if legacy.exists():
        return str(legacy.resolve())
    return None


def token_path_for(name: Optional[str] = None) -> str:
    """The absolute path of the token file for account ``name``.

    ``None`` resolves through :func:`resolve_account` first. The ``default``
    account honours the legacy single-token location when one is configured.
    """
    resolved = resolve_account(name)
    if resolved == DEFAULT_ACCOUNT:
        legacy = legacy_token_path()
        if legacy:
            return legacy
    return str((accounts_dir() / f"{resolved}.json").resolve())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _known_names() -> List[str]:
    """Every account with a token file on disk, plus ``default``, sorted."""
    names = {DEFAULT_ACCOUNT}
    directory = accounts_dir()
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:  # pragma: no cover - unreadable config dir
        entries = []
    for entry in entries:
        try:
            names.add(validate_account_name(entry.stem))
        except AccountError:
            logger.debug("Ignoring token file with an unusable name: %s", entry)
    ordered = sorted(names - {DEFAULT_ACCOUNT})
    return [DEFAULT_ACCOUNT] + ordered


def _inspect_token(path: str) -> Tuple[bool, Optional[str]]:
    """Reports ``(usable, email)`` for a saved token file, without any network.

    "Usable" means the file parses as an authorized-user token that either is
    still valid or carries a refresh token we could exchange.
    """
    if not os.path.exists(path):
        return False, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.debug("Token file %s is unreadable: %s", path, exc)
        return False, None
    if not isinstance(data, dict):
        return False, None

    email = data.get("account") or None

    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_info(data)
    except Exception as exc:
        logger.debug("Token file %s does not parse as credentials: %s", path, exc)
        return False, email

    return bool(getattr(creds, "valid", False) or getattr(creds, "refresh_token", None)), email


def account_exists(name: str) -> bool:
    """True when a token file exists for ``name``."""
    return os.path.exists(token_path_for(name))


def list_accounts() -> List[AccountInfo]:
    """Every known account, the default first.

    ``default`` is always listed even when it has no token yet, so a fresh
    install can still be told where its token would go.
    """
    default_name = resolve_account(None)
    infos: List[AccountInfo] = []
    for name in _known_names():
        path = token_path_for(name)
        valid, email = _inspect_token(path)
        infos.append(
            AccountInfo(
                name=name,
                token_path=path,
                valid=valid,
                email=email,
                is_default=(name == default_name),
            )
        )
    infos.sort(key=lambda info: (not info.is_default, info.name))
    return infos


def resolve_account(name: Optional[str] = None) -> str:
    """Turns an optional account argument into a concrete account name.

    With a name, validates and returns it. Without one:

    1. ``$CALENDAR_MCP_DEFAULT_ACCOUNT`` when set;
    2. ``default`` when that account has a token;
    3. the sole account, when exactly one exists;
    4. ``default``.

    Raises:
        AccountError: If an explicitly given name is malformed.
    """
    if name is not None and str(name).strip():
        return validate_account_name(str(name))

    configured = os.getenv(DEFAULT_ACCOUNT_ENV)
    if configured and configured.strip():
        return validate_account_name(configured)

    default_path = legacy_token_path() or str((accounts_dir() / f"{DEFAULT_ACCOUNT}.json"))
    if os.path.exists(default_path):
        return DEFAULT_ACCOUNT

    try:
        existing = [entry.stem for entry in sorted(accounts_dir().glob("*.json"))]
    except OSError:  # pragma: no cover - unreadable config dir
        existing = []
    if len(existing) == 1:
        try:
            return validate_account_name(existing[0])
        except AccountError:  # pragma: no cover - odd file name
            pass

    return DEFAULT_ACCOUNT


def describe_known_accounts() -> str:
    """A short ``'default, work'`` listing for error messages."""
    try:
        return ", ".join(info.name for info in list_accounts()) or DEFAULT_ACCOUNT
    except Exception:  # pragma: no cover - never let diagnostics raise
        return DEFAULT_ACCOUNT


__all__ = [
    "APP_NAME",
    "ACCOUNTS_SUBDIR",
    "AccountError",
    "AccountInfo",
    "CONFIG_DIR_ENV",
    "DEFAULT_ACCOUNT",
    "DEFAULT_ACCOUNT_ENV",
    "LEGACY_TOKEN_FILE",
    "TOKEN_FILE_ENV",
    "account_exists",
    "accounts_dir",
    "config_dir",
    "describe_known_accounts",
    "ensure_accounts_dir",
    "ensure_config_dir",
    "legacy_token_path",
    "list_accounts",
    "resolve_account",
    "token_path_for",
    "validate_account_name",
]
