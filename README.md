# calendar-mcp

An MCP server that gives an LLM client read and write access to your Google Calendar. It runs as a single process — stdio by default, streamable HTTP optionally — and exposes 23 tools with structured output: listing and searching calendars and events, creating, updating, moving, RSVPing to and deleting events, free/busy queries, busyness analysis, recurring-event projection, and finding a mutual slot and booking it. On top of that it has a scheduling brain that knows your working hours: finding and booking focus time, detecting double-bookings across several accounts at once, proposing better times for a meeting, and auditing where your week actually went. Authentication is Google OAuth 2.0 (Desktop app flow); tokens are cached locally, per account, and refreshed automatically.

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

This opens a browser and saves a token in the config directory (see [Accounts](#accounts)); if you already have a `.gcp-saved-tokens.json` or set `TOKEN_FILE_PATH`, that file is used instead. Verify it with `calendar-mcp check`. Add more Google accounts with `calendar-mcp auth --account work`. After that the server runs non-interactively — it never opens a browser on its own unless you set `CALENDAR_MCP_ALLOW_BROWSER_AUTH=1`.

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

**Any other client** that speaks the standard `mcpServers` JSON takes the same entry. `TOKEN_FILE_PATH` is optional now that tokens default to the OS config directory, which does not depend on the working directory the client picks — but if you do set it, make it an absolute path.

## Remote / HTTP mode

```bash
calendar-mcp --transport http --host 127.0.0.1 --port 8000
```

The MCP endpoint is then `http://127.0.0.1:8000/mcp` (change the path with `--path`).

**There is no authentication layer on the HTTP transport yet.** Anyone who can reach the endpoint gets full access to the calendar the saved token belongs to. Bind it to loopback, or expose it only behind a trusted reverse proxy that authenticates, or on a private network such as a tailnet. Do not put it on a public interface.

## Tools

Read-only tools never change anything. "Writes" tools create or modify events;
`delete_event` is the only one that destroys data.

**Calendars and events**

| Tool | | Description |
| --- | --- | --- |
| `list_calendars` | read | List the calendars the user can see, with IDs and timezones. |
| `find_events` | read | Search a calendar for events, expanding recurring series into instances. |
| `check_attendee_status` | read | Report who accepted, declined or has not answered an invitation. |
| `query_free_busy` | read | Busy intervals for one or more calendars, without event details. |
| `analyze_busyness` | read | Per-day event count and total scheduled minutes over a range. |
| `project_recurring_events` | read | Compute future occurrences from recurrence rules. |
| `create_calendar` | writes | Create a new secondary calendar. |
| `create_event` | writes | Create an event with explicit start/end times and optional attendees. |
| `quick_add_event` | writes | Create an event from a plain-English phrase, parsed by Google. |
| `update_event` | writes | Change fields on an event; omitted fields are left untouched. |
| `move_event` | writes | Reschedule an event, and/or move it to another calendar. |
| `add_attendee` | writes | Invite one or more people to an existing event. |
| `respond_to_event` | writes | Set your own RSVP (`accepted`/`declined`/`tentative`/`needsAction`). |
| `schedule_mutual` | writes | Find the first slot where everyone is free, then book it. |
| **`delete_event`** | **destroys** | Permanently delete an event. Asks the client to confirm via elicitation when supported. |

**Scheduling brain** — these read your saved preferences (working hours, lunch, buffer, minimum focus block).

| Tool | | Description |
| --- | --- | --- |
| `find_focus_time` | read | Uninterrupted blocks in a window that could be used for deep work, longest first. |
| `detect_conflicts` | read | Double-bookings and too-tight transitions, across every signed-in account at once. |
| `time_audit` | read | Where the time went: meeting hours, by size, domain, recurrence and person. |
| `suggest_reschedule` | writes *(opt-in)* | Ranked better times for an existing meeting. Suggests only, unless `apply: true`. |
| `block_focus_time` | writes | Book the best free blocks as focus time. `dry_run: true` to preview. |

**Local configuration** — no Google call, no `account` argument.

| Tool | | Description |
| --- | --- | --- |
| `list_accounts` | read | The accounts you have signed in, and which is the default. |
| `get_preferences` | read | Working hours, lunch, buffer, minimum focus block, focus calendar. |
| `set_preferences` | writes | Update and save those preferences. Local file only. |

Every calendar tool takes an optional trailing `account` argument (`detect_conflicts`
takes `accounts`, a list, because checking several at once is the point). All times
are ISO 8601 strings — a naive timestamp is interpreted in the target calendar's own
timezone.

### Scheduling brain

The five scheduling tools share one idea: your calendar is not the same as your
availability. They start from your working hours, subtract lunch, subtract what is
already booked, and subtract the buffer you want around meetings — then reason about
what is left.

Prompts that exercise them:

- "Find me six hours of focus time next week and block it out — show me the times first."
- "Is anything double-booked between my work and personal calendars this week?"
- "My Thursday is back-to-back. Suggest better times for the design review."
- "Where did my time go last month? Who am I spending it with?"
- "I need a 90-minute deep work block before Friday. Is there one?"

`find_focus_time` reports what exists; `block_focus_time` defends it by creating
events (busy, reminders off, nobody notified), trimming the last block so it books
exactly the hours you asked for rather than a whole afternoon. `detect_conflicts`
separates genuine overlaps from *tight* transitions that merely break your buffer,
and ignores events you declined, events marked free, and (by default) all-day
entries. `suggest_reschedule` keeps the meeting's duration, ranks candidate slots by
fewest attendee conflicts and prefers the event's current day, and moves nothing
unless you pass `apply: true`.

### Time audit

`time_audit` answers "how much of my week is meetings?" in one pass, grouped by
`day` or `week`:

```
2026-08-01 .. 2026-09-01, Europe/Berlin, grouped by week
  Meetings:         41.5 h across 63 meetings
  Working hours:    152.0 h available (lunch removed)
  In working hours: 38.0 h -> 25% of the week
  Busiest week:     2026-W34, 14.0 h
  Heaviest day:     2026-08-20, 6.5 h
  By size:          1:1 18.0 h | small 15.5 h | large 8.0 h
  By recurrence:    recurring 26.0 h | one-off 15.5 h
  Top people:       a.schmidt@example.com 9.0 h | j.lee@example.com 7.5 h
```

Declined meetings, events marked free and all-day entries are left out by default;
`include_declined` and `include_all_day` bring them back.

### Safety

- **`delete_event` is the only destructive tool.** It asks the client to confirm
  through MCP elicitation when the client supports it, and proceeds normally when
  it does not.
- **`block_focus_time` takes `dry_run`.** Run it with `dry_run: true` to see the
  exact blocks it would book before anything is written.
- **`suggest_reschedule` does not move anything by default.** `apply` is `false`
  and has to be set explicitly, once the user has agreed to a time.
- **Everything else that writes is additive** — creating or editing an event —
  and `update_event` leaves fields you omit untouched.
- **`list_accounts`, `get_preferences` and `set_preferences` never touch Google.**
  They read and write local files in the config directory.

## Accounts

You can sign in more than one Google account and pick between them per call.

```bash
calendar-mcp auth                    # the default account
calendar-mcp auth --account work     # a second, named account
calendar-mcp accounts                # list them, with token paths and sign-in state
```

Every calendar tool takes an optional `account` argument naming one of these
("what's on my work calendar tomorrow?"). Omit it and the server uses the
default: `CALENDAR_MCP_DEFAULT_ACCOUNT` if set, otherwise the account named
`default`, otherwise the only account you have signed in. `list_accounts`
returns the same list the CLI prints, so the model can discover the names
itself.

Account names must match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.

**Where things live.** Tokens and preferences are stored in the OS user config
directory — `%LOCALAPPDATA%\calendar-mcp` on Windows, `~/.config/calendar-mcp`
on Linux, `~/Library/Application Support/calendar-mcp` on macOS — with one
token file per account under `accounts/`. Override the whole directory with
`CALENDAR_MCP_CONFIG_DIR`.

**Back-compat.** `TOKEN_FILE_PATH` still works and now means *the default
account's token*. If it is set, or if a `.gcp-saved-tokens.json` exists in the
working directory, that file is used for the `default` account and nothing
moves. Named accounts always live in the config directory.

## Preferences

The server remembers how you like your week to be shaped, so the scheduling
tools do not have to guess. Read them with `get_preferences` and change them
with `set_preferences` ("I start at 8 and I want 15 minutes between meetings").
Preferences are global — they describe you, not one account — and are stored as
`preferences.json` in the config directory.

| Field | Default | Meaning |
| --- | --- | --- |
| `timezone` | unset | IANA zone the working hours are expressed in, e.g. `Europe/Berlin`. |
| `working_hours` | Mon–Fri 09:00–17:00 | Per weekday (`mon`…`sun`), a list of `["HH:MM", "HH:MM"]` spans. |
| `buffer_minutes` | `0` | Gap to leave either side of a meeting when proposing times. |
| `min_focus_block_minutes` | `60` | Shortest free stretch that still counts as usable focus time. |
| `lunch` | unset | A daily break carved out of the working hours. |
| `focus_calendar_id` | `primary` | Calendar that focus blocks are booked on. |

```json
{
  "timezone": "Europe/Berlin",
  "working_hours": {
    "mon": [["09:00", "12:00"], ["13:00", "18:00"]],
    "tue": [["09:00", "17:00"]],
    "wed": [["09:00", "17:00"]],
    "thu": [["09:00", "17:00"]],
    "fri": [["09:00", "15:00"]],
    "sat": [],
    "sun": []
  },
  "buffer_minutes": 15,
  "min_focus_block_minutes": 90,
  "lunch": ["12:30", "13:15"],
  "focus_calendar_id": "primary"
}
```

`set_preferences` merges: only the arguments you pass change, and the merged
result is validated before it is written, so a rejected change leaves the saved
file untouched. The one exception is `working_hours`, which is a **whole-schedule
replacement** — weekdays you leave out of the dict become non-working days. Pass
`clear_lunch: true` to remove a lunch break.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` | — | OAuth client ID (required). |
| `GOOGLE_CLIENT_SECRET` | — | OAuth client secret (required). |
| `TOKEN_FILE_PATH` | `.gcp-saved-tokens.json` | Where the **default** account's OAuth token is cached. |
| `CALENDAR_MCP_CONFIG_DIR` | OS user config dir | Directory holding per-account tokens (`accounts/`) and `preferences.json`. |
| `CALENDAR_MCP_DEFAULT_ACCOUNT` | `default` | Account used when a tool's `account` argument is omitted. |
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
calendar-mcp auth [--account NAME] [--no-browser]
calendar-mcp accounts      # list known accounts; exit 1 if none is signed in
calendar-mcp check [--account NAME]   # token status + calendar list; exit 1 if no valid token
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

Layout: `calendar_mcp/server.py` (the `MCPServer`, shared helpers and the credential provider), `tools/` (one module per tool area — the tool functions themselves), `calendar_actions.py` (Google API calls), `analysis.py`, `timeutil.py` (pure interval maths), `accounts.py` (multi-account token paths), `preferences.py` (the saved schedule), `models.py` (pydantic input/output models), `auth.py` (OAuth), `cli.py` (the `calendar-mcp` command). `scripts/smoke_stdio.py` spawns a real stdio server and checks the handshake and tool list.

## Upgrading from 1.0

Nothing breaks. All 15 original tools keep their names and their existing
parameters; each simply gained an optional trailing `account`. Your existing
`TOKEN_FILE_PATH` keeps working, and now names the default account's token.
What is new: multiple accounts, saved scheduling preferences, and eight new
tools (`find_focus_time`, `block_focus_time`, `detect_conflicts`,
`suggest_reschedule`, `time_audit`, `list_accounts`, `get_preferences`,
`set_preferences`).

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

<!-- MCP Registry ownership marker; do not remove -->
mcp-name: io.github.deciduus/calendar-mcp
