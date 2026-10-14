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
- **Team:** Ashelycole (core/agent), Victoria (officer app/approval gate), Evans (farmer channels/data)
- **How it is built:** see [ARCHITECTURE.md](ARCHITECTURE.md) (one page: agent flow, MCP servers, model, data)

> **Status:** All nine tools, the LangGraph agent, the approval gate, officer
> screens, the USSD simulator and the farmer web page work end to end against real
> data, a real weather API and the open-weights model `openai/gpt-oss-120b` on Groq.
> Without a `GROQ_API_KEY` the app still runs, using a deterministic rule-based
> drafting mode. Translation into Ngakarimojong does **not** work yet: the model
> declines, so farmers are shown the approved English text - see "Known gaps".

## The problem

In district extension offices like the Napak District Production Office in Karamoja, Uganda (our demo uses synthetic data for this office), one or two officers must turn rainfall, pest and visit information into advice for many smallholder households they cannot all visit in a season. Doing it by hand means cross-checking each household's plot history against the weather and the crop calendar, so most households never get timely advice specific to their plot. Karamoja has one unreliable rainy season, so late or wrong advice is costly.

## Who it is for

- **Extension officers:** draft, review and approve sourced advice for household groups, and see urgent cases flagged for a visit.
- **Farming households:** read approved advice on a simple web page or a USSD menu, report pests, request visits and say whether advice helped.

## Agent architecture

One LangGraph agent (`backend/agent/graph.py`) runs a fixed, auditable sequence: **plan** (locate the group) → **gather** (the four read tools, the borrowed clock, then the crop calendar for each crop found) → **check_urgent** → **draft_or_flag**. Urgent cases go to `flag_for_visit`; everything else is drafted by the model and saved with `draft_advice` for officer approval. Each call to our own tools is retried once, and a second failure is recorded as a gap, never guessed. Full details: [ARCHITECTURE.md](ARCHITECTURE.md).

## Technology stack

- **Language:** Python 3.12
- **Frameworks:** FastAPI, uvicorn, LangGraph, the `mcp` Python SDK (FastMCP), SQLModel, Jinja2 with plain JavaScript
- **Model:** openai/gpt-oss-120b (open weights) via Groq; a rule-based stub runs when no key is set
- **MCP servers:** our own AgriMoist server (9 tools) and the borrowed mcp-server-time, both over stdio
- **Database:** SQLite
- **External data:** Open-Meteo weather API; FEWS NET crop calendar (stored in `data/crop_calendar.json`)
- **Testing:** pytest and `scripts/run_evals.py`
- **Deployment:** Docker (Dockerfile + docker-compose). Live demo hosted on Render's free plan, set up through the Render dashboard (no config file in the repo): https://agri-moist-ai.onrender.com

## Run it (one command)

Requirements: Docker and Docker Compose.

```bash
git clone https://github.com/VictoriaMirembe/Agri-moist-AI.git
cd Agri-moist-AI
cp .env.example .env   # add your Groq key if you have one - works without it too
docker compose up --build
```

Then open <http://localhost:8000>. The app creates its SQLite database and loads the
made-up demo households on first start.

**Without Docker** (Python 3.12+):

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload
```

**Run the regression tests on Windows (PowerShell):** use the standard Windows
CPython installation, not the MSYS Python runtime. If `py -3.12` is unavailable,
install Python 3.12 for Windows from python.org, then open a new PowerShell
terminal. From the repository root, create an isolated environment and invoke its
Python directly:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.platform)"
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe -m pytest tests/test_farmer_and_ussd.py tests/test_officer.py
```

The printed executable should be inside this repository's `.venv` and the platform
should be `win32`. `--only-binary=:all:` requires published wheels, so pip will not
try to compile Pydantic, Ruff or other dependencies from source.

Keys needed in `.env` (all optional - the app runs fully without any of them):

| Key | What it is for | Where to get it |
| --- | --- | --- |
| `GROQ_API_KEY` | Open-weights model (`openai/gpt-oss-120b` by default, set by `GROQ_MODEL`) that drafts advice and tries to translate it. Blank = a deterministic, rule-based drafting mode is used instead, clearly labelled `rule-based-stub` in every run log. | <https://console.groq.com/keys> |
| `WEATHER_FAIL` | Set to `1` to make `get_weather` fail on purpose, to see the retry-then-mark-a-gap behaviour (EVALS task 4). | - |
| `WEATHER_OVERRIDE_PAST_MM` / `WEATHER_OVERRIDE_NEXT_MM` | Force a specific rain scenario instead of today's live weather, for a demo or an eval run. Always labelled `demo-override`, never shown as a live reading. | - |

Open-Meteo needs no key.

## Run the demo

On a desktop the officer screens are in the left sidebar: **Home**, **Approve
advice**, **Farmer messages**, **Visits needed**, **Record a visit**, and under
**More**: **Activity log**, **USSD simulator** and **Farmer page**. On a phone they are
in the bottom tabs: **Home**, **Approve**, **Messages**, **Visits** and **More**.

1. On **Home**, press **Open** next to a household group, or choose **Record a visit**
   to add a new one.
2. On the group's screen, press **Draft advice**. You land on the **AI draft** screen
   and watch each step arrive live over server-sent events (the raw tool calls and
   their sources are under **Technical details**).
3. Open **Approve advice**, read the draft and its sources (gaps show in red), then
   press **Approve**, **Save edit & approve**, or **Reject** as the officer.
4. Open **Farmer page** (under **More**, or go to `/farmer`), sign in with a demo
   number (`+256700000001` to `+256700000010`), and read **My advice** - only what was
   just approved shows.
5. Open **USSD simulator** (under **More**, or go to `/ussd`), press **Dial \*123#**,
   and tap **1** (My advice). Same rule: nothing unapproved ever appears.
6. Open **Activity log** (under **More**) to see every tool call for every run, with
   its source and actor, and each run's cost.

To see a deliberate failure: set `WEATHER_FAIL=1` in `.env`, restart, and draft
advice again. The agent retries once, then marks "weather missing, check before
approving" instead of guessing a rain figure.

## How it works

Every piece of advice goes through five steps: **collect, check, draft, approve,
follow up.** The assistant handles checking and drafting. The officer handles
collecting, approving and following up.

More detail: [docs/API_CONTRACT.md](docs/API_CONTRACT.md) (every route and the
approval states), [docs/TOOL_CONTRACT.md](docs/TOOL_CONTRACT.md) (the nine tools),
[EVALS.md](EVALS.md) (measured results) and
[docs/user-research.md](docs/user-research.md).

## Farmer channels

Farmers never talk to the AI directly. They see only officer-approved advice, read
through the `advice_visible_to_farmer` database view, which only ever contains rows
whose status is `approved` or `edited_and_approved`.

| Channel | For | What farmers can do |
| --- | --- | --- |
| USSD simulator (`/ussd`) | Any basic phone, no internet | Report pests, rain or crop problems; read approved advice; ask for an officer visit; say whether advice helped |
| Farmer web page (`/farmer`) | Smartphones, weak signal | The same four actions |

Farmer reports are saved as "farmer-reported, not yet checked" and appear on the
officer's **Farmer messages** screen (**Reports** tab). The assistant may use them as
signals, and each sourced fact built from one says so (`farmer_ussd` or `farmer_web`,
`unchecked`).

The web portal and the extension-officer portal share the same backend records:
farmer sign-in resolves the registered household, reports appear in **Farmer
messages**, visit requests appear in **Visits needed**, and officers can open the
affected group directly from those queues. After an officer approves advice, the
farmer can read it and submit a follow-up; officers see scheduled and answered
follow-ups in **Farmer messages** (**Did advice help?** tab), with each household's
own answer listed.
Follow-up answers are accepted only for approved advice belonging to that farmer's
household group; visit requests name the household that asked.

The active farmer portal is served at `/farmer` from `frontend/farmer/templates`.
Its dashboard, advice, report, visit and follow-up views share one dashboard
template; sign-in and the USSD simulator remain separate entry screens. The old
unmounted root-level farmer portal prototypes, which used the inactive
`/api/reports` endpoint, have been removed. The phone-number check in this demo
resolves a seeded household; it is not production authentication.

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
"Safety rules" below.

**Borrowed MCP server:** [`mcp-server-time`](https://pypi.org/project/mcp-server-time/)
(the official Model Context Protocol reference server). The agent needs an
authoritative "today" in Africa/Kampala time to judge whether a crop's planting
window has closed; reimplementing timezone-safe clock handling ourselves would be
exactly the kind of small, well-solved problem borrowing is for. Connected over
stdio exactly like our own server (`backend/agent/mcp_client.py`).

**Agent:** LangGraph (`backend/agent/graph.py`), four nodes - plan, gather, check urgent
(urgent food or overdue visits get flagged instead of drafted), draft or flag - with one retry per failing call to our own MCP tools before the gap is
marked in the advice, never guessed. A failed call to the borrowed clock is not retried;
it is recorded as a gap.

**Model:** [gpt-oss-120b](https://huggingface.co/openai/gpt-oss-120b) (open weights,
Apache 2.0), served through [Groq](https://groq.com) using the `openai` client with
Groq's base URL, for drafting advice and for translating approved advice into
Ngakarimojong. With no key configured, drafting falls back to a deterministic,
rule-based mode and farmers see the English text.

## Safety rules

- The assistant drafts only. It never sends advice, orders inputs or requests
  credit - no such tool or route exists in the codebase at all.
- Nothing reaches a household until a named officer approves it
  (`backend/approvals/router.py` is the only code allowed to set an advice status to
  `approved` or `edited_and_approved`).
- Every fact in a draft cites its source. A draft with an unsourced fact is set to
  `blocked` and can never enter the approval queue. A draft whose wording uses a
  number that appears in none of its sources (a rain figure, a date, a dose the model
  made up) is also `blocked`. This catches invented numbers only: the officer still
  has to read the wording against the sources listed under it.
- If data is missing after one retry, the draft says so in plain words. It never
  guesses.
- Every tool call, every approve / edit / reject decision, and every officer and
  farmer action that saves something (mark checked, mark visited, follow-up
  answers) is logged with its inputs, outputs, time, source and actor
  (`backend/logging/tool_log.py`), as is each time a farmer is shown approved advice.
  Each agent run records its model and cost; each approval records the translation
  model and cost.

## Data: where it came from and what it does not cover

| Data | Source | Limits |
| --- | --- | --- |
| Households, plots, visits | Made-up by the team (`backend/db/seed.py`). No real person's details anywhere. | A few messy records on purpose: a missing planting date, two visits with disagreeing soil readings, one household overdue a visit. Real records will be messier. |
| Rain forecast | [Open-Meteo](https://open-meteo.com) | A model forecast, not a ground measurement. |
| Crop calendar (sorghum, maize) | [FEWS NET Uganda-Karamoja Region Livelihood Zones report](https://fews.net/sites/default/files/documents/reports/Karamoja%20Final_LHZ%20Report.pdf) | Seasonal windows only (land prep Jan-Mar, planting Mar-Jul, harvest Aug-Dec for the agro-pastoral zone); covers sorghum and maize only. |
| Pest reports | Derived from the same visit/report records (no separate table) | Not a live feed; only as current as the last recorded visit or report. |

## Cost per run

Measured on `openai/gpt-oss-120b` over 15 real drafting runs in the final check
(see [EVALS.md](EVALS.md)): **about $0.0008 per agent run** (average
$0.00077, highest $0.00089) and **about 6.0 seconds** on average (median 3.5
seconds). A translation attempt costs about $0.00006. Prices used: $0.15 per million input
tokens and $0.60 per million output tokens (Groq's published price for this model).

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
- **Photo reports are not built yet.** Farmers can pick the problem and the pest from a
  list, but cannot attach a photo of a damaged crop. Next step: optional photo upload
  on the farmer web page for smartphone users, shown to the officer for diagnosis (the
  officer still decides). USSD users on basic phones cannot send photos, and our
  open-weights model reads text only, so photos would support the officer, not the AI
  draft.
- **Community alerts.** Officer-approved alerts to all farmers in an area (for example,
  a disease outbreak warning), using the same approval gate and log as advice: a draft
  alert is shown to farmers only after a named officer approves it.

## Known gaps

- **Translation into Ngakarimojong does not work.** The model replies that it cannot
  translate the language (EVALS task 10 failed 3 of 3). The app detects this and
  shows farmers the officer-approved English text instead; no translation has been
  checked by a speaker.
- **The model can still give wrong advice.** In the final check it told farmers to
  water just before 45 mm of forecast rain in 3 of 3 runs (7 of 9 across the last
  three runs; EVALS task 2).
  The officer approval step is what stops this reaching a farmer - see EVALS.md.
- **The model's wording is not checked fact-by-fact against the sources.** Only
  numbers are checked automatically, and only numbers written as digits ("8 mm",
  not "eight millimetres"). The officer reviews the rest.
- **No login for officers.** The officer picks a name from an "Acting as" list; no
  password or account is checked. Fine for a demo, not for real use.
- **Farmer sign-in is a phone-number lookup only.** Anyone who types a registered
  number (`+256...`, `256...` or `07...`) sees that household's advice and details;
  it is not authentication.
- **The consent checkbox is browser-only.** The tick box on the visit form is not
  sent to the server; the server checks the consent stored on the household record.
- **No real SMS or USSD sending** - the USSD menu is simulated in the browser at
  `/ussd`, using the real gateway request/reply shape.
- **USSD pest reports cannot name the pest.** The USSD menu only offers "Pests", so
  those reports say "farmer-reported pest"; the farmer web page lets the farmer pick
  one.
- **Only "no food left" in a visit record counts as urgent.** A field record with
  `food_left` "none", or an open visit request that mentions food, makes the agent
  flag the group instead of drafting; a record of "little" food left does not.
- **Officer screens refresh every 30 seconds, not instantly**, and pause while the
  browser tab is hidden. A group added after a page was opened appears on Home after
  a reload.
- **"Seen by the farmer" is a log entry, not a status.** Reading approved advice (web
  or USSD) writes a `farmer_viewed_advice` log row; the advice status stays
  `approved` / `edited_and_approved` - see `docs/API_CONTRACT.md`.
- **Made-up data only** - 10 households, 2 villages, 3 groups; see `backend/db/seed.py`.
- **No registration screen yet.** Households, farmer contacts and groups come from the
  synthetic seed data (`backend/db/seed.py`), so no real farmer's data is used without
  consent. Next step: an officer form to register a household and farmer that records
  the farmer's consent before saving.
- **Docker was not run in this build environment** (no Docker available here); the
  app was verified directly with `uvicorn` plus the automated test suite instead.
  The Dockerfile and compose file follow the same `pip install -r requirements.txt`
  + `uvicorn` path, so they are expected to work, but that specific path is
  untested.

## Tests

Automated tests in `tests/` (`pytest`). Tests always use the rule-based mode, so
they never call Groq even if `.env` has a key. `scripts/run_evals.py` runs the 14
EVALS.md tasks three times each against the real stack, including the real model
when a key is set. See [EVALS.md](EVALS.md) for the real results.

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
