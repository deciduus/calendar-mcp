"""Backward-compatible shim: `python run_server.py` now runs the calendar-mcp CLI.

The FastAPI + uvicorn + stdio-bridge stack this file used to launch is gone;
`calendar-mcp` (or `python -m calendar_mcp`) is the supported entry point.
"""

import sys

from calendar_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
