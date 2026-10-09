# AgriMoist AI: Tool Contract

Our MCP server (`backend/mcp_server/server.py`, built with the official `mcp` Python
SDK's FastMCP). Implementations live in `backend/tools/agrimoist_tools.py` as plain,
testable functions; the MCP server wraps each one with
`backend.logging.tool_log.run_logged` so every call is logged once, however it was
invoked (MCP, the API, or the agent).

| Tool | Reads or writes | Inputs | Output | Source field |
| --- | --- | --- | --- | --- |
| `record_observation` | Writes | `household_code, source, crop?, soil_wetness?, rain_last_7_days?, pest_seen?, crop_condition?, food_left?, notes?, recorded_by?` | `{observation_id, household_code, group_id, checked, source_ref}` | `source` (officer_visit / farmer_ussd / farmer_web) |
| `get_field_records` | Reads | `group_id, days=60` | `{records[], households_in_group, days_since_last_visit_by_household}` | `database:observations` |
| `get_plot_history` | Reads | `group_id` | `{history[]}`, each row flags `planting_date_missing` | `database:plot_history` |
| `get_crop_calendar` | Reads | `zone, crop` | `{plant_from, plant_to, harvest_from, harvest_to, planting_window_open_today, planting_window_closed_this_year, source_ref}` | `source_ref` is the crop calendar's own `source_url` |
| `get_weather` | Reads | `latitude, longitude` | `{past_7_days_rain_mm, next_7_days_rain_mm, source_ref}` | Open-Meteo request URL |
| `get_pest_reports` | Reads | `group_id, days=21` | `{reports[]}` | `database:observations` |
| `draft_advice` | Writes | `group_id, draft_text, why_text, sources[], language="English"` | `{advice_id, status, blocked_reason}` | `sources[]` must each carry `fact, source_type, source_ref`; any missing one blocks the draft |
| `flag_for_visit` | Writes | `group_id, reason` | `{flag_id, group_id, reason, status}` | `assistant-flag` |
| `schedule_followup` | Writes | `advice_id, due_on` | `{followup_id, advice_id, due_on}` | `assistant-followup` |

All nine tools take an optional `run_id` so calls made during an agent run are tied to
that run's log rows and cost.

## Borrowed MCP server

**`mcp-server-time`** (official reference server from the Model Context Protocol
project, `pip install mcp-server-time`), run over stdio exactly like our own server.

**Why borrow it:** the agent needs an authoritative "today" in Africa/Kampala time to
decide whether a crop's planting window has closed (EVALS task 11). Reimplementing
timezone and DST-safe clock handling is exactly the kind of small, well-solved problem
that borrowing beats writing: one extra process call replaces code we would otherwise
have to write and test ourselves, with no advisory logic of our own at risk.

Only one tool from it is used: `get_current_time(timezone="Africa/Kampala")`. Its
result is cited in advice sources as `source_type: "clock"`,
`source_ref: "mcp-server-time:Africa/Kampala"`.
