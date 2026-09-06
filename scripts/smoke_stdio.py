"""End-to-end stdio smoke test for the calendar-mcp server.

Spawns the installed ``calendar-mcp`` console script as a real MCP subprocess,
performs the protocol handshake, and lists the tools. Deliberately runs with a
token path that does not exist: the handshake and tool listing must work with no
Google credentials at all, because credentials are loaded lazily on the first
tool call.

Usage:
    python scripts/smoke_stdio.py [command]      # default command: calendar-mcp
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOL_COUNT = 23


async def smoke(command: str) -> int:
    env = dict(os.environ)
    env.update(
        {
            "GOOGLE_CLIENT_ID": "smoke-test-client-id",
            "GOOGLE_CLIENT_SECRET": "smoke-test-client-secret",
            # Point at a path that cannot exist, so the run proves no token is needed.
            "TOKEN_FILE_PATH": os.path.join(tempfile.gettempdir(), "calendar-mcp-no-such-token.json"),
            # Same for the multi-account config directory.
            "CALENDAR_MCP_CONFIG_DIR": os.path.join(tempfile.gettempdir(), "calendar-mcp-no-such-config"),
            "CALENDAR_MCP_LOG_LEVEL": "WARNING",
        }
    )
    env.pop("CALENDAR_MCP_ALLOW_BROWSER_AUTH", None)

    params = StdioServerParameters(command=command, args=[], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server:       {init.server_info.name} {init.server_info.version}")
            print(f"protocol:     {init.protocol_version}")
            print(f"instructions: {len(init.instructions or '')} chars")

            listing = await session.list_tools()
            names = sorted(tool.name for tool in listing.tools)
            structured = sum(1 for tool in listing.tools if tool.output_schema)
            print(f"tools:        {len(names)}")
            for name in names:
                print(f"  - {name}")
            print(f"structured:   {structured}/{len(names)} tools declare an output schema")

            if len(names) != EXPECTED_TOOL_COUNT:
                print(
                    f"FAIL: expected {EXPECTED_TOOL_COUNT} tools, got {len(names)}",
                    file=sys.stderr,
                )
                return 1
            if structured != len(names):
                print("FAIL: some tools have no structured output schema", file=sys.stderr)
                return 1

    print("OK")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "calendar-mcp"
    resolved = shutil.which(command) or command
    return anyio.run(smoke, resolved)


if __name__ == "__main__":
    sys.exit(main())
