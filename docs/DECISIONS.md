# Walk2AI — Decision Log

Decisions that add to, change or sharpen the original specification. Each entry states
what was decided and why.

| Status | Meaning |
|---|---|
| **Approved** | Decided by you |
| In force | An engineering choice made within the approved architecture; say so if you disagree |
| Superseded | Replaced by a later entry |

_Phase 0 was approved on 2026-11-13. Entries D-23 to D-31 were made during Phase 1,
D-32 to D-42 during Phase 2 (approved to proceed on 2026-11-14), D-43 onward during
Phase 3._

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

Sharpened by D-44: this applies when nothing was generated. Once generation has begun,
an outcome without reported usage is charged the worst case.

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

### D-32 · A reward rule can only start on a day that has not begun anywhere

The earliest start for a new or moved rule is the UTC date plus two days: the
furthest-ahead timezone is UTC+14, so by then the date has not started for anyone. This
is stricter than "tomorrow" on purpose — a rule starting "tomorrow" in Lisbon would
already be changing the current day of a user travelling in Asia. With D-11 it makes
"rate changes never alter historical rewards" true without exceptions.

### D-33 · A rule in force can only be ended; a scheduled rule can be changed or withdrawn

The specification's `PUT /admin/reward-rules/{id}` is implemented with that distinction.
Disabling a rule in force is not offered: it would change what late-arriving activity
for past days is worth. Moving or withdrawing a scheduled rule moves the end date of the
rule it succeeds with it, so the timeline never has a gap or an overlap.

### D-34 · Test activity has its own route, and a day has one authoritative source

Test data goes through `POST /fitness/sync/test` (administrator session), not the device
route, so the two cannot be confused and each has exactly the authentication it needs.
It is refused unless `ALLOW_TEST_FITNESS_DATA=true`; production refuses to start with
that set. Sources have a priority order and a day is paid from the first source that has
data for it — real activity always wins over test activity, and two sources are never
added together. Stored test rows earn nothing on an instance that forbids them.

### D-35 · Days are bucketed in the user's Walk2AI timezone, on the device too

The server tells the device which timezone to use (`GET /fitness/sync/status`) and
rejects a date that has not started in that timezone. The phone's own current timezone
is deliberately not used: travelling would otherwise shift which "day" activity belongs
to, and with it the daily cap.

### D-36 · Among duplicate workout records, the most complete one is kept

When the same session arrives under two ids (Mi Fitness and another app both writing to
Apple Health), the record with the longest active time is kept, then the earliest
received. The choice depends only on the records, not on arrival order, so it does not
flip between syncs. Independently, eligible minutes are bounded by the union of workout
intervals, so even an undetected duplicate cannot count a minute twice.

### D-37 · Deleting fitness data keeps the date and the credits awarded

Days, workouts and sync records are erased. Per day, the date and the credits already
awarded remain; without them, synchronising the same days again would pay them a second
time. This is the minimum needed to keep "never reward the same activity twice" true
after a deletion.

### D-38 · A workout deleted in Apple Health does not claw credits back

Consistent with D-12: a deletion that arrives by synchronisation is a data revision, not
an explicit decision in Walk2AI. The workout stops counting from then on; what was paid
stays paid. The explicit exclusion in Walk2AI remains the only path that reverses credits.

### D-39 · Spending limits are optional and default to none

A new account has no limits; the balance itself is the bound, and the wallet can never be
overdrawn. Limits count the *actual* cost of finished requests plus credits held for
requests in flight. An empty wallet is reported as "insufficient credits" before any
limit is considered, so the message points to earning rather than to settings.

### D-40 · Two meanings of "earned today", both available

`GET /wallet/summary` totals ledger entries *created* today (a reward for yesterday's
walk that arrives this morning counts toward today). `GET /fitness/daily` reports what
a given day's *activity* earned. The dashboard's "today's credits" will use the second.

### D-41 · Classifying a workout type can add credits, never remove them

Reclassification takes effect when a day is next evaluated (its next sync, or an explicit
recalculation) and only for days still inside the reward window. Because paid amounts
are a high-water mark, making a type ineligible does not take back what it already earned.

### D-42 · Hypothesis as a development dependency

The reward formula is checked with generated inputs as well as examples (never above a
cap, never negative, order-independent, duplicates never add minutes). Development-only;
nothing is added to the runtime image.

### D-43 · A closed browser does not cancel an AI request

The provider call runs in its own task; the HTTP response only reads its events. If the
connection drops, the request finishes on the server, is charged exactly, and the full
answer is saved to the conversation. Cancelling upstream would not make the request free
— the provider bills what it already generated — and most providers report no usage for
a cancelled stream, so the cost would become unknown. The price of this choice is paying
for an answer nobody watched arrive, bounded by the request's worst case.

### D-44 · A stream that breaks after generation began is charged its worst case

If the provider started generating and the stream then broke or timed out without
reporting usage, the request is charged the reserved worst case (`cost_basis =
estimated`). The provider will bill for it, and charging less could let real spend exceed
the credits that paid for it. If the provider refused before generating, nothing is
charged; a timeout before anything arrived is released and flagged (D-19).

### D-45 · No prompt caching, and requests name one exact model

Prompt caching is not enabled: cache writes cost more than plain input (1.25× for five
minutes, 2× for an hour on Anthropic) and pay off only when a long prefix is re-sent
within that time, which at one person's volume saves little. The catalogue already
stores every cache price and the pricing engine charges them, so enabling it later is a
small change. Every request names one exact model id and is priced at that model's
price; nothing lets a provider substitute a different model.

### D-46 · The USD→EUR rate follows the ECB's daily reference rate — **Approved** (2026-11-15)

No rate is invented or seeded. You chose an automatic daily rate: the European Central
Bank's euro reference rate (free, public, no account; published each working day around
16:00 CET). The backend fetches it at start-up and every three hours, outside any
request, and stores a new publication as a new rate version (`1 / USD-per-EUR`, rounded
up at the eighth decimal so a converted cost is never understated).

- If the ECB cannot be reached or its answer cannot be read, the rate in force stays in
  force; `GET /admin/exchange-rates/status` shows the rate's age and the last outcome.
- A figure outside 0.5–2.5 USD per EUR is discarded; a move of more than 10% from the
  previous ECB rate is not applied unattended (set it by hand if it is right).
- A rate set by hand holds until the ECB publishes a rate for that day or later.
  `EXCHANGE_RATE_SOURCE=manual` turns the automation off.
- Until a rate exists, USD-priced models are unusable (`no_exchange_rate`). Each request
  stores the rate it was charged at.

This adds no dependency: `httpx2` was already installed by both provider SDKs.

### D-47 · Prices are seeded only from the provider's own page

The three Claude 5.5 prices were read from Anthropic's pricing page and `gpt-6-luna`'s
from OpenAI's (standard tier: input $0.10, cached input $0.01, 30-minute cache write
$0.125, 12-hour cache write $0.25, output $0.50 per million tokens), both on 2026-11-15,
and are seeded as verified with those pages as their source. OpenAI's page was at first
unreachable from the build environment; a database seeded then holds an unverified
`gpt-6-luna` price, which the seed upgrades in place on the next start (only while it is
still unverified and unedited — it never charged anything). All seeded models start
disabled.

### D-48 · A verified price in force can never be edited

It may already have charged requests. Corrections are new versions; the old one ends
where the new one starts. An unverified price never charged anything (its model was
unusable), so it can be corrected and then verified. The same versioning applies to
conversion rates.

### D-49 · Conversation history is bounded, and only complete messages are replayed

Each request sends the last 20 complete messages of the conversation
(`AI_HISTORY_MESSAGES`). A failed question or a cut-off answer is kept and shown, but
not sent back to the model. Messages are ordered by a sequence number
(migration `0002`), not by timestamp, so order is exact even within one instant.

### D-50 · The AI gateway owns its transactions

An exception to D-26, because the gateway is an orchestrator around a slow external
call rather than a step in someone else's unit of work: it commits the reservation (with
the question and a pending usage record) before the provider is called, and settles in
one new transaction afterwards. No transaction is open while a model is answering.

### D-51 · Availability checks read metadata only

`POST /admin/models/{id}/check` asks the provider's models endpoint whether this key can
use a model. It generates nothing and costs nothing. A model found unavailable is
disabled.

### D-52 · Apple Health decimals are accepted and normalised on arrival

HealthKit reports every quantity as a double (`8423.0` steps, `6120.38291204` metres).
Distances and energy are rounded half-to-even to 0.01; steps and durations are rounded
down to whole numbers; negative, non-finite and non-numeric values are rejected.
Rejecting the whole batch over a trailing `.0` would have made real syncs fail.

### D-53 · Phase 4 is design-first: research, then design, then code — **Approved** (2026-11-15)

Your instruction. The frontend is not coded straight away:

1. **Research** — fintech dashboards, wallets and transaction histories, AI chat and
   model selection, fitness tracking, usage analytics, settings — analysed for
   navigation, layout, hierarchy, interaction and consistency. Inspiration only:
   Walk2AI gets its own identity, nothing is copied.
2. **Design system and main screens** — colour and semantic tokens, typography, spacing,
   navigation, components, chart standards, responsive layouts, and loading / empty /
   error / success states. The aim is a polished, premium fintech / AI product, not a
   generic admin dashboard.
3. **Your review** of the visual direction and main screens. Nothing is implemented
   before it.
4. **Implementation** on the approved stack (Next.js, React, TypeScript, Tailwind,
   shadcn/ui, Recharts) with the approved designs as the visual source of truth.

**Changed the same day — no Mobbin, no Figma, no spending.** The plan was to use the
Mobbin and Figma connectors. Mobbin's refused to work without a paid plan, and the
connected Figma account is on a free plan with a view-only seat. You decided: skip
Mobbin, do not use Figma, spend nothing on anything. The research is therefore written
from knowledge of established products ([design/RESEARCH.md](design/RESEARCH.md)), and
the designs are drawn directly as HTML and CSS on a private design canvas
([design/README.md](design/README.md)). Because the direction is a matter of taste,
three genuinely different directions are shown for the dashboard first; the full system
and the remaining screens follow the one you choose.

The design must make the chain *activity → credits earned → balance → AI consumed →
allowance left* immediately understandable, with one visual language across Dashboard,
AI Chat, Activity, Wallet, Usage Analytics and Settings. The backend architecture, API
contracts, financial calculations and wallet logic are not changed for visual
convenience; no unnecessary dependencies; no paid tools or subscriptions without your
approval; the design system is documented in the repository and reusable.

---

## Open questions for you

Nothing blocks Phase 4 development. These are needed along the way:

1. **Workout type labels** (Phase 5) — how your workouts are labelled in Apple Health.
   Until confirmed, an unexpected label earns nothing (your decision of 2026-11-15).
2. **Provider accounts and the first real request** — OpenAI and Anthropic API accounts
   with a small prepaid balance and auto-recharge off; your keys go only in the server's
   `.env`. With your approval, one real request per provider (cost: a fraction of a
   cent) confirms the integration end to end.
3. **Default spending limits** (optional) — none are set today (D-39).
