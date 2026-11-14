# Walk2AI — Backend API

> Status: **Authentication, devices, wallet, fitness, reward rules and health are
> implemented** (Phases 1–2). AI, analytics and the model/pricing administration are
> Phase 3. For what is implemented, the generated OpenAPI document at
> `/api/v1/openapi.json` (interactive at `/api/v1/docs`) is authoritative; this file is
> the guide to it and the contract for what is not yet built.
>
> | Group | State |
> |---|---|
> | Authentication, account, devices | Implemented |
> | Fitness (sync, views, exclusion, deletion) | Implemented |
> | Wallet (read) and spending limits | Implemented |
> | Administration | Wallet adjustments, reconciliation, audit log, reward rules, workout types, recalculation implemented; models, pricing and exchange rates in Phase 3 |
> | Operational | Implemented |
> | AI, Analytics | Phase 3 |

## Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/v1`. Paths below are relative to it. |
| Format | JSON, UTF-8. Timestamps ISO 8601 with offset. Dates `YYYY-MM-DD`. |
| Credits | Integers, field names end in `_wc`. |
| Money | Decimal **strings** (`"0.00070000"`), never JSON floats. Currency stated alongside. |
| Auth | Browser: `HttpOnly` session cookies. iOS: `Authorization: Bearer <device token>`. |
| Cross-origin | State-changing requests carrying an `Origin` other than the web app's, or `Sec-Fetch-Site: cross-site`, are refused with `403`. Requests with an `Authorization` header are exempt (they carry no ambient credentials). |
| Request bodies | Unknown fields are rejected with `422`. |
| Request id | Every response carries `X-Request-ID`; a valid incoming one is kept. Errors repeat it in the body. |
| Errors | `application/problem+json`: `{type, title, status, detail, code}`. `code` is stable and machine-readable. |
| Pagination | Cursor-based: `?limit=&cursor=` → `{items, next_cursor}`. |
| Idempotency | `POST /fitness/sync` on `batch_id`; `POST /ai/chat` on `request_id`. |
| Rate limits | Per route group; `429` with `Retry-After`. |

Error codes that matter to clients:

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | `not_authenticated` | No valid session or token |
| 401 | `invalid_credentials` | Wrong e-mail or password (deliberately the same answer for both) |
| 401 | `session_revoked` | A rotated refresh token was replayed; the login session was revoked |
| 401 | `invalid_pairing_code` | Pairing code wrong, used or expired |
| 403 | `forbidden` | Authenticated, not allowed (wrong role) |
| 403 | `insufficient_scope` | Device token lacks the scope this route needs |
| 403 | `registration_closed` | This instance does not accept new accounts |
| 403 | `account_disabled` | Correct credentials, suspended account |
| 403 | `cross_origin_rejected` | State-changing request from another origin |
| 402 | `insufficient_credits` | Balance cannot cover the worst-case estimate |
| 402 | `spending_limit_exceeded` | A per-request, daily, monthly or minimum-balance limit would be broken; the body's `limit` field says which |
| 403 | `test_data_disabled` | Test activity is not enabled on this instance |
| 404 | `workout_not_found` | No such workout for this user |
| 409 | `batch_id_reused` | A sync `batch_id` was already used with different content |
| 409 | `rule_in_force` | A reward rule that is already in force cannot be changed, only ended |
| 409 | `rule_conflict` | The reward rule would overlap another rule |
| 409 | `model_unavailable` | Model disabled, unverified price, or not available to this API account |
| 409 | `duplicate_request` | `request_id` already used |
| 409 | `email_taken` | An account with this e-mail exists |
| 409 | `idempotency_conflict` | Idempotency key reused with a different amount or type |
| 409 | `reservation_not_active` | The hold was already settled, released or expired |
| 422 | `validation_error` | Payload rejected; per-field details included (submitted values are never echoed) |
| 429 | `rate_limited` | Too many attempts; `Retry-After` says when to try again |
| 502 | `provider_error` | The AI provider failed; reservation released |
| 504 | `provider_timeout` | The AI provider timed out |

## Authentication

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/register` | none | Create an account. With the default `first_user_only` mode the first account becomes the administrator and registration then closes. Password: 12–128 characters. Does not sign in. |
| POST | `/auth/login` | none | Verify credentials, set session cookies. Returns the user, never a token. |
| POST | `/auth/refresh` *(added)* | refresh cookie | Rotate the refresh token and issue a new access token. |
| POST | `/auth/logout` | session (optional) | End the login session on the server, clear cookies. Always `204`. |
| GET | `/auth/me` | session | Current user, role, timezone. |
| PATCH | `/auth/me` *(added)* | session | Update display name and timezone (must be a valid IANA name). |
| POST | `/auth/password` *(added)* | session | Change the password; every other login session is signed out. |
| GET | `/auth/devices` *(added)* | session | List paired devices. Tokens are never shown again. |
| POST | `/auth/devices/pairing-code` *(added)* | session | Issue a one-time pairing code (`ABCD-EFGH`, valid 10 minutes). Replaces any unused one. |
| POST | `/auth/devices/pair` *(added)* | pairing code | Exchange the code for a device token (shown once). |
| GET | `/auth/devices/current` *(added)* | device token | Lets a paired device confirm its token still works. |
| DELETE | `/auth/devices/{id}` *(added)* | session | Revoke a device; its token stops working immediately. |

Session cookies: `w2a_access` (15 minutes, `Path=/`, `SameSite=Lax`) and `w2a_refresh`
(30 days, sliding, `Path=/api/v1/auth`, `SameSite=Strict`). Both `HttpOnly`, and `Secure`
in production. A web client calls `POST /auth/refresh` when it receives a `401`.

Rate limits: login 20 per minute per address and 10 per 5 minutes per account;
registration 10 per hour; pairing 10 per minute; password change 5 per 5 minutes.

## Fitness

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/fitness/sync` | device token (`fitness:sync`) | Upload daily totals and workouts. Idempotent on `batch_id`. |
| POST | `/fitness/sync/test` *(added)* | admin session | Submit clearly-labelled **test** activity. Refused unless `ALLOW_TEST_FITNESS_DATA=true`, which production forbids. |
| GET | `/fitness/sync/status` *(added)* | device token or session | Last sync, and what a device needs to sync by: the user's timezone and the reward and sync windows. |
| GET | `/fitness/daily` | session | One day (`?date=`, default today): steps, distances, minutes, credits. |
| GET | `/fitness/history` | session | Days with activity or rewards (`?from=&to=`, default the last 30 days, at most 366), newest first. |
| GET | `/fitness/workouts` | session | Workouts in a range, each with whether it earns minutes and why. `?include_deleted=true`. |
| PATCH | `/fitness/workouts/{id}` *(added)* | session | Exclude a workout from rewards, or include it again. |
| DELETE | `/fitness/data` *(added)* | session | Erase stored activity. Credits already earned remain. |

A device token can post activity and read the sync status. It cannot read activity back,
exclude workouts or delete data: those need the user's own session.

### `POST /fitness/sync`

The device sends facts. There is no field for credits, and unknown fields are rejected
with `422`, so a device cannot state or influence what its activity is worth.

```json
{
  "batch_id": "8f0c5a1e-3b0e-4d43-9a57-0c8e2a6f1b11",
  "device": { "timezone": "Europe/Lisbon", "app_version": "1.0.0" },
  "days": [
    {
      "date": "2026-11-12",
      "steps": 8000,
      "distance_walking_running_m": "6120.40",
      "distance_cycling_m": "0",
      "active_energy_kcal": "412.00"
    }
  ],
  "workouts": [
    {
      "external_id": "5E0B2C0A-6C1F-4F6B-9E57-2B1D0C7A9F10",
      "type": "traditionalStrengthTraining",
      "start": "2026-11-12T18:00:00+00:00",
      "end": "2026-11-12T18:30:00+00:00",
      "duration_seconds": 1800,
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

- `date` is a calendar day **in the user's Walk2AI timezone** (`GET /fitness/sync/status`
  returns it). The server rejects a date that has not started yet in that timezone.
- `type` may be HealthKit's own spelling (`traditionalStrengthTraining`,
  `HKWorkoutActivityTypeCycling`) or snake_case; the server normalises it.
- `duration_seconds` is active time, excluding pauses. `start` and `end` must carry a
  UTC offset.
- `days` should be HealthKit *statistics* (already merged across iPhone and Mi Fitness),
  never a sum of raw samples.

Response:

```json
{
  "batch_id": "8f0c5a1e-3b0e-4d43-9a57-0c8e2a6f1b11",
  "replayed": false,
  "source": "apple_healthkit",
  "is_test_data": false,
  "days": [
    { "date": "2026-11-12", "status": "accepted", "steps": 8000 }
  ],
  "workouts": [
    { "external_id": "5E0B2C0A-…", "status": "accepted", "type": "traditional_strength_training",
      "category": "non_step", "verified": true, "duplicate": false, "earns_minutes": true }
  ],
  "deleted_workouts": 0,
  "rewards": [
    { "date": "2026-11-12", "source": "apple_healthkit", "is_test_data": false,
      "steps": 8000, "eligible_minutes": 30,
      "step_credits_wc": 800, "workout_credits_wc": 150,
      "entitled_wc": 950, "capped": false,
      "awarded_now_wc": 950, "reversed_now_wc": 0, "awarded_total_wc": 950,
      "in_reward_window": true }
  ],
  "wallet": { "available_wc": 950, "reserved_wc": 0 }
}
```

`rewards` has one entry for every day the batch touched. `entitled_wc` is what the day is
worth under the rules of its own date; `awarded_now_wc` is what this request paid;
`awarded_total_wc` is what the day has been paid in total.

**Idempotency.** Sending the same `batch_id` again returns the stored result with
`"replayed": true` and changes nothing. The same `batch_id` with different content is
refused (`409 batch_id_reused`). Independently of batch ids, a day is only ever paid the
difference between what it is worth and what it has already been paid, so re-sending a
day in new batches is also harmless — a device should simply re-send the last several
days on every sync.

**Validation.** A rejected record never blocks the rest of the batch; it is returned with
`"status": "rejected"`, a stable `code` and a `detail`:

| Record | `code` | Meaning |
|---|---|---|
| day | `date_in_future` | The date has not started in the user's timezone |
| day, workout | `too_old` | Older than `SYNC_WINDOW_DAYS` (30) |
| day | `implausible_steps` | Outside 0 … `MAX_STEPS_PER_DAY` (100,000) |
| day, workout | `implausible_distance`, `implausible_energy` | Negative or absurd |
| workout | `invalid_interval` | Does not end after it starts |
| workout | `in_future` | Ends more than five minutes in the future |
| workout | `too_long` | Longer than `MAX_WORKOUT_HOURS` (12) |
| workout | `invalid_duration`, `duration_exceeds_interval` | Active time negative, or longer than start-to-end |
| workout | `invalid_type` | Not a recognisable type name |
| day, workout | `duplicate_in_batch` | Appears twice in the same batch |

A malformed payload (wrong types, missing fields, a timestamp without an offset, an
unknown field) is rejected as a whole with `422`.

**What earns.** Steps earn through the day's total. A workout earns minutes only if it
is verified (recorded by a device, not hand-entered), not excluded, not a duplicate, and
its type is classified `non_step`. Walking, running and hiking are `step_based`: they are
already paid through the step count. A type the server does not know is stored as
`unclassified` and earns nothing until an administrator classifies it. Activity older
than `REWARD_WINDOW_DAYS` (7) is stored but not paid (`"in_reward_window": false`).

### `GET /fitness/workouts`

Each workout carries `earns_minutes` and a `reason`:

| `reason` | Meaning |
|---|---|
| `eligible` | Earns workout minutes |
| `step_based` | Walking, running, hiking: paid through steps only |
| `unclassified_type` | Type not yet classified by an administrator |
| `type_excluded` | Type classified as never rewarded |
| `user_entered` | Typed into the Health app by hand: not verified |
| `duplicate` | The same session is already known under another id |
| `excluded` | Excluded by the user |
| `deleted` | Deleted in the source (shown only with `include_deleted=true`) |
| `source_not_rewardable` | From a source that cannot earn here (test data on an instance that forbids it) |

### `PATCH /fitness/workouts/{id}`

```json
{ "excluded_from_rewards": true, "reason": "Left the watch running" }
```

Excluding a workout that was already rewarded reverses those credits — the only path
that takes credits back. The reversal is capped at the available balance (the wallet
never goes negative) and the response reports `reversed_now_wc` per affected day.
Including it again pays it again, once.

### `DELETE /fitness/data`

Deletes stored days, workouts and sync records and returns how many of each. Per day,
only the date and the credits awarded are kept — without that, synchronising the same
days again would pay them twice. Credits already earned stay in the wallet.

## Wallet

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/wallet` | session | Available, reserved, lifetime earned and spent; EUR equivalent and the rate used. |
| GET | `/wallet/history` | session | Ledger rows, newest first. `?limit=` (1–200), `?cursor=`, `?type=`. |
| GET | `/wallet/summary` | session | Earned, spent and adjusted today and this month, and how much of each spending limit is used — in the user's timezone. |
| GET | `/wallet/reservations` | session | Holds, newest first. `?active_only=true`. |
| GET | `/wallet/limits` *(added)* | session | Current spending limits. `null` means no limit of that kind. |
| PUT | `/wallet/limits` *(added)* | session | Replace all spending limits (audit-logged). An omitted limit is removed. |

```json
GET /wallet
{
  "available_wc": 3300,
  "reserved_wc": 0,
  "lifetime_earned_wc": 1950,
  "lifetime_spent_wc": 650,
  "wc_per_eur": "10000.00000000",
  "available_eur": "0.3300",
  "updated_at": "2026-11-13T12:00:00Z"
}
```

`available_eur` is the internal allowance expressed in EUR, rounded down. It is not a
balance at any AI provider. It is `null` if no conversion rate is configured.

```json
GET /wallet/summary
{
  "timezone": "Europe/Lisbon",
  "today": { "date": "2026-07-15", "earned_wc": 250, "spent_wc": 0, "adjustments_wc": 0 },
  "month": { "month": "2026-07", "earned_wc": 650, "spent_wc": 0, "adjustments_wc": 0 },
  "spending": {
    "max_request_wc": 400,
    "min_balance_wc": 0,
    "alert_threshold_pct": 80,
    "daily":   { "limit_wc": 1000, "used_wc": 700, "held_wc": 150, "remaining_wc": 150,  "alert": true },
    "monthly": { "limit_wc": 3000, "used_wc": 700, "held_wc": 150, "remaining_wc": 2150, "alert": false }
  }
}
```

`today` and `month` total the ledger entries *created* in that period: a reward for
yesterday's walk that arrives this morning counts toward today here. For what a given
day's *activity* earned, use `GET /fitness/daily`.

### Spending limits

```json
PUT /wallet/limits
{ "max_request_wc": 400, "max_daily_wc": 1000, "max_monthly_wc": 20000,
  "min_balance_wc": 0, "alert_threshold_pct": 80 }
```

All limits are optional; with none set the only bound is the balance. They are checked
when credits are reserved for an AI request, before anything is sent to a provider:

| Limit | Rule |
|---|---|
| `max_request_wc` | The worst-case cost of one request may not exceed it |
| `max_daily_wc` | Charged today + currently held + this request ≤ limit |
| `max_monthly_wc` | Charged this month + currently held + this request ≤ limit |
| `min_balance_wc` | The available balance may not fall below it |

Days and months are the user's own (Europe/Lisbon by default). Only the *actual* cost
of a finished request counts as spent; a request that fails gives its allowance back.
A refusal is `402 spending_limit_exceeded` with `limit` naming which one. An empty
wallet is reported as `402 insufficient_credits` instead, so the user is told to earn
credits rather than to raise a limit. `alert` turns true once usage reaches
`alert_threshold_pct` of a limit.

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
| POST | `/admin/wallet/adjustments` *(added)* | **Implemented.** `ADMIN_ADJUSTMENT`: signed `amount_wc`, mandatory `reason`, optional `idempotency_key`. Cannot overdraw (`402`). |
| GET | `/admin/wallet/reconcile` *(added)* | **Implemented.** Verify balances equal ledger sums; list discrepancies. Read-only. |
| GET | `/admin/audit-log` *(added)* | **Implemented.** Newest first; `?limit=`, `?cursor=`, `?action=`. |
| GET | `/admin/reward-rules` | **Implemented.** All rule versions, each with `state`: `scheduled`, `in_force` or `ended` |
| POST | `/admin/reward-rules` | **Implemented.** Schedule a rule version; the rule currently open for that activity ends on the day the new one starts |
| PUT | `/admin/reward-rules/{id}` | **Implemented.** A scheduled rule can be edited freely; one in force can only be given an end date |
| DELETE | `/admin/reward-rules/{id}` *(added)* | **Implemented.** Withdraw a scheduled rule; the rule it would have succeeded carries on |
| GET | `/admin/workout-types` *(added)* | **Implemented.** How each workout type is treated |
| PUT | `/admin/workout-types/{type}` *(added)* | **Implemented.** Classify a type as `step_based`, `non_step` or `excluded` |
| POST | `/admin/rewards/recalculate` *(added)* | **Implemented.** Re-evaluate a user's days (at most 62) under the same rules a sync follows |
| GET | `/admin/models` *(added)* | Full catalogue including disabled models |
| POST | `/admin/models` *(added)* | Add a model (disabled until verified) |
| PUT | `/admin/models/{id}` *(added)* | Edit flags, enable or disable |
| POST | `/admin/models/{id}/check` *(added)* | Confirm availability against the provider with the configured key |
| GET | `/admin/model-pricing` | All price versions |
| POST | `/admin/model-pricing` *(added)* | Add a price version (closes the current one) |
| PUT | `/admin/model-pricing/{id}` | Edit an unverified or not-yet-effective row; mark verified with a source URL |
| GET / POST | `/admin/exchange-rates` *(added)* | View and add conversion-rate versions *(Phase 3)* |

### Changing reward rules

```json
POST /admin/reward-rules
{ "activity_type": "steps", "conversion_rate": "0.2", "effective_from": "2026-12-01" }
```

`activity_type` is `steps` (rate = WC per step), `workout_minutes` (rate = WC per
eligible minute) or `daily_total` (no rate; `daily_limit` is the day's cap). `steps` and
`workout_minutes` may also carry a `daily_limit` of their own.

A rule can only start on a date that has not begun anywhere on Earth — the UTC date
plus two days at the earliest. Rules are matched by *activity date*, so a day that has
already started is never revalued: late activity for 30 November is still paid at the
old rate when it arrives in December. A rule in force cannot have its rate or limit
changed (`409 rule_in_force`); schedule a successor instead.

Reclassifying a workout type takes effect the next time a day is evaluated (its next
sync, or `POST /admin/rewards/recalculate`). It can add credits for days still inside
the reward window; it never takes credits back.

## Operational

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness |
| GET | `/health/ready` | none | Readiness: database reachable and migrated to the revision this build expects; `503` otherwise |

## Additions to the specification's endpoint list

The specification's endpoints are all present with their original paths. The additions
exist because a required behaviour had no endpoint: session refresh, password change,
device pairing and revocation, labelled test activity, sync status, the pre-submission
cost estimate, workout exclusion, fitness data deletion, spending-limit management,
workout-type classification, reward recalculation, the model catalogue, exchange rates,
and wallet adjustment and reconciliation.
