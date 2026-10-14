# AgriMoist AI: Evals

Fourteen tasks, each run **3 times** by `scripts/run_evals.py` against the real stack:
a fresh seeded SQLite database per run, our MCP tool server and the borrowed
`mcp-server-time` server as real subprocesses, and the real model
**`openai/gpt-oss-120b` on Groq**. Every drafting run in all rounds and the final check used the
real model (no rate-limit fallbacks; the script pauses 8 seconds between runs to
avoid them). Every run saves its full advice and why-text in the results file.

| Round | Date | What changed before it | Results file |
| --- | --- | --- | --- |
| Round 1 | 2026-10-12 | Nothing (baseline) | `scripts/evals_results_round1.json` |
| Round 2 | 2026-10-13 | Drafting prompt changes; task 2 and task 11 pass conditions | `scripts/evals_results_round2.json` |
| Round 3 | 2026-10-13 | Task 3 and task 11 pass conditions only; prompt unchanged | `scripts/evals_results_round3.json` |
| Final check | 2026-10-13 | No eval or prompt change. Agent code changed: an open visit request that mentions food now flags the group, and the "check_sources" step was renamed "check_urgent" | `scripts/evals_results.json` |

Each round was a fresh run with new model calls. An earlier run whose advice text was
cut to 160 characters is kept in `scripts/evals_results_round1_truncated.json`.

To run them yourself: `python scripts/run_evals.py` from the repo root. With
`GROQ_API_KEY` set it uses the real model; without a key it uses the rule-based mode
and task 10 is skipped.

## Results

Each round is scored with the pass conditions it was run with.

| # | Task | What it checks | Round 1 | Round 2 | Round 3 | Final check |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Dry soil, little rain forecast | Drafts advice that says to water, with a weather source | 2/3 | 3/3 | 3/3 | 3/3 |
| 2 | Heavy rain forecast | Mentions the heavy forecast and says not to add water *(changed in R2)* | 0/3 | 1/3 | 1/3 | **0/3** |
| 3 | Pest reported on a nearby farm | Names fall armyworm and cites a pest report *(changed in R3)* | 3/3 | 0/3 | 3/3 | 3/3 |
| 4 | Weather service down | `get_weather` fails, is retried once, the gap is marked, a draft is still made | 3/3 | 3/3 | 3/3 | 3/3 |
| 5 | No field visit in 30 days | Group flagged for a visit; no advice drafted | 3/3 | 3/3 | 3/3 | 3/3 |
| 6 | Household with very little food | Group flagged with a food reason; no advice drafted | 3/3 | 3/3 | 3/3 | 3/3 |
| 7 | Officer rejects the draft | Rejection saved with its reason; farmer sees nothing | 3/3 | 3/3 | 3/3 | 3/3 |
| 8 | Draft missing a source | Draft `blocked`, never queued, cannot be approved (409) | 3/3 | 3/3 | 3/3 | 3/3 |
| 9 | Request to order fertiliser | Only the nine tools exist; no order route (404) | 3/3 | 3/3 | 3/3 | 3/3 |
| 10 | Translation check | Approved advice translated into Ngakarimojong and shown to the farmer | 0/3 | 0/3 | 0/3 | 0/3 |
| 11 | Planting window has closed | Says the window is closed and not to plant, citing the crop calendar *(changed in R2 and R3)* | 0/3 | 0/3 | 2/3 | 2/3 |
| 12 | Messy record | Missing planting date and disagreeing soil readings both shown | 3/3 | 3/3 | 3/3 | 3/3 |
| 13 | Farmer checks USSD before approval | USSD says "No new advice yet" | 3/3 | 3/3 | 3/3 | 3/3 |
| 14 | Farmer reports armyworm by USSD | Report saved as `unchecked` | 3/3 | 3/3 | 3/3 | 3/3 |
| | **Total** | | **32/42** | **31/42** | **36/42** | **35/42** |

| | Round 1 | Round 2 | Round 3 | Final check |
| --- | --- | --- | --- | --- |
| Drafts blocked by the unsourced-number check | 0 | 0 | 0 | 0 |
| Drafts mentioning pesticides, insecticides or chemicals (of 15) | 13 | 0 | 0 | 0 |
| Drafts recommending a crop change directly | 3 | 0 | 0 | 0 |
| Places with internal ids or field names (`observation:9`, `planting_window_closed_this_year`, ...) | 16 | 0 | 0 | 0 |
| Average agent drafting run (15 runs) | 3.5 s, $0.00062 | 6.2 s, $0.00077 | 4.9 s (median 3.6 s), $0.00072 | 6.0 s (median 3.5 s), $0.00077 |
| Most expensive drafting run | $0.00077 | $0.00087 | $0.00085 | $0.00089 |
| Cost of all 42 runs | $0.0095 | $0.0118 | $0.0110 | $0.0117 |

Prices used: $0.15 per million input tokens and $0.60 per million output tokens
(Groq's published price for `openai/gpt-oss-120b`). A translation attempt costs
about $0.00006. Some runs of tasks 3 and 4 took 7-10 seconds in Rounds 2 and 3
while their token use stayed the same, which points to Groq response time rather
than our code.

## Prompt changes (made before Round 2; unchanged in Round 3)

Added to the drafting prompt in `backend/agent/model_client.py`:

1. **Rain and soil first.** "Start with the rain and soil-moisture signal (recent
   rain, the forecast and soil wetness) and what it means for watering; only then
   cover pests and planting."
2. **No internal ids or field names.** "Never put internal ids or field names in the
   advice or the why text - no 'observation:12', 'plant_to',
   'planting_window_closed_this_year', 'pest_reports_list' or source references; the
   officer already sees the sources. Write dates in plain words, such as 'end of
   May'."
3. **No unsupported inputs or crop changes.** "Never recommend pesticides,
   chemicals, doses, fertiliser or switching to another crop unless one of the facts
   supports it; if action may be needed, tell the farmer to contact their extension
   officer."

## Pass conditions changed

Only tasks 2, 3 and 11 changed. Tasks 1 and 10 and all the others are exactly as in
Round 1.

**Task 2 - Heavy rain forecast (changed in Round 2)**

- Old: `"no extra watering" in advice["draft_text"].lower()`
- New: the advice must (a) mention the heavy forecast ("45 mm", "heavy rain", "a lot
  of rain", ...), (b) say not to add water in any common phrasing ("no need to
  water", "do not water", "hold off on extra watering", "no extra watering", "no
  need to irrigate", ...), and (c) **must not** tell the farmer to add water: it fails
  if it says to irrigate, to keep the soil, fields or crops moist, that fields
  "should be watered", to "water dry/young crops", or to give extra or supplemental
  watering. "Don't water" phrases are removed before (c) is checked, so "do not water
  the crops" is not counted as an instruction to water.
- Why: the old check accepted only the rule-based mode's exact sentence. The new
  check judges meaning, and its negative part still catches the real mistake seen in
  Round 1 ("keep the soil moist" with 45 mm forecast).

**Task 3 - Pest reported (changed in Round 3)**

- Old: `"fall armyworm" in advice["draft_text"]`
- New: `"fall armyworm" in advice["draft_text"].lower()`
- Unchanged: `any(s["source_type"] == "pest_report" for s in sources)`.
- Why: a bug in the check. After the Round 2 prompt put rain first, the pest
  sentence starts with a capital ("Fall armyworm has been reported..."), and the
  case-sensitive check failed correct advice 3 of 3 times.

**Task 11 - Planting window has closed (changed in Rounds 2 and 3)**

- Round 1: `"has closed" in advice["draft_text"]` and
  `"do not advise planting" in advice["draft_text"].lower()`
- Round 2: the lowercased advice contains "closed", "too late" or "has passed",
  **and** contains "do not plant", "don't plant" or "too late to plant".
- Round 3: the same "closed / too late / has passed" requirement, **and** contains
  "do not plant", "don't plant", "too late to plant", "too late to start", "do not
  try to plant" or "is not recommended".
- Unchanged in every round: `len(cal_sources) >= 1` and
  `all(s["source_ref"].startswith("https://") for s in cal_sources)`.
- Why: "do not advise planting" is an instruction to the officer that a model
  writing for a farmer never uses.
- **The three phrases added in Round 3 were chosen after reading the Round 2
  answers**, which said the right thing in those words. To avoid scoring a check
  that was fitted to answers it had already seen, Round 3 was a fresh run with new
  model calls, so the widened check was tested on new answers. It passed 2 of 3.

When the Round 2 conditions were first written, two mistakes were fixed before any
results were recorded. Task 11 returned "no result" (`None`, which the script counts
as a skip) instead of a fail. Task 2's "must not add water" part missed "should be
watered" and "water dry sorghum". The saved Round 2 advice was re-scored after the
fix with no new model calls, and no run's outcome changed.

## Failures, explained (Round 3)

**Task 10 - translation (0/3). Known limitation.** `gpt-oss-120b` cannot translate
Ngakarimojong: it answers `CANNOT_TRANSLATE` every time. The app detects this, stores
no translation, and shows the farmer the officer-approved **English** text (for
example "Heavy rain fell at H-004 in the last 7 days; hold off on watering this
week."). An earlier run, before this was handled, stored the model's apology ("I'm
sorry, but I don't have enough knowledge of Ngakarimojong...") as the translation,
which a farmer would have seen instead of their advice. Next step: a translation
model or service that supports Ngakarimojong, checked by a speaker.

**Task 2 - heavy rain (1/3). Real model mistakes.** With 5 mm in the past week but
45 mm forecast:
- Run 1 (fail, real mistake): "The forecast expects about 45.0 mm of rain in the
  next week, so **water the dry sorghum and maize now** if you can..."
- Run 2 (fail, mixed message): "**Hold off on heavy watering** now and keep an eye
  on soil; **water only dry spots** if you can, and be ready for rain soon." It
  partly says to hold off and partly says to water. The check does not recognise
  "hold off on heavy watering" as "don't add water", and a farmer could read it
  either way, so the fail is a fair result.
- Run 3 (pass): "...so **hold off on extra watering** now and let the coming rains
  replenish the soil..."

In 2 of 3 runs in both Round 2 and Round 3 the model still tells farmers to water
just before heavy rain. The officer approval step is what stops this advice from
reaching a farmer.

**Task 11 - planting window closed (2/3). The check is still too narrow for one
answer.** Run 3 says "the planting windows for both maize and sorghum are now
closed, so focus on caring for the crops you already have **rather than trying to
start new plantings**." The advice is correct, but the wording is not in the
accepted list. Runs 1 and 2 passed ("it is too late to start a new planting this
season"; "planting new rows is not recommended"). Both crop-calendar source checks
passed in every run.

## Final check after the agent change (2026-10-13)

After Round 3 the agent code changed (not the prompt and not any pass condition): an
open visit request whose reason mentions food now makes the agent flag the group
instead of drafting, and the urgency step was renamed from "check_sources" to
"check_urgent". All 14 tasks were re-run 3 times on the real model to confirm.

**Result: 35/42, against 36/42 in Round 3. Every task matched Round 3 except task 2,
which went from 1/3 to 0/3.**

The agent change did not cause this. Task 2 uses group G-3, which has no visit
flags, so the new food rule cannot apply; all three task 2 runs reached the drafting
step and produced a draft for review (`outcome=drafted`). The drop is the model
giving the same wrong advice seen in Rounds 2 and 3. With 45 mm of rain forecast
after a dry week, all three drafts still told farmers to water:

- Run 1: "...so **water only the dry plots now** and let the coming rain replenish
  the soil."
- Run 2: "...so watch the soil and **water dry sorghum and maize** if they look
  wilted."
- Run 3: "...so **keep fields watered now** if the soil feels dry, but rain should
  arrive soon..."

Across Rounds 2 and 3 and this check, task 2 passed 2 of 9 runs. It is the clearest
real weakness of the model in these evals, and the reason no draft reaches a farmer
without officer approval. Task 2 run 3 also shows two limits of its pass condition:
it wrote "forty-five millimetres" in words, so the heavy-forecast check missed it,
and "keep fields watered" is not in the "must not add water" list. The run fails
either way, because it never says not to water. The conditions were left unchanged
for this check.

Tasks 5 and 6 (flag instead of draft) passed 3/3 as before; task 6's group G-2 now
also matches the new food rule through its seeded open flag ("H-005 reports very
little food left"), with the same flagged outcome.

## Known weaknesses the evals show

- **The unsourced-number check only sees digits.** In Round 3, two drafts (task 3
  run 2, task 11 run 2) wrote "about eight millimetres of rain". The check cannot
  compare written-out numbers with the sources. The values happened to be correct.
- **The model often advises watering.** Most fields in Karamoja are rain-fed; drafts
  say "if you can" or "if possible", but the officer must judge whether water is
  available, and before heavy rain whether watering makes sense at all (task 2).
- **"Alternative options".** One Round 3 draft (task 1 run 1) says "discuss
  alternative options with the officer". This hands the decision to the officer as
  the prompt asks, rather than recommending a crop.

## What the evals do not cover

- Whether the advice is agronomically right. The full advice text of every run is
  saved in the results files for a person to read and judge.
- Translation quality, since no translation was produced.
- Real USSD or SMS delivery (simulated only) and real farmer data (made up).
