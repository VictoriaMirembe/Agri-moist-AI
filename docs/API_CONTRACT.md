# AgriMoist AI: API Contract

Base URL in development: `http://localhost:8000`. All request and response bodies are
JSON unless noted. See `DATA_SCHEMA.md` for field definitions and `DESIGN.md` for the
approval states.

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
| `GET` | `/api/runs/{id}/events` | - | Server-sent events stream of tool calls as they happen (`tool_call`, `tool_result`, `tool_error`, `run_finished`) |
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
| `POST` | `/api/farmer-reports` | `{phone, problem, crop}` | Saved via `record_observation`, `checked=unchecked` |
| `GET` | `/api/farmer-reports` | `?checked=unchecked` | List of reports |
| `POST` | `/api/farmer-reports/{id}/check` | `{officer_id}` | Marks the observation `checked` |
| `GET` | `/api/flags` | `?status=open` | List of visit flags |
| `POST` | `/api/flags/{id}/visited` | `{}` | Marks a flag `visited` |
| `POST` | `/api/visit-requests` | `{phone, reason}` | Creates a visit flag, `raised_by=farmer_ussd` or `farmer_web` |

## Farmer advice

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/farmer/advice` | `?phone=` | Most recent `approved`/`edited_and_approved` advice for that phone's household group, translated text if available |
| `POST` | `/api/farmer/followup` | `{phone, advice_id, followed, helped}` | Saves a followup answer |

## USSD (simulated)

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/ussd` | `{session_id, phone, text}` (Africa's Talking request shape) | `CON ...` or `END ...` reply text |

## Logs and reference data

| Method | Path | Query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/logs` | `?run_id=` | Tool log rows, newest first |
| `GET` | `/api/runs` | - | Recent agent runs with model, outcome, cost and timing |
| `GET` | `/api/runs/{id}` | - | One run's summary |
| `GET` | `/api/officers` | - | Officer list, for the Visit form and Approval queue selectors |

## Approval states

```
drafted -> blocked           (a fact has no source)
drafted -> pending_review -> rejected            (reason recorded)
                           -> approved
                           -> edited_and_approved
                                  -> visible_to_farmer
                                        -> followed_up
```

Only `approved` and `edited_and_approved` advice can ever reach a farmer.
