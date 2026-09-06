# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-05

Multiple accounts, saved scheduling preferences, and a scheduling brain that
reasons about your working hours rather than just your calendar. 15 tools ->
23. Nothing is removed and nothing is renamed.

### Added

- **Multiple Google accounts.** `calendar-mcp auth --account work` signs in an
  additional named account; `calendar-mcp accounts` lists them with their token
  files and sign-in state. Every calendar tool gained an optional trailing
  `account` argument, and `detect_conflicts` takes `accounts` (a list) so it can
  check them all at once. New `list_accounts` tool, new
  `CALENDAR_MCP_DEFAULT_ACCOUNT` environment variable.
- **A config directory.** Tokens (`accounts/<name>.json`) and `preferences.json`
  now live in the OS user config directory, overridable with
  `CALENDAR_MCP_CONFIG_DIR`.
- **Saved scheduling preferences**: timezone, per-weekday working hours, lunch,
  `buffer_minutes`, `min_focus_block_minutes` and `focus_calendar_id`, read with
  `get_preferences` and updated with `set_preferences`. Both are local-only and
  never call Google. `set_preferences` merges field by field and validates the
  merged result before writing, so a rejected change leaves the saved file
  intact; `working_hours` is the exception and replaces the whole schedule.
- **`find_focus_time`** - uninterrupted blocks inside the working hours, with
  lunch, booked events and the buffer removed, longest first.
- **`block_focus_time`** - books those blocks as events (busy, reminders off,
  no notifications), trimming the last one to the hours actually requested.
  `dry_run: true` previews without writing.
- **`detect_conflicts`** - genuine overlaps and too-tight transitions, across
  every signed-in account, so a work meeting clashing with a personal one is
  visible. Declined, free-marked, cancelled and (by default) all-day events are
  ignored.
- **`suggest_reschedule`** - ranked alternative times for an existing meeting,
  preserving its duration, ranked by fewest attendee conflicts and preferring
  its current day. Suggests only; `apply: true` performs the move.
- **`time_audit`** - meeting hours as a share of available working hours,
  grouped by day or week, broken down by meeting size, attendee domain,
  recurring vs one-off, and the people you spend the most time with.
- `platformdirs` is now a dependency.

### Changed

- Tool functions moved out of `server.py` into a `calendar_mcp.tools`
  subpackage, one module per subject area. `server.py` keeps the `MCPServer`,
  the shared helpers and the credential provider, and still re-exports every
  tool by name. New internal modules: `accounts.py`, `preferences.py`,
  `timeutil.py`.
- Credentials are cached per account rather than globally, and the
  calendar-timezone cache is now keyed by account as well as calendar ID, so
  one account's `primary` can no longer supply another account's timezone when
  a naive timestamp is resolved.
- Events carry Google's `transparency` field, so an event the user marked
  "free" is correctly ignored by `detect_conflicts` and `time_audit`.
- `TOKEN_FILE_PATH` now means "the default account's token". It, and an existing
  `.gcp-saved-tokens.json` in the working directory, continue to work unchanged.

### Compatibility

- All 15 tools from 1.0 keep their names and their existing parameters; the only
  change is an added optional `account`. Existing prompts and client configs
  keep working.

## [1.0.1] - 2026-09-05

- Add the MCP Registry ownership marker (`mcp-name`) to the README so the package can be listed at registry.modelcontextprotocol.io.

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
