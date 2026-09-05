"""calendar-mcp: Google Calendar as a Model Context Protocol server.

The MCP server instance lives in :mod:`calendar_mcp.server` and the command
line entry point in :mod:`calendar_mcp.cli`. Both are imported lazily here so
that ``import calendar_mcp`` stays cheap and side-effect free.
"""

__version__ = "1.0.1"

__all__ = ["__version__"]
