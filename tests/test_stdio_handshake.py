"""End-to-end stdio test: a real ``python -m calendar_mcp`` subprocess.

Proves that the packaged server speaks MCP over stdio with dummy credentials and
no token file at all -- credentials are loaded lazily on the first tool call, so
`initialize` and `tools/list` must succeed without them.

The whole exchange is bounded by ``anyio.fail_after`` so a hung server fails the
test instead of hanging the suite, and the test skips (rather than fails) if the
subprocess cannot be started in this environment.
"""
import os
import sys
import tempfile

import anyio
import pytest

pytest.importorskip("mcp.client.stdio")

# Kept in step with tests/test_server.py deliberately: this test must be able to
# fail on its own if the wire-level tool list drifts from the in-process one.
EXPECTED_TOOLS = {
    "list_calendars",
    "create_calendar",
    "find_events",
    "check_attendee_status",
    "query_free_busy",
    "analyze_busyness",
    "project_recurring_events",
    "create_event",
    "quick_add_event",
    "update_event",
    "move_event",
    "add_attendee",
    "respond_to_event",
    "schedule_mutual",
    "delete_event",
    "list_accounts",
    "get_preferences",
    "set_preferences",
    "time_audit",
    "find_focus_time",
    "block_focus_time",
    "detect_conflicts",
    "suggest_reschedule",
}

HANDSHAKE_TIMEOUT_SECONDS = 60.0


def _child_env():
    env = dict(os.environ)
    env.update({
        "GOOGLE_CLIENT_ID": "test-client-id",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
        # A path that cannot exist: the run proves no token is needed.
        "TOKEN_FILE_PATH": os.path.join(tempfile.gettempdir(), "calendar-mcp-no-such-token.json"),
        "CALENDAR_MCP_LOG_LEVEL": "WARNING",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("CALENDAR_MCP_ALLOW_BROWSER_AUTH", None)
    env.pop("CALENDAR_MCP_LOG_FILE", None)
    return env


async def test_stdio_subprocess_handshake_lists_all_tools():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "calendar_mcp"],
        env=_child_env(),
        cwd=str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )

    try:
        with anyio.fail_after(HANDSHAKE_TIMEOUT_SECONDS):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    listing = await session.list_tools()
    except TimeoutError:  # pragma: no cover - only on a wedged server
        pytest.fail(
            f"The stdio server did not finish the handshake within "
            f"{HANDSHAKE_TIMEOUT_SECONDS}s."
        )
    except (OSError, FileNotFoundError) as exc:  # pragma: no cover - sandboxed CI
        pytest.skip(f"Could not spawn the stdio server subprocess: {exc}")

    assert init.server_info.name == "calendar-mcp"
    assert init.server_info.version == "1.1.0"
    assert init.instructions

    names = {tool.name for tool in listing.tools}
    assert names == EXPECTED_TOOLS
    assert len(listing.tools) == len(EXPECTED_TOOLS) == 23
    # Every tool advertises structured output over the wire, not just in-process.
    assert all(tool.output_schema for tool in listing.tools)
