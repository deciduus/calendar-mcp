# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0]

Full rewrite onto the MCP 2.x SDK as a single process. FastAPI, uvicorn and the
HTTP bridge are gone.

### Added

- Published to PyPI as **`calendar-mcp-server`** with a **`calendar-mcp`** console
  script, so clients can run `uvx --from calendar-mcp-server calendar-mcp`.
- A real CLI: `calendar-mcp serve` (default), `calendar-mcp auth` for the one-time
  Google sign-in, and `calendar-mcp check` to verify the saved token and list
  calendars. `python -m calendar_mcp` accepts the same arguments.
- Streamable HTTP transport: `calendar-mcp --transport http --host H --port P
  [--path /mcp]`. Note that it carries no authentication layer.
- Structured tool output — every tool returns a typed pydantic result with an
  output schema, and carries MCP tool annotations (read-only / destructive /
  idempotent hints).
- Three new tools: `move_event` (reschedule, preserving duration when only a new
  start is given, and/or move between calendars), `respond_to_event` (set your
  own RSVP), and `project_recurring_events` (previously internal logic only).
- `delete_event` asks the client to confirm through MCP elicitation when the
  client supports it, and proceeds normally when it does not.
- Naive ISO timestamps are resolved against the target calendar's own timezone.
- New environment variables `CALENDAR_MCP_ALLOW_BROWSER_AUTH`,
  `CALENDAR_MCP_LOG_FILE` and `CALENDAR_MCP_LOG_LEVEL`.
- `scripts/smoke_stdio.py`: end-to-end stdio handshake and tool-list check.

### Changed

- The Python package moved from `src/` to `calendar_mcp/`.
- Authentication is non-interactive by default. `get_credentials()` never opens a
  browser unless `CALENDAR_MCP_ALLOW_BROWSER_AUTH=1`; otherwise it raises a
  message telling you to run `calendar-mcp auth`. Auth configuration is read at
  call time, so `.env` changes apply without reimporting.
- Google API errors now surface to the client with the HTTP status and Google's
  own error text, instead of being flattened into a null result.
- Logging goes to stderr (and optionally a file), never to stdout.
- `requirements.txt` is now just `-e .`; dependencies live in `pyproject.toml`.
- Minimum Python is 3.10.

### Removed

- The FastAPI application, all REST endpoints, the readiness endpoint, uvicorn,
  `requests`, and the separate stdio bridge process.
- The `RELOAD` and `MCP_API_HOST` environment variables.

### Deprecated

- `run_server.py` is now a thin shim that calls the CLI. It still works, but use
  `calendar-mcp` instead; the shim will be removed in a future release.

### Compatibility

- Tool names are unchanged from 0.2.0, so existing prompts keep working.

## [0.2.0]

Maintenance release (PR #4): fixed the install, the server startup race, and the
broken analysis tools.

### Fixed

- Dependency specification, so a clean install actually works.
- Honour `PORT` and report readiness correctly on startup, closing the race where
  the MCP bridge connected before the HTTP server was listening (#2, #3).
- Recurring-event projection: correct expansion, and `TZID` on `EXDATE`/`RDATE`
  is now resolved (added the `tzdata` dependency for Windows).
- Busyness analysis produced wrong counts and durations.
- Hardened MCP stdio mode and the application lifespan; removed dead code.

### Added

- A pytest suite and a GitHub Actions CI workflow.
- Packaging metadata and an updated README.
