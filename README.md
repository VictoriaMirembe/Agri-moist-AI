# AgriMoist AI

A field assistant for agricultural extension officers in Karamoja, Uganda. Officers
record farm visits. The AI assistant checks rain and pest signals against each
plot's history and the local crop calendar, then drafts sourced advice on when to
plant, water, spray or hold off. **A named officer approves every piece of advice
before it reaches a household.** Farmers take part too: by USSD on any basic phone,
or on a simple farmer web page on a smartphone.

- **Track:** Agentic AI for Africa Challenge, Agriculture & Food Security
- **Theme:** Climate-smart advisory
- **Office served (demo data):** Napak District Production Office, Karamoja, Uganda
- **Team:** Ashelycole (core/agent), Victoria (officer app/approval gate), Evans (farmer channels/data) - see `AgriMoist_AI_Work_Plan_and_Build_Plan.pdf`

> **Status:** All nine tools, the LangGraph agent, the approval gate, officer
> screens, the USSD simulator and the farmer web page work end to end against real
> data and a real weather API. No `GROQ_API_KEY` is configured in this build
> environment, so advice drafting and translation run in a deterministic rule-based
> fallback mode rather than on the open-weights model - see "Known gaps" below.

## Run it (one command)

Requirements: Docker and Docker Compose.

```bash
git clone https://github.com/[account]/agrimoist-ai.git
cd agrimoist-ai
cp .env.example .env   # add your Groq key if you have one - works without it too
docker compose up --build
```

Then open <http://localhost:8000>. The app creates its SQLite database and loads the
made-up demo households on first start.

**Without Docker** (Python 3.12+):

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows: .venv\Scripts\activate ; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload
```

Keys needed in `.env` (all optional - the app runs fully without any of them):

| Key | What it is for | Where to get it |
| --- | --- | --- |
| `GROQ_API_KEY` | Open-weights model (`llama-3.3-70b-versatile` by default) that drafts advice and translates it. Blank = a deterministic, rule-based drafting mode is used instead, clearly labelled `rule-based-stub` in every run log. | <https://console.groq.com/keys> |
| `WEATHER_FAIL` | Set to `1` to make `get_weather` fail on purpose, to see the retry-then-mark-a-gap behaviour (EVALS task 4). | - |
| `WEATHER_OVERRIDE_PAST_MM` / `WEATHER_OVERRIDE_NEXT_MM` | Force a specific rain scenario instead of today's live weather, for a demo or an eval run. Always labelled `demo-override`, never shown as a live reading. | - |

Open-Meteo needs no key.

## Run the demo

1. Open the Home screen and click a household group, or open **Record visit** to add
   a new one.
2. Open a household group and click **Draft advice**. You land on the Agent run
   screen and watch each tool call (and its source) arrive live over
   server-sent events.
3. Open **Approvals**, read the draft and its sources (gaps show in red), then
   approve, edit-and-approve, or reject it as the officer.
4. Open the farmer page at `/farmer`, sign in with a demo number (`+256700000001`
   to `+256700000010`), and read **My advice** - only what was just approved shows.
5. Open the USSD simulator at `/ussd`, dial, and choose **1. My advice**. Same rule:
   nothing unapproved ever appears.
6. Open **Log** to see every tool call for every run, with its source, actor and
   cost.

To see a deliberate failure: set `WEATHER_FAIL=1` in `.env`, restart, and draft
advice again. The agent retries once, then marks "weather missing, check before
approving" instead of guessing a rain figure.

## How it works

Every piece of advice goes through five steps: **collect, check, draft, approve,
follow up.** The assistant handles checking and drafting. The officer handles
collecting, approving and following up. See [ARCHITECTURE.md](ARCHITECTURE.md).

More detail: [DESIGN.md](DESIGN.md) (screens, flows, approval states),
[DATA_SCHEMA.md](DATA_SCHEMA.md), [USSD_MENU.md](USSD_MENU.md), [RULES.md](RULES.md),
[docs/API_CONTRACT.md](docs/API_CONTRACT.md) and
[docs/TOOL_CONTRACT.md](docs/TOOL_CONTRACT.md).

## Farmer channels

Farmers never talk to the AI directly. They see only officer-approved advice, read
through the `advice_visible_to_farmer` database view, which only ever contains rows
whose status is `approved` or `edited_and_approved`.

| Channel | For | What farmers can do |
| --- | --- | --- |
| USSD simulator (`/ussd`) | Any basic phone, no internet | Report pests, rain or crop problems; read approved advice; ask for an officer visit; say whether advice helped |
| Farmer web page (`/farmer`) | Smartphones, weak signal | The same four actions |

Farmer reports are saved as "farmer-reported, not yet checked" and appear on the
officer's **Farmer reports** screen. The assistant may use them as signals, and each
sourced fact built from one says so (`farmer_ussd` or `farmer_web`, `unchecked`).

The USSD handler uses the same request and reply shape as a real USSD gateway (such
as Africa's Talking): a session id, a phone number, and the digits typed so far; a
reply starting `CON` (more screens) or `END` (session over). Going live means
pointing a real short code at `POST /ussd`. No real SMS or USSD messages are sent in
this build.

## Tools (our MCP server)

Built with the official `mcp` Python SDK (`backend/mcp_server/server.py`), exposing
nine tools - four that change data, five that only read:

| Tool | What it does | Type |
| --- | --- | --- |
| `record_observation` | Saves a field visit or a farmer report (USSD or web), marked by source | Writes |
| `get_field_records` | Reads recent visits and farmer reports for a household group | Reads |
| `get_plot_history` | Reads a plot's past crops, planting dates and harvests | Reads |
| `get_crop_calendar` | Gives planting and harvest windows for a crop in that agroclimatic zone | Reads |
| `get_weather` | Gets rain for the last 7 days and the forecast for the next 7, from Open-Meteo | Reads |
| `get_pest_reports` | Lists pest sightings from recent visits and farmer reports nearby | Reads |
| `draft_advice` | Writes sourced advice into the officer's approval queue; blocks it if any fact has no source | Writes |
| `flag_for_visit` | Flags a household group for an urgent officer visit | Writes |
| `schedule_followup` | Books a check-back date | Writes |

None of them can send a message, place an order, or request credit - see
[RULES.md](RULES.md).

**Borrowed MCP server:** [`mcp-server-time`](https://pypi.org/project/mcp-server-time/)
(the official Model Context Protocol reference server). The agent needs an
authoritative "today" in Africa/Kampala time to judge whether a crop's planting
window has closed; reimplementing timezone-safe clock handling ourselves would be
exactly the kind of small, well-solved problem borrowing is for. Connected over
stdio exactly like our own server (`backend/agent/mcp_client.py`).

**Agent:** LangGraph (`backend/agent/graph.py`), four nodes - plan, gather, check
sources, draft or flag - with one retry per failing tool call before the gap is
marked in the advice, never guessed.

**Model:** [Llama 3.3 70B](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct)
(open weights), served through [Groq](https://groq.com) using the `openai` client
with Groq's base URL, for one full advisory-drafting task and for translating
approved advice into Ngakarimojong. With no key configured, both fall back to a
deterministic, rule-based mode - see "Known gaps".

## Safety rules

- The assistant drafts only. It never sends advice, orders inputs or requests
  credit - no such tool or route exists in the codebase at all.
- Nothing reaches a household until a named officer approves it
  (`backend/approvals/router.py` is the only code allowed to set an advice status to
  `approved` or `edited_and_approved`).
- Every fact in a draft cites its source. A draft with an unsourced fact is set to
  `blocked` and can never enter the approval queue.
- If data is missing after one retry, the draft says so in plain words. It never
  guesses.
- Every tool call is logged with its inputs, outputs, time, source, actor and cost
  (`backend/logging/tool_log.py`).

## Data: where it came from and what it does not cover

| Data | Source | Limits |
| --- | --- | --- |
| Households, plots, visits | Made-up by the team (`backend/db/seed.py`). No real person's details anywhere. | A few messy records on purpose: a missing planting date, two visits with disagreeing soil readings, one household overdue a visit. Real records will be messier. |
| Rain forecast | [Open-Meteo](https://open-meteo.com) | A model forecast, not a ground measurement. |
| Crop calendar (sorghum, maize) | [FEWS NET Uganda-Karamoja Region Livelihood Zones report](https://fews.net/sites/default/files/documents/reports/Karamoja%20Final_LHZ%20Report.pdf) | Seasonal windows only (land prep Jan-Mar, planting Mar-Jul, harvest Aug-Dec for the agro-pastoral zone); covers sorghum and maize only. |
| Pest reports | Derived from the same visit/report records (no separate table) | Not a live feed; only as current as the last recorded visit or report. |

## Cost per run

See [EVALS.md](EVALS.md) for the real, measured figures (average time and cost per
agent run, from `scripts/run_evals.py`).

## Scaling beyond the hackathon

- **Real USSD and SMS:** connect `POST /ussd` and SMS sending to a gateway such as
  Africa's Talking, after officer approval only.
- **Farmer web page:** turn it into an installable offline web app (PWA).
- **More districts:** every record already carries a district, directly or through
  its village. Each district gets its own approval queue, officers, languages and
  crop calendar.
- **Bigger database:** move from SQLite to PostgreSQL when more than one office uses
  it (the app already uses SQLAlchemy/SQLModel, so this is a connection-string
  change, not a rewrite).

## Known gaps

- **No model key configured in this environment.** Drafting and translation run in a
  deterministic, rule-based fallback (clearly labelled `rule-based-stub` /
  `fallback` in the log and in EVALS.md), not the open-weights model. Add
  `GROQ_API_KEY` to `.env` to switch both on.
- **Translation is unverified** (EVALS task 10) for the same reason - see EVALS.md.
- **No real SMS or USSD sending** - the USSD menu is simulated in the browser at
  `/ussd`, using the real gateway request/reply shape.
- **Made-up data only** - 10 households, 2 villages, 3 groups; see `backend/db/seed.py`.
- **Docker was not run in this build environment** (no Docker available here); the
  app was verified directly with `uvicorn` plus the automated test suite instead.
  The Dockerfile and compose file follow the same `pip install -r requirements.txt`
  + `uvicorn` path, so they are expected to work, but that specific path is
  untested.
- **Agent run cost is always $0.00** in this environment (see above) - the cost
  field and its arithmetic are implemented and tested, but have never priced a real
  model call.

## Tests

52 automated tests in `tests/` (`pytest`), plus `scripts/run_evals.py`, which runs
the 14 EVALS.md tasks three times each against the real stack. See
[EVALS.md](EVALS.md) for the real results.

```bash
pytest
# or, inside Docker:
docker compose run app pytest
```

## Credits

- Borrowed MCP server: [`mcp-server-time`](https://pypi.org/project/mcp-server-time/)
  (Model Context Protocol project, MIT licence).
- Crop calendar: [FEWS NET Uganda-Karamoja Region Livelihood Zones
  report](https://fews.net/sites/default/files/documents/reports/Karamoja%20Final_LHZ%20Report.pdf).
- Weather: [Open-Meteo](https://open-meteo.com).
- Built with FastAPI, SQLModel, LangGraph, the `mcp` Python SDK, and the `openai`
  client against Groq's OpenAI-compatible API.

## Licence

MIT. See [LICENSE](LICENSE).
