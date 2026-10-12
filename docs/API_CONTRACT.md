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

## Officer authentication

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/officer/login` | Form fields `username`, `password` | Starts a signed officer session when bootstrap credentials match a real configured officer |
| `POST` | `/api/officer/logout` | - | Clears the current session |

Officer pages, officer APIs, and the approval API require the signed officer session;
otherwise they return HTTP 403. Actions that identify an officer must use the ID
bound to the current session. Configure the demo bootstrap account and session secret
in `.env`; the password is PBKDF2-hashed in the matching officer record. Never commit
the environment values.

The site root (`/`) redirects visitors to the farmer sign-in page and signed-in
officers to `/officer/dashboard`. The officer login page is `/officer/login`.

## Farmer reports and visit flags

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/farmer-reports` | `{phone, problem, crop}` | Saved via `record_observation`, `checked=unchecked` |
| `GET` | `/api/farmer-reports` | `?checked=unchecked` | List of reports |
| `POST` | `/api/farmer-reports/{id}/check` | `{officer_id}` | Marks the observation `checked` |
| `GET` | `/api/followups` | - | Scheduled and answered follow-ups for approved advice |
| `GET` | `/api/flags` | `?status=open` | List of visit flags |
| `POST` | `/api/flags/{id}/visited` | `{}` | Marks a flag `visited` |
| `POST` | `/api/visit-requests` | `{phone, reason}` | Creates a visit flag, `raised_by=farmer_ussd` or `farmer_web` |

## Farmer advice

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| `POST` | `/api/login` | Form field `phone` | Demo farmer contact lookup and `/farmer/home` redirect URL; does not authenticate officers |
| `GET` | `/api/farmer/profile` | `?phone=` | Registered household code and group for the farmer portal |
| `GET` | `/api/farmer/advice` | `?phone=` | Most recent `approved`/`edited_and_approved` advice for that phone's household group, translated text if available |
| `POST` | `/api/farmer/followup` | `{phone, advice_id, followed, helped}` | Saves a follow-up answer for approved advice belonging to that farmer's group |

Farmer reports and visit requests are saved against the registered phone's household
and appear in the officer's Farmer reports and Visit flags screens. Approved advice
is returned only to a registered household in its group. Farmer follow-up responses
are scoped to that household's approved advice and appear in the officer's Farmer
follow-ups screen. The phone-number login is only a demo lookup, not proof of
identity. Officer authentication is provided by the separately configured bootstrap
account; `/api/login` does not authenticate officers.

## USSD (simulated)

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/ussd` | Africa's Talking form fields `sessionId`, `serviceCode`, `phoneNumber`, `text`; JSON `{sessionId, phoneNumber, text}` remains supported by the simulator | `CON ...` or `END ...` plain text reply |

This callback uses the application's existing SQLite records and approval states:
farmers receive only `approved` or `edited_and_approved` advice, farmer reports are
saved as unchecked observations, and visit requests are saved as open visit flags.

## Logs and reference data

| Method | Path | Query | Returns |
| --- | --- | --- | --- |
| `GET` | `/api/logs` | `?run_id=` | Tool log rows, newest first |
| `GET` | `/api/runs` | - | Recent agent runs with model, outcome, cost and timing |
| `GET` | `/api/runs/{id}` | - | One run's summary |
| `GET` | `/api/officers` | - | The signed-in officer, for identity-bound action forms |

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
