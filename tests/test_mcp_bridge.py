"""Tests for src/mcp_bridge.py.

Only the tool registration is checked: creating the FastMCP server does not
talk to the FastAPI server or to Google.
"""
import asyncio

from src import mcp_bridge

EXPECTED_TOOLS = {
    "list_calendars",
    "find_events",
    "create_event",
    "quick_add_event",
    "update_event",
    "delete_event",
    "add_attendee",
    "check_attendee_status",
    "query_free_busy",
    "schedule_mutual",
    "analyze_busyness",
    "create_calendar",
}


def _tools(mcp):
    """List registered tools, tolerating FastMCP API differences across mcp versions."""
    manager = getattr(mcp, "_tool_manager", None)
    if manager is not None:
        return list(manager.list_tools())
    return list(asyncio.run(mcp.list_tools()))


def test_create_mcp_server_registers_expected_tools():
    assert {tool.name for tool in _tools(mcp_bridge.create_mcp_server())} == EXPECTED_TOOLS


def test_expected_tool_count():
    assert len(EXPECTED_TOOLS) == 12
    assert len(_tools(mcp_bridge.create_mcp_server())) == 12


def test_every_tool_has_a_description():
    for tool in _tools(mcp_bridge.create_mcp_server()):
        assert tool.description, f"tool {tool.name} has no description"
