# AgriMoist AI: API Contract

Base URL in development: `http://localhost:8000`. All request and response bodies are
JSON unless noted. Field definitions are in `backend/db/models.py`; the approval states are at
the end of this file.

## Visits and groups

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/visits` | `{household_code, officer_id, crop, soil_wetness, rain_last_7_days, pest_seen, crop_condition, food_left, notes}` | `{observation_id, household_code, group_id}` |
| `GET` | `/api/groups` | - | List of groups with household count, open flags, drafts waiting |
| `GET` | `/api/groups/{id}` | - | Group detail: households, recent visits, plot history, crop calendar window, last advice |

## Agent

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/groups/{id}/draft` | `{}` | `{run_id}` - starts an agent run in the background |
| `GET` | `/api/runs/{id}/events` | - | Server-sent events stream as the run happens: `plan`, `tool_call`, `tool_result`, `tool_error`, `check_urgent` (urgent food or overdue visit, so flag instead of draft), `model_call`, `model_result`, `run_error`, `run_finished` |
| `GET` | `/api/runs/{id}` | - | Run summary: model, outcome, cost, timing |

## Approvals

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/approvals` | `?status=pending_review` | List of advice rows with their sources |
| `POST` | `/api/approvals/{advice_id}/approve` | `{officer_id}` | Advice set to `approved` |
| `POST` | `/api/approvals/{advice_id}/edit` | `{officer_id, final_text}` | Advice set to `edited_and_approved` |
| `POST` | `/api/approvals/{advice_id}/reject` | `{officer_id, reason}` | Advice set to `rejected` (reason required) |

Only this router may change an advice row's status to `approved` or
`edited_and_approved` - see `backend/approvals/router.py`.

## Farmer reports and visit flags

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/farmer-reports` | `{phone, problem, crop, pest?}` | Saved via `record_observation`, `checked=unchecked`. Optional `pest` (problem `pests` only) is saved as the pest name; without it `pest_seen` is "farmer-reported pest" |
| `GET` | `/api/farmer-reports` | `?checked=unchecked` | List of reports |
| `POST` | `/api/farmer-reports/{id}/check` | `{officer_id}` | Marks the observation `checked` |
| `GET` | `/api/followups` | - | Scheduled and answered follow-ups for approved advice. `followed`, `helped`, `answered_via` hold the latest answer; `answers` lists each household's own answer: `[{household_code, followed, helped, answered_via, answered_at}]` |
| `GET` | `/api/flags` | `?status=open` | List of visit flags, each with `visited_by` (officer name or null) and `visited_at` |
| `POST` | `/api/flags/{id}/visited` | `{}` or `{officer_id}` | Marks a flag `visited`; records and logs the officer when given. Returns `{id, status, visited_by}`; 400 if the officer does not exist |
| `POST` | `/api/visit-requests` | `{phone, reason}` | Creates a visit flag, `raised_by=farmer_ussd` or `farmer_web`, reason "H-003 asked for a visit: little food left". An open flag whose reason mentions food makes the agent flag the group instead of drafting |
| `GET` | `/api/pending-summary` | - | `{pending_advice, unchecked_reports, open_flags, total_pending}` - counts for the officer nav badges and Home, refreshed every 30 seconds |

## Farmer advice

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/farmer/profile` | `?phone=` | Registered farmer name, household code, and household group |
| `GET` | `/api/farmer/dashboard` | `?phone=` | The farmer page's data: profile, latest observation, current weather (or `weather_error`) and crop calendar |
| `GET` | `/api/farmer/advice` | `?phone=` | Most recent `approved`/`edited_and_approved` advice for that phone's household group, translated text if available. When advice is returned, a `farmer_viewed_advice` log entry is written |
| `POST` | `/api/farmer/followup` | `{phone, advice_id, followed, helped}` | Saves a follow-up answer for approved advice belonging to that farmer's group |

Every `?phone=` and `phone` field accepts `+256...`, `256...` or `07...`, with or
without spaces and dashes.

Farmer reports and visit requests are saved against the registered phone's household
and appear in the officer's Farmer reports and Visit flags screens. Approved advice
is returned only to a registered household in its group. Farmer follow-up responses
are scoped to that household's approved advice and appear in the officer's Farmer
follow-ups screen.

## USSD (simulated)

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/ussd` | `{session_id, phone, text}` (Africa's Talking request shape) | `CON ...` or `END ...` reply text |

Menu: `1` my advice (logged as `farmer_viewed_advice`), `2` report a problem, `3` ask
for a visit, `4` did advice help (logged as `record_followup`). Option `4` replies "No
advice to answer yet" and saves nothing when the group has no approved advice.

## Logs and reference data

| Method | Path | Query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/logs` | `?run_id=` | Log rows, newest first: every tool call, plus `approve_advice`, `edit_and_approve_advice`, `reject_advice` (with translation model and cost), `mark_report_checked`, `mark_flag_visited`, `record_followup` and `farmer_viewed_advice` |
| `GET` | `/api/runs` | - | Recent agent runs with model, outcome, cost and timing |
| `GET` | `/api/runs/{id}` | - | One run's summary |
| `GET` | `/api/officers` | - | Officer list, for the Visit form and Approval queue selectors |

## Approval states

```
drafted -> blocked           (a fact has no source, or the wording uses a number no source contains)
drafted -> pending_review -> rejected            (reason recorded)
                           -> approved
                           -> edited_and_approved
                                  -> visible_to_farmer
                                        -> followed_up
```

Only `approved` and `edited_and_approved` advice can ever reach a farmer.

The `advice.status` column only ever holds `blocked`, `pending_review`, `rejected`,
`approved` or `edited_and_approved`. The last two steps are not statuses:

- **visible_to_farmer** means the advice is in the `advice_visible_to_farmer`
  database view (every `approved` / `edited_and_approved` row). Each time a farmer is
  shown it (web or USSD option 1) a `farmer_viewed_advice` log entry records the
  household and advice id. Nothing changes the advice's status.
- **followed_up** means a follow-up answer exists for the advice: a `followupanswer`
  row per household (and the latest answer on the advice's `followup` row).
