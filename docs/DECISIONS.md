# Walk2AI — Decision Log

Decisions that add to, change or sharpen the original specification. Each entry states
what was decided and why. Entries marked **needs approval** are proposals; the rest are
ordinary engineering choices that can still be overturned if you disagree.

| Status | Meaning |
|---|---|
| Proposed | Recommended; takes effect on approval of Phase 0 |
| Needs approval | Changes something the specification stated explicitly, or costs money |

---

## Changes to the specification

### D-01 · No Supabase; own PostgreSQL and first-party auth — *needs approval*

The specification offered Supabase "if appropriate". It is not, here. The platform has
one user and one server; the wallet needs row-level locking and strict control over who
can write; and FastAPI already has to authorise every request. Supabase would add an
external dependency, a second authentication system and a second route to the data.
Plain PostgreSQL in Docker (or DigitalOcean Managed PostgreSQL) with Argon2id passwords
and cookie sessions in FastAPI is smaller and safer. The schema is standard PostgreSQL,
so moving to Supabase later remains possible.

### D-02 · Caddy instead of Nginx as the reverse proxy — *needs approval*

The specification allows "Nginx or an appropriate reverse proxy". Caddy obtains and
renews certificates automatically with a few lines of configuration and streams
responses without tuning. Fewer ways to get TLS wrong. Nginx is a drop-in alternative
if you prefer it.

### D-03 · No Redis, Celery or message queue in the MVP — *proposed*

One user, one backend process. Reservation expiry runs as an in-process background
task; rate limiting is in-process plus the reverse proxy; everything stateful lives in
PostgreSQL. A queue can be added when there is something for it to do.

### D-04 · Registration closed by default — *proposed*

`REGISTRATION_MODE=first_user_only`: the first account becomes the administrator and
registration then closes. A personal platform connected to paid APIs should not accept
sign-ups from the internet.

### D-05 · Eight tables and one transaction type beyond the specification — *proposed*

Added tables: `auth_sessions`, `device_tokens`, `fitness_sync_batches`,
`workout_type_rules`, `daily_rewards`, `spending_limits`, `exchange_rates`, `audit_log`.
Added transaction type: `REWARD_REVERSAL`. Each exists because a stated requirement
(idempotent sync, scoped device access, deterministic rewards, spending limits, currency
conversion, audit logging, manual exclusion) had nowhere to live. Details in
[DATABASE.md](DATABASE.md).

### D-06 · API under `/api/v1`, with additional endpoints — *proposed*

All specified endpoints keep their paths under a versioned prefix. Additions cover
behaviour the specification requires but gave no endpoint for: cost estimation before
submission, device pairing, session refresh, workout exclusion, fitness data deletion,
spending limits, model catalogue management. Details in [API.md](API.md).

### D-07 · iOS requires a Mac and the paid Apple Developer Program — *needs approval (cost)*

US$99 per year. Evidence indicates a free Apple account cannot sign the HealthKit
entitlement, and free builds expire weekly in any case. Not needed until Phase 5; no
money is spent without asking.

### D-08 · Verify the Mi Fitness → Apple Health link now, not in Phase 5 — *proposed*

Whether workouts reach Apple Health is unknown and decides how workout rewards are
built. It takes ten minutes on your phone
([FITNESS_INTEGRATION.md](FITNESS_INTEGRATION.md#3-device-check-please-run-this-first)).
Doing it before Phase 2 avoids building a reward path on an assumption.

---

## Clarifications of the specification

### D-09 · Rounding

Rewards round **down** on the day's total; charges round **up** to a whole credit with
a minimum of 1 WC per billable request. The wallet can only err toward spending less
real money than was earned. Maximum rounding effect: 1 WC (€0.0001) per request.

### D-10 · The reward formula is evaluated per day, on totals

`WC(day) = min(cap, floor(0.1 × S) + floor(5 × M))`, computed from the day's full totals
every time, then compared with what the day has already been paid. This is what makes
late and repeated syncs safe. The 1,500 WC cap is a rule row, not a constant.

### D-11 · Reward rules are matched by activity date and cannot be edited once in force

A rule can be closed and succeeded, never rewritten. "Changing reward rates must not
retroactively alter historical rewards" then holds by construction, not by convention.

### D-12 · Downward corrections do not claw back automatically

HealthKit totals occasionally shrink when sources are re-merged. The amount paid for a
day is a high-water mark. Only an explicit exclusion of an already-rewarded workout
reverses credits, and never below a zero balance.

### D-13 · Late activity is rewarded for a bounded window

Activity arriving up to `REWARD_WINDOW_DAYS` (default 7) after its date still earns,
under that date's cap and rules. Older data is stored for history but earns nothing.

### D-14 · "Verified" is defined, not implied

Verified means: delivered by an authenticated paired device, not hand-entered in the
Health app, within plausibility bounds, not previously rewarded. It is not cryptographic
proof of exercise. See [SECURITY.md](SECURITY.md#honest-limits).

### D-15 · Walking and running distance are derived

HealthKit exposes one combined walking + running distance. Running distance comes from
running workouts; walking distance is the remainder. These are display metrics and do
not affect rewards.

### D-16 · A model without a verified price cannot be used

The specification says to mark unverifiable prices as unverified. The consequence is
made explicit: no verified price in force → the model is listed as unavailable. A request
that cannot be costed cannot be reserved against, so it is not sent.

### D-17 · Temperature and other controls are per-model capabilities

Several current models reject sampling parameters. The catalogue records what each model
supports and the interface offers only those controls.

### D-18 · Estimates are worst-case; charges are actual

The reservation covers `input tokens × price + max_output_tokens × price`. The charge
after completion uses the provider's reported usage. Where a stream is cut off before
usage is reported, the charge is a conservative estimate and is labelled `estimated` in
the usage record — the only case in which an estimate is ever charged.

### D-19 · Unknown outcomes release the hold and are flagged

If a request times out or the process dies with no information about what the provider
did, the reservation is released and the usage record is marked `unknown` for
reconciliation. The alternative (charging the full hold) would punish you for
infrastructure failures. The provider-side prepaid balance is the backstop for this
rare case.

### D-20 · The real spending ceiling is set at the provider

Prepaid balance, auto-recharge off, dedicated project/workspace, monthly limit. Walk2AI's
controls prevent overspending in normal operation; the provider-side settings bound the
damage if Walk2AI is ever wrong.

### D-21 · Conversations can be deleted; cost records cannot

Deleting a conversation removes its content. The usage record (tokens, cost, credits —
no content) remains, because the ledger must stay explainable.

### D-22 · Business parameters live in the database, not in environment variables

Reward rates, the cap, prices, exchange rates and limits are versioned rows managed
through the admin API. Environment variables hold infrastructure and secrets only.

---

## Open questions for you

These are the only inputs I need. Everything else has a default.

1. **Device check result** (D-08) — do workouts from the Watch S3 appear in Apple Health?
2. **Do you have a Mac with Xcode?** (D-07) — determines whether Phase 5 can be built at all.
3. **Supabase** (D-01) and **Caddy** (D-02) — accept the recommendations, or keep the originals?
4. **Your timezone** — defines when your "day" and its 1,500 WC cap roll over. It is a
   per-user setting and defaults to `UTC` until you tell me the IANA zone to use.
5. **USD→EUR rate policy** — a rate you set by hand (simple, default), or a daily official
   reference rate fetched automatically (later addition).
