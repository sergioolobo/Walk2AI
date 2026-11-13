# Walk2AI — Decision Log

Decisions that add to, change or sharpen the original specification. Each entry states
what was decided and why.

| Status | Meaning |
|---|---|
| **Approved** | Decided by you |
| In force | An engineering choice made within the approved architecture; say so if you disagree |
| Superseded | Replaced by a later entry |

_Phase 0 was approved on 2026-11-13. Entries D-23 onward were made during Phase 1._

---

## Approved by you

### D-01 · No Supabase; PostgreSQL directly, authentication in FastAPI — **Approved**

Supabase is not used. PostgreSQL runs as a container (or, later, a managed instance) and
authentication is implemented in the backend with established libraries: Argon2id through
`argon2-cffi` for passwords, PyJWT for signed access tokens, the standard library's
`secrets` for random tokens. One user, one server: the wallet needs row-level locking and
tight control over who can write, and FastAPI already authorises every request. The schema
is standard PostgreSQL, so a managed provider remains possible later.

### D-02 · Caddy as the reverse proxy, with automatic HTTPS — **Approved**

Caddy obtains and renews certificates automatically and streams responses without tuning.
It is configured in Phase 6. Until then the backend is reached directly on localhost.

### D-07 · iOS companion: no Mac, no purchases, no Apple Developer Program — **Approved (revised)**

You work on Windows and do not own a Mac. Therefore:

- Nothing in Phases 1–4 depends on Apple tooling, and none of it is blocked.
- The companion's source and architecture are prepared so that they can be compiled on
  macOS later. It is **not** built, signed or deployed until you decide how.
- No Apple hardware is purchased and no paid Apple Developer Program enrolment is made.
- **Before Phase 5** there is a dedicated investigation: what can be developed and tested
  without a paid membership and without owning a Mac (free provisioning limits, whether
  the HealthKit entitlement can be signed, 7-day expiry, hosted macOS build options and
  their cost, alternatives to a native app). Its findings are presented to you before any
  Phase 5 work or spending. See [ROADMAP.md](ROADMAP.md#before-phase-5--feasibility-investigation).

Because the backend validates everything the phone sends, the fitness pipeline can be
built and tested end to end in Phase 2 with labelled test data, without the companion.

### D-08 · Apple Health is a viable source for steps *and* workouts — **Approved (resolved)**

You confirmed on your own devices that workouts synchronise from Mi Fitness to Apple
Health. The planned architecture stands unchanged:

- Steps are the primary earning mechanism.
- Verified workouts earn additional credits under the reward rules.
- Walking and running are never paid twice: their workouts are classified `step_based`
  and earn through the step count only.
- The remaining detail for Phase 2 is the exact workout type names the watch produces,
  so that each is classified correctly (see open questions).

### D-23 · Application timezone: Europe/Lisbon — **Approved**

- Every timestamp is stored in UTC (`TIMESTAMPTZ`); the test-suite asserts there is no
  timezone-naive timestamp column in the schema.
- A user's "day" is a calendar day in their IANA timezone. New accounts get
  `Europe/Lisbon` (`DEFAULT_TIMEZONE`); it is also the database default for the column.
- Day and month boundaries are computed by `zoneinfo`, so daylight saving is automatic:
  the daily cap resets at 00:00 Portuguese time, which is 00:00 UTC in winter (WET) and
  23:00 UTC in summer (WEST). The transition days are 23 and 25 hours long; both are
  covered by tests.
- All conversion lives in one module (`app/core/timeutil.py`).

### D-24 · Wallet infrastructure brought forward into Phase 1 — **Approved**

You asked for wallet infrastructure, database integrity and concurrency / financial
integrity tests in Phase 1. `WalletService` (rewards, reservations, settlement, release,
expiry, adjustments, reconciliation) and its tests were therefore built now rather than
in Phase 2. Phase 2 keeps the parts that need fitness data: ingestion, the reward engine
and spending limits.

---

## Engineering decisions in force

### D-03 · No Redis, Celery or message queue in the MVP

One user, one backend process. Reservation expiry is an in-process background task; rate
limiting is in-process; everything stateful lives in PostgreSQL.

### D-04 · Registration closed by default

`REGISTRATION_MODE=first_user_only`: the first account becomes the administrator and
registration then closes. Also available: `open` and `closed`. Production refuses to start
with `open`. (The `invite` mode sketched in Phase 0 was dropped: it had no user.)

### D-05 · Tables and types beyond the specification

The schema has 21 tables: the 12 specified plus `auth_sessions`, `device_tokens`,
`device_pairing_codes`, `fitness_sync_batches`, `workout_type_rules`, `daily_rewards`,
`spending_limits`, `exchange_rates`, `audit_log`. One extra transaction type:
`REWARD_REVERSAL`. Details in [DATABASE.md](DATABASE.md).

### D-06 · API under `/api/v1`, with additional endpoints

All specified endpoints keep their paths under a versioned prefix. Additions cover
behaviour the specification requires but gave no endpoint for. Details in [API.md](API.md).

### D-09 · Rounding

Rewards round **down** on the day's total; charges round **up** to a whole credit with a
minimum of 1 WC per billable request. EUR equivalents shown to the user round down.

### D-10 · The reward formula is evaluated per day, on totals

`WC(day) = min(cap, floor(0.1 × S) + floor(5 × M))`, computed from the day's full totals
every time, then compared with what the day has already been paid. This is what makes
late and repeated syncs safe. The 1,500 WC cap is a rule row, not a constant.

### D-11 · Reward rules are matched by activity date and cannot be edited once in force

A rule can be closed and succeeded, never rewritten. The database rejects overlapping
ranges.

### D-12 · Downward corrections do not claw back automatically

The amount paid for a day is a high-water mark. Only an explicit exclusion of an
already-rewarded workout reverses credits — capped at the available balance and at what
was ever earned, so a reversal can neither overdraw the wallet nor consume an adjustment.

### D-13 · Late activity is rewarded for a bounded window

`REWARD_WINDOW_DAYS` (default 7).

### D-14 · "Verified" is defined, not implied

Delivered by an authenticated paired device, not hand-entered, within plausibility bounds,
not previously rewarded. Not cryptographic proof. See
[SECURITY.md](SECURITY.md#honest-limits).

### D-15 · Walking and running distance are derived

HealthKit exposes one combined walking + running distance.

### D-16 · A model without a verified price cannot be used

### D-17 · Temperature and other controls are per-model capabilities

### D-18 · Estimates are worst-case; charges are actual

### D-19 · Unknown outcomes release the hold and are flagged

### D-20 · The real spending ceiling is set at the provider

Prepaid balance, auto-recharge off, dedicated project/workspace, monthly limit.

### D-21 · Conversations can be deleted; cost records cannot

### D-22 · Business parameters live in the database, not in environment variables

### D-25 · Session model

A login creates a *login session*: a family of refresh tokens. Each refresh rotates the
token. The 15-minute access token is a signed JWT, but it is honoured only while its
login session is still alive in the database — so logout, password change, account
suspension and role changes take effect on the next request, not when the token expires.
Replaying an already-rotated refresh token revokes the whole login session (theft is
assumed); a 10-second grace window keeps two browser tabs refreshing at once from
signing the user out.

### D-26 · Routers commit; services never do

A service method runs inside the caller's transaction and flushes. The router commits
once the whole operation has succeeded, before the response is built. This lets the
reward engine and the AI gateway compose wallet operations into one atomic unit. A test
scans the source to keep `.commit()` out of the service layer.

### D-27 · The migrations are the schema; the models are the mapping

Migrations are hand-written SQL, because the parts that matter most (CHECK and EXCLUDE
constraints, immutability triggers) are the parts autogeneration does not understand.
The SQLAlchemy models map onto that schema. Tests always build the database by running
the real migrations, and one test proves models and schema agree exactly.
`docs/database/schema.sql` is generated from the migrated database (`make schema-doc`).

### D-28 · A small in-process rate limiter instead of a dependency

About sixty lines, fully tested, no external store — consistent with D-03. The reverse
proxy adds an edge limit in Phase 6.

### D-29 · Fitness sources are modular

A source is one member of `FitnessSource` plus the `CHECK` constraints that list the
allowed values. Adding Strava (or Health Connect) later means: a new enum member, a
one-line migration, and an ingestion adapter that produces the same normalised day and
workout records. Validation, de-duplication, the reward engine and the wallet do not
change.

### D-30 · Accounts with financial history are disabled, never erased

Foreign keys from the ledger are `RESTRICT`: a user with wallet history cannot be
hard-deleted, and neither can an administrator who authored an adjustment. Accounts are
suspended instead. The audit log survives user deletion (the actor reference is
detached; the row is otherwise immutable).

### D-31 · Schema refinements made while implementing (relative to the Phase 0 design)

| Change | Why |
|---|---|
| New table `device_pairing_codes` | One-time pairing codes need somewhere to live |
| `auth_sessions.family_id`, `revoked_reason` | Refresh-token families and reuse detection (D-25) |
| `wallet_reservations.description` | Human-readable context for a hold |
| IP addresses stored as `TEXT`, not `INET` | Audit display only; avoids driver-specific type handling |
| `wallet_transactions.created_by` is `RESTRICT` | A ledger row can never be rewritten, not even by a cascade |
| Audit-log trigger permits detaching a deleted actor | Otherwise `ON DELETE SET NULL` would be blocked by immutability |
| `users.timezone` defaults to `Europe/Lisbon` | D-23 |
| Every constraint and index is explicitly named | Stable names in errors, tests and future migrations |

---

## Open questions for you

Nothing blocks Phase 2. These improve it:

1. **Workout type names** — when convenient, tell me how your strength, cycling, walking
   and running workouts are labelled in Apple Health (Health → Browse → Activity →
   Workouts → open one). I have seeded sensible defaults; unknown types are stored but
   earn nothing until classified, so nothing can be over-rewarded in the meantime.
2. **USD→EUR rate policy** (needed in Phase 3) — a rate you set by hand (default), or a
   daily official reference rate fetched automatically.
