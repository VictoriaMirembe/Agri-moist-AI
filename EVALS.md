# AgriMoist AI: Evals

Fourteen tasks, each run **3 times** by `scripts/run_evals.py` against the real stack:
a fresh seeded SQLite database per run, our MCP tool server and the borrowed
`mcp-server-time` server as real subprocesses, and the real model
**`openai/gpt-oss-120b` on Groq**. Every drafting run in this round used the real
model (no rate-limit fallbacks). Raw results: `scripts/evals_results.json`.

- **Date run:** 2026-10-12
- **Result:** 31 of 42 runs passed, 11 failed. Every failure is explained below.
- **Cost:** $0.0095 for all 42 runs. One agent drafting run costs about **$0.0006**
  (highest $0.0008) and takes about **3.4 seconds**.
- **Prices used:** $0.15 per million input tokens, $0.60 per million output tokens
  (Groq's published price for `openai/gpt-oss-120b`).

To run them yourself: `python scripts/run_evals.py` from the repo root. With
`GROQ_API_KEY` set it uses the real model and pauses 8 seconds between runs to stay
under Groq's tokens-per-minute limit; without a key it uses the rule-based mode and
task 10 is skipped.

## Results

| # | Task | Passed | Avg time | Avg cost | What it checks |
| --- | --- | --- | --- | --- | --- |
| 1 | Dry soil, little rain forecast | **1/3** | 2.65s | $0.00057 | Drafts advice that says to water, with a weather source |
| 2 | Heavy rain forecast | **0/3** | 3.53s | $0.00073 | Drafts advice containing "no extra watering" |
| 3 | Pest reported on a nearby farm | 3/3 | 2.98s | $0.00064 | Advice names fall armyworm and cites a pest report |
| 4 | Weather service down | 3/3 | 5.23s | $0.00056 | `get_weather` fails, is retried exactly once, the gap is marked, a draft is still made |
| 5 | No field visit in 30 days | 3/3 | 0.68s | $0 | The group is flagged for a visit; no advice is drafted |
| 6 | Household with very little food | 3/3 | 0.63s | $0 | The group is flagged with a food reason; no advice is drafted |
| 7 | Officer rejects the draft | 3/3 | 0.02s | $0 | Rejection is saved with its reason; the farmer sees nothing |
| 8 | Draft missing a source | 3/3 | 0.01s | $0 | The draft is `blocked`, never in the queue, and cannot be approved (409) |
| 9 | Request to order fertiliser | 3/3 | 0.01s | $0 | Only the nine tools exist; there is no order route (404) |
| 10 | Translation check | **0/3** | 0.68s | $0.00006 | Approved advice is translated into Ngakarimojong and shown to the farmer |
| 11 | Planting window has closed | **0/3** | 2.81s | $0.00061 | Advice says the window "has closed" and "do not advise planting", citing the crop calendar |
| 12 | Messy record | 3/3 | 0.00s | $0 | A missing planting date and disagreeing soil readings are both shown, not hidden |
| 13 | Farmer checks USSD before approval | 3/3 | 0.01s | $0 | USSD says "No new advice yet" |
| 14 | Farmer reports armyworm by USSD | 3/3 | 0.01s | $0 | The report is saved as `unchecked` |

## Failures, explained

**Task 10 - translation (0/3). A real failure of the model.** In every run
`gpt-oss-120b` replied that it cannot translate into Ngakarimojong. In the first
eval round this reply ("I'm sorry, but I don't have enough knowledge of
Ngakarimojong...") was stored as the translation and **would have been shown to
the farmer in place of their advice**. That was fixed before this round: the model
is now told to answer `CANNOT_TRANSLATE` when it cannot translate, any refusal is
detected, and the farmer is shown the officer-approved English text instead
(`backend/agent/model_client.py`). The task still fails because no translation is
produced. Next step: a model or service that supports Ngakarimojong, checked by a
speaker.

**Tasks 1, 2 and 11 - wording checks (1/3, 0/3, 0/3). Mostly a problem with the
checks, but not only.** These checks look for exact phrases that the rule-based
mode writes ("water", "no extra watering", "has closed", "do not advise
planting"). The model gives the same kind of advice in its own words, for example
"Do not plant maize now - the planting window for maize in Karamoja is closed for
this year" (task 11), so the phrase check fails. The checks need to test meaning,
not exact words. One drafted advice in the first round was also genuinely poor:
with 45 mm of rain forecast (task 2) the model said "keep the soil moist". That is
the kind of mistake the officer approval step exists to catch, and it is why no
advice reaches a farmer without it.

**Unsourced numbers.** The `draft_advice` tool blocks any draft whose wording uses a
number that is in none of its sources. In the first round this check wrongly blocked
one draft because the model numbered its points "1.", "2."; list numbering is now
ignored. In this round no draft was blocked. The check covers numbers only - the
officer still reads the wording against the listed sources.

## What the evals do not cover

- Whether the model's advice is agronomically right. The recorded text (first 160
  characters per run, in `scripts/evals_results.json`) needs a person to read it.
- Translation quality, since no translation was produced.
- Real USSD or SMS delivery (simulated only) and real farmer data (made up).
