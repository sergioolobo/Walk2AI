# Walk2AI — Backend API

> Status: **Phase 0 design, awaiting approval.** This is the contract the backend will
> implement in Phases 1–3. Once the backend exists, the generated OpenAPI document at
> `/api/v1/openapi.json` is authoritative and this file becomes a guide to it.

## Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/v1`. Paths below are relative to it. |
| Format | JSON, UTF-8. Timestamps ISO 8601 with offset. Dates `YYYY-MM-DD`. |
| Credits | Integers, field names end in `_wc`. |
| Money | Decimal **strings** (`"0.00070000"`), never JSON floats. Currency stated alongside. |
| Auth | Browser: `HttpOnly` session cookies. iOS: `Authorization: Bearer <device token>`. |
| Errors | `application/problem+json`: `{type, title, status, detail, code}`. `code` is stable and machine-readable. |
| Pagination | Cursor-based: `?limit=&cursor=` → `{items, next_cursor}`. |
| Idempotency | `POST /fitness/sync` on `batch_id`; `POST /ai/chat` on `request_id`. |
| Rate limits | Per route group; `429` with `Retry-After`. |

Error codes that matter to clients:

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | `not_authenticated` | No valid session or token |
| 403 | `forbidden` | Authenticated, not allowed (wrong role or token scope) |
| 402 | `insufficient_credits` | Balance cannot cover the worst-case estimate |
| 402 | `spending_limit_exceeded` | Per-request, daily or monthly limit would be exceeded (`detail` says which) |
| 409 | `model_unavailable` | Model disabled, unverified price, or not available to this API account |
| 409 | `duplicate_request` | `request_id` already used |
| 422 | `validation_error` | Payload rejected; per-field details included |
| 502 | `provider_error` | The AI provider failed; reservation released |
| 504 | `provider_timeout` | The AI provider timed out |

## Authentication

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/register` | none | Create an account. Subject to `REGISTRATION_MODE`. |
| POST | `/auth/login` | none | Verify credentials, set session cookies. |
| POST | `/auth/refresh` *(added)* | refresh cookie | Rotate the session. |
| POST | `/auth/logout` | session | Revoke the session, clear cookies. |
| GET | `/auth/me` | session | Current user, role, timezone. |
| PATCH | `/auth/me` *(added)* | session | Update display name, timezone. |
| GET | `/auth/devices` *(added)* | session | List paired devices. |
| POST | `/auth/devices/pairing-code` *(added)* | session | Issue a short-lived one-time pairing code. |
| POST | `/auth/devices/pair` *(added)* | pairing code | Exchange the code for a device token (shown once). |
| DELETE | `/auth/devices/{id}` *(added)* | session | Revoke a device. |

## Fitness

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/fitness/sync` | device token (`fitness:sync`) | Upload daily totals and workouts. |
| GET | `/fitness/sync/status` *(added)* | device token or session | Last successful sync, server-side view of recent days. |
| GET | `/fitness/daily` | session | One day (default today): steps, distances, minutes, credits earned. |
| GET | `/fitness/history` | session | Daily records for a date range. |
| GET | `/fitness/workouts` | session | Workouts for a date range, with reward eligibility and reason. |
| PATCH | `/fitness/workouts/{id}` *(added)* | session | Exclude a workout from rewards, or re-include it. |
| DELETE | `/fitness/data` *(added)* | session | Delete stored fitness data (privacy). Credits already earned remain in the ledger. |

### `POST /fitness/sync`

The device sends facts. It never sends credit amounts, and any it did send would be ignored.

```json
{
  "batch_id": "8f0c5a1e-3b0e-4d43-9a57-0c8e2a6f1b11",
  "source": "apple_healthkit",
  "device": { "timezone": "Europe/Lisbon", "app_version": "1.0.0" },
  "days": [
    {
      "date": "2026-11-12",
      "steps": 8423,
      "distance_walking_running_m": "6120.40",
      "distance_cycling_m": "0",
      "active_energy_kcal": "412.00"
    }
  ],
  "workouts": [
    {
      "external_id": "5E0B2C0A-6C1F-4F6B-9E57-2B1D0C7A9F10",
      "type": "traditional_strength_training",
      "start": "2026-11-12T18:02:11+00:00",
      "end": "2026-11-12T18:35:40+00:00",
      "duration_seconds": 1920,
      "distance_m": null,
      "active_energy_kcal": "210.50",
      "origin_bundle_id": "<bundle id of the app that wrote the workout>",
      "origin_device": "Xiaomi Watch S3",
      "was_user_entered": false
    }
  ],
  "deleted_workout_ids": []
}
```

Response:

```json
{
  "batch_id": "8f0c5a1e-3b0e-4d43-9a57-0c8e2a6f1b11",
  "replayed": false,
  "days": [
    { "date": "2026-11-12", "status": "accepted", "steps": 8423, "eligible_minutes": 32,
      "entitled_wc": 1002, "awarded_now_wc": 160, "awarded_total_wc": 1002 }
  ],
  "workouts": [
    { "external_id": "5E0B2C0A-…", "status": "accepted", "category": "non_step" }
  ],
  "rejected": [],
  "wallet": { "available_wc": 3460, "reserved_wc": 0 }
}
```

Server-side validation (a rejected item never blocks the rest of the batch):

- Dates not in the future and not older than the accepted sync window.
- Steps, distances and durations within configured plausibility bounds.
- Workout `end` after `start`; active duration not longer than the interval.
- Known workout type; unknown types are stored as `excluded` and reported.
- `was_user_entered` workouts are stored but not verified, so not rewarded.
- `source = "test"` accepted only when `ALLOW_TEST_FITNESS_DATA=true` and only from an
  admin session; every resulting row and ledger entry is labelled as test data.

## Wallet

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/wallet` | session | Available, reserved, lifetime earned and spent; EUR equivalent and the rate used. |
| GET | `/wallet/history` | session | Ledger rows, newest first; filter by type and date. |
| GET | `/wallet/summary` | session | Earned and spent today / this month; remaining daily and monthly spending headroom. |
| GET | `/wallet/reservations` | session | Active and recent holds. |
| GET | `/wallet/limits` *(added)* | session | Current spending limits. |
| PUT | `/wallet/limits` *(added)* | session | Change spending limits (audit-logged). |

There is no endpoint that sets or changes a balance for an ordinary user. Balances move
only as a side effect of a sync, an AI request, or an admin adjustment.

## AI

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/ai/providers` | session | Providers and whether each is configured (key present, reachable). |
| GET | `/ai/models` | session | Usable models: enabled, available to this account, with a verified price. Includes capability flags and prices. |
| POST | `/ai/estimate` *(added)* | session | Cost estimate for a prospective message, without sending it. |
| POST | `/ai/chat` | session | Send a message; JSON or streamed response. |
| GET | `/ai/sessions` | session | Conversation list. |
| POST | `/ai/sessions` *(added)* | session | Create a conversation. |
| GET | `/ai/sessions/{id}` | session | A conversation with its messages and per-message usage. |
| PATCH | `/ai/sessions/{id}` *(added)* | session | Rename; change model. |
| DELETE | `/ai/sessions/{id}` | session | Delete a conversation and its messages (usage records are kept). |

### `POST /ai/estimate`

```json
{ "session_id": "…", "model_id": "…", "message": "…", "max_output_tokens": 1024 }
```

```json
{
  "input_tokens": 1840,
  "input_tokens_basis": "provider_count",
  "max_output_tokens": 1024,
  "worst_case": { "cost": "0.00069600", "currency": "USD", "cost_eur": "0.00064032", "credits_wc": 7 },
  "available_wc": 3300,
  "can_afford": true,
  "limits": { "request_ok": true, "daily_ok": true, "monthly_ok": true }
}
```

`input_tokens_basis` is `provider_count` when the provider's token-counting endpoint was
used and `local_estimate` (with a safety margin) otherwise. The UI labels estimates as
estimates; the charge shown after a request is the actual cost.

### `POST /ai/chat`

```json
{
  "request_id": "client-generated UUID",
  "session_id": "… or null to start a conversation",
  "model_id": "…",
  "message": "…",
  "max_output_tokens": 1024,
  "temperature": null,
  "stream": true
}
```

`temperature` is accepted only for models whose catalogue entry has
`supports_temperature = true`; otherwise it must be `null`.

With `"stream": true` the response is `text/event-stream`:

| Event | Data | When |
|---|---|---|
| `start` | `request_id`, `session_id`, `reserved_wc`, `estimate` | After the reservation succeeds, before the provider is called |
| `delta` | `text` | Each chunk of the answer |
| `usage` | tokens by category, `provider_cost`, `currency`, `cost_eur`, `charged_wc`, `released_wc`, `cost_basis`, `wallet` | After settlement |
| `error` | `code`, `detail`, `charged_wc`, `released_wc` | On failure, after the reservation has been settled or released |
| `done` | — | Always last |

A refusal for lack of credits or a limit is a plain `402` JSON response — the stream never
starts, and nothing is sent to the provider. With `"stream": false` the same information
arrives as one JSON object.

## Analytics

All accept `?from=&to=` (dates, user's timezone) and `?granularity=day|week|month`.

| Method | Path | Returns |
|---|---|---|
| GET | `/analytics/activity` | Steps, distances, workout minutes per period |
| GET | `/analytics/rewards` | Credits earned per period, split steps / workouts; cap hits |
| GET | `/analytics/ai-usage` | Requests, tokens (input / output / cached / reasoning) per period, per provider, per model |
| GET | `/analytics/spending` | Provider cost, EUR, credits per period, per provider, per model; averages per request and per 1,000 tokens; earned vs spent |
| GET | `/analytics/dashboard` *(added)* | The dashboard's figures in one round trip |

## Administration

Every route requires `role = admin`. Every write is recorded in `audit_log`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/reward-rules` | All rule versions |
| POST | `/admin/reward-rules` | Create a rule effective from a future date; closes the current one |
| PUT | `/admin/reward-rules/{id}` | Edit a rule that has not started; for one in force, only its end date or `enabled` |
| GET | `/admin/models` *(added)* | Full catalogue including disabled models |
| POST | `/admin/models` *(added)* | Add a model (disabled until verified) |
| PUT | `/admin/models/{id}` *(added)* | Edit flags, enable or disable |
| POST | `/admin/models/{id}/check` *(added)* | Confirm availability against the provider with the configured key |
| GET | `/admin/model-pricing` | All price versions |
| POST | `/admin/model-pricing` *(added)* | Add a price version (closes the current one) |
| PUT | `/admin/model-pricing/{id}` | Edit an unverified or not-yet-effective row; mark verified with a source URL |
| GET / POST | `/admin/exchange-rates` *(added)* | View and add conversion-rate versions |
| POST | `/admin/wallet/adjustments` *(added)* | `ADMIN_ADJUSTMENT` with a mandatory reason |
| GET | `/admin/wallet/reconcile` *(added)* | Verify balances equal ledger sums; list anomalies |
| GET | `/admin/audit-log` *(added)* | Read the audit log |

## Operational

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness |
| GET | `/health/ready` | none | Readiness (database reachable, migrations current) |

## Additions to the specification's endpoint list

The specification's endpoints are all present with their original paths. The additions
exist because a required behaviour had no endpoint: session refresh, device pairing and
revocation, the pre-submission cost estimate, workout exclusion, fitness data deletion,
spending-limit management, the model catalogue, exchange rates, and wallet adjustment and
reconciliation.
