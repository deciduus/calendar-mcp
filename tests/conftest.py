"""Shared pytest setup for the calendar_mcp test suite.

Every test in this suite runs offline: no network, no Google credentials and no
token file. Dummy OAuth env vars are set before ``calendar_mcp`` is imported so
that configuration code has something to read, and the repository root is put on
``sys.path`` so ``import calendar_mcp`` works no matter where pytest is invoked
from.
"""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Dummy config. `setdefault` so a developer's real .env-derived environment is
# not clobbered, but note that no test may depend on real values.
os.environ.setdefault("GOOGLE_CLIENT_ID", "dummy-client-id.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "dummy-client-secret")
# Point the token file somewhere that cannot exist, so nothing picks up a real
# token from the developer's working copy.
os.environ["TOKEN_FILE_PATH"] = str(REPO_ROOT / "tests" / ".no-such-token.json")
# Never let a test open a browser.
os.environ["CALENDAR_MCP_ALLOW_BROWSER_AUTH"] = "0"


@pytest.fixture(autouse=True)
def _clear_timezone_cache():
    """The server caches calendar timezones per process; isolate every test."""
    from calendar_mcp import server as server_module

    with server_module._timezone_lock:
        server_module._timezone_cache.clear()
    yield
    with server_module._timezone_lock:
        server_module._timezone_cache.clear()
