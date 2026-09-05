# calendar-mcp

An MCP server that gives an LLM client read and write access to your Google Calendar. It runs as a single process — stdio by default, streamable HTTP optionally — and exposes 15 tools with structured output: listing and searching calendars and events, creating, updating, moving, RSVPing to and deleting events, free/busy queries, busyness analysis, recurring-event projection, and finding a mutual slot and booking it. Authentication is Google OAuth 2.0 (Desktop app flow); the token is cached locally and refreshed automatically.

## Quick start

**1. Create Google OAuth credentials.** In the [Google Cloud console](https://console.cloud.google.com/), enable the Google Calendar API, then create an OAuth client ID of type **Desktop app**. Copy the client ID and secret. (Details in [Google Cloud setup](#google-cloud-setup).)

**2. Set them in your environment**, or in a `.env` file in the directory you run from (see `example.env`):

```dotenv
GOOGLE_CLIENT_ID='...'
GOOGLE_CLIENT_SECRET='...'
```

**3. Sign in once, then add the server to your client:**

```bash
uvx calendar-mcp-server auth
```

This opens a browser, and saves a token to `.gcp-saved-tokens.json` (override with `TOKEN_FILE_PATH`). Verify it with `calendar-mcp check`. After that the server runs non-interactively — it never opens a browser on its own unless you set `CALENDAR_MCP_ALLOW_BROWSER_AUTH=1`.

> The PyPI distribution is `calendar-mcp-server`. It installs two identical console scripts, `calendar-mcp` and `calendar-mcp-server`, so `uvx calendar-mcp-server` and a local `calendar-mcp` are the same command.

## Client configuration

**Claude Code**

```bash
claude mcp add calendar \
  --env GOOGLE_CLIENT_ID=... \
  --env GOOGLE_CLIENT_SECRET=... \
  -- uvx calendar-mcp-server
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "calendar": {
      "command": "uvx",
      "args": ["calendar-mcp-server"],
      "env": {
        "GOOGLE_CLIENT_ID": "...",
        "GOOGLE_CLIENT_SECRET": "...",
        "TOKEN_FILE_PATH": "/absolute/path/to/.gcp-saved-tokens.json"
      }
    }
  }
}
```

**Cursor** — `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global): same `mcpServers` block as above.

**Any other client** that speaks the standard `mcpServers` JSON takes the same entry. Set `TOKEN_FILE_PATH` to an absolute path in client configs — the client decides the working directory, and a relative path may not resolve to where `calendar-mcp auth` wrote the token.

## Remote / HTTP mode

```bash
calendar-mcp --transport http --host 127.0.0.1 --port 8000
```

The MCP endpoint is then `http://127.0.0.1:8000/mcp` (change the path with `--path`).

**There is no authentication layer on the HTTP transport yet.** Anyone who can reach the endpoint gets full access to the calendar the saved token belongs to. Bind it to loopback, or expose it only behind a trusted reverse proxy that authenticates, or on a private network such as a tailnet. Do not put it on a public interface.

## Tools

| Tool | Description |
| --- | --- |
| `list_calendars` | List the calendars the user can see, with IDs and timezones. |
| `find_events` | Search a calendar for events, expanding recurring series into instances. |
| `check_attendee_status` | Report who accepted, declined or has not answered an invitation. |
| `query_free_busy` | Busy intervals for one or more calendars, without event details. |
| `analyze_busyness` | Per-day event count and total scheduled minutes over a range. |
| `project_recurring_events` | Compute future occurrences from recurrence rules. |
| `create_calendar` | Create a new secondary calendar. |
| `create_event` | Create an event with explicit start/end times and optional attendees. |
| `quick_add_event` | Create an event from a plain-English phrase, parsed by Google. |
| `update_event` | Change fields on an event; omitted fields are left untouched. |
| `move_event` | Reschedule an event, and/or move it to another calendar. |
| `add_attendee` | Invite one or more people to an existing event. |
| `respond_to_event` | Set your own RSVP (`accepted`/`declined`/`tentative`/`needsAction`). |
| `schedule_mutual` | Find the first slot where everyone is free, then book it. |
| **`delete_event`** | **Destructive.** Permanently delete an event. Asks the client to confirm via elicitation when supported. |

The first six are read-only. `delete_event` is the only tool marked destructive; the rest write but do not destroy. All times are ISO 8601 strings — a naive timestamp is interpreted in the target calendar's own timezone.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` | — | OAuth client ID (required). |
| `GOOGLE_CLIENT_SECRET` | — | OAuth client secret (required). |
| `TOKEN_FILE_PATH` | `.gcp-saved-tokens.json` | Where the OAuth token is cached. |
| `CALENDAR_SCOPES` | `https://www.googleapis.com/auth/calendar` | Scope requested. Use `.../auth/calendar.readonly` for read-only. |
| `OAUTH_CALLBACK_PORT` | `8080` | Local port for the OAuth callback during `calendar-mcp auth`. |
| `CALENDAR_MCP_ALLOW_BROWSER_AUTH` | unset | Set to `1` to let the server itself open a browser when no token exists. Off by default so a stdio server never hangs. |
| `CALENDAR_MCP_LOG_FILE` | unset | Mirror the stderr log to this file. |
| `CALENDAR_MCP_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. Overrides `--log-level`. |
| `HOST` | `127.0.0.1` | Default for `--host` in HTTP mode. |
| `PORT` | `8000` | Default for `--port` in HTTP mode. |

A `.env` file in the working directory is loaded on startup. Logs never go to stdout — stdout is the MCP protocol channel in stdio mode.

### Commands

```
calendar-mcp [--transport {stdio,http}] [--host H] [--port P] [--path /mcp] [--log-level L]
calendar-mcp serve ...     # explicit form of the default
calendar-mcp auth [--no-browser]
calendar-mcp check         # token status + calendar list; exit 1 if no valid token
calendar-mcp --version
```

`python -m calendar_mcp` accepts the same arguments.

## Google Cloud setup

1. Create or select a project and **enable the Google Calendar API**.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID → Application type: Desktop app.** Copy the client ID and secret. There is no JSON download to keep.
3. A Desktop app client has **no "Authorized redirect URIs" field** — Google permits `http://localhost` on any port for this client type, which is what the local callback on `OAUTH_CALLBACK_PORT` uses. Nothing to configure there.
4. On the **OAuth consent screen**: User Type *External*, fill in the app name and contact emails, add the `https://www.googleapis.com/auth/calendar` scope, and **add your own Google account as a test user**. Without that last step the sign-in is rejected.

## Development

```bash
git clone https://github.com/deciduus/calendar-mcp
cd calendar-mcp
uv venv
uv pip install -e ".[dev]"
pytest
```

Layout: `calendar_mcp/server.py` (the `MCPServer` and its tools), `calendar_actions.py` (Google API calls), `analysis.py`, `models.py` (pydantic input/output models), `auth.py` (OAuth), `cli.py` (the `calendar-mcp` command). `scripts/smoke_stdio.py` spawns a real stdio server and checks the handshake and tool list.

## Upgrading from 0.x

- **Package and command renamed.** The distribution is now `calendar-mcp-server` and installs `calendar-mcp` (and an identical `calendar-mcp-server` alias). Point your client at `uvx calendar-mcp-server` instead of `python /path/to/run_server.py`.
- **`run_server.py` still works** — it is now a thin shim over the CLI — but it is deprecated and will be removed in a future release.
- **The FastAPI/uvicorn HTTP API is gone.** There are no REST endpoints, no `/health`, and no separate stdio bridge process; the server is one process on the MCP SDK. If you want HTTP, it is now MCP streamable HTTP at `/mcp`.
- **Authentication no longer happens implicitly.** Run `calendar-mcp auth` once; the server will not open a browser unless `CALENDAR_MCP_ALLOW_BROWSER_AUTH=1`.
- **Tool names are unchanged**, so existing prompts keep working. Results are now structured output rather than JSON stuffed into text.
- **Three new tools:** `move_event`, `respond_to_event`, and `project_recurring_events` (the last previously existed only as internal logic).
- Removed env vars: `RELOAD`, `MCP_API_HOST`. `HOST`/`PORT` now apply to the MCP HTTP transport.

## License

This project is dual-licensed to support both open-source collaboration and sustainable development:

1.  **GNU Affero General Public License v3.0 (AGPL-3.0):**
    *   This software is free to use, modify, and distribute under the terms of the AGPLv3 license. 
    *   Key conditions include that derivative works (including modifications used over a network) must also be licensed under AGPLv3 and their source code made available.
    *   This license is suitable for open-source projects or internal use where AGPLv3 compliance is feasible.
    *   See the [LICENSE](LICENSE) file for the full text.

2.  **Commercial License:**
    *   If the terms of the AGPLv3 are not suitable for your specific use case (e.g., integrating this software into a proprietary, closed-source commercial product or service without complying with AGPLv3's source-sharing requirements), a separate commercial license is available.
    *   Please contact **deciduusleaf@gmail.com** for inquiries regarding commercial licensing options.

By using, modifying, or distributing this software, you agree to be bound by the terms of either the AGPLv3 or a separately negotiated commercial license.
