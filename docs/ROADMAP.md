# Walk2AI — Implementation Roadmap

Each phase ends with working software, passing tests and updated documentation. A phase
does not start until the previous one is accepted. Items needing something from you are
marked **(you)**.

## Phase 0 — Architecture and repository preparation — done

- [x] Inspect the development environment and toolchain
- [x] Evaluate the proposed technology stack
- [x] Research the Xiaomi Watch S3 → Mi Fitness → Apple Health pipeline
- [x] Architecture, database design, API contract, security and development documents
- [x] Repository skeleton and environment configuration
- [x] **(you)** Architecture approved (2026-11-13)
- [x] **(you)** Device check: workouts do reach Apple Health

## Phase 1 — Backend foundation — done

- [x] Backend project: `pyproject.toml`, uv lockfile, Ruff, mypy, pytest
- [x] Typed settings with startup validation (stricter in production)
- [x] Async database session management
- [x] SQLAlchemy models for all 21 tables
- [x] Alembic: initial migration with constraints, triggers, extensions; tested up, down and up again
- [x] Seed command (reward rules, workout type rules, EUR→WC rate)
- [x] Authentication: register, login, refresh, logout, profile, password change
- [x] Argon2id passwords; rotating refresh tokens with theft detection
- [x] Roles and the admin dependency; registration modes
- [x] Device pairing and scoped device tokens
- [x] Error handling (problem+json), request ids, structured logging
- [x] Rate limiting; audit log
- [x] Health and readiness endpoints
- [x] Europe/Lisbon day and month boundaries, daylight saving included
- [x] Backend Dockerfile; `docker compose up` runs database, migrations and API
- [x] Test foundation: real-PostgreSQL fixtures, controllable clock, API client
- [x] CI workflow: lint, types, tests, image build

Brought forward from Phase 2 at your request:

- [x] `WalletService`: reward, reverse, reserve, settle, release, expire, adjust
- [x] Immutable ledger with idempotency keys
- [x] Reservation expiry task
- [x] Reconciliation (balances = ledger sums), as endpoint and CLI
- [x] Wallet read endpoints; audited admin adjustments
- [x] Concurrency and financial-integrity tests

Result: 299 automated tests passing; the stack was also started with Docker Compose and
exercised end to end over HTTP.

## Phase 2 — Fitness ingestion and reward engine — done, awaiting your review

- [x] Sync payload schemas and server-side validation (plausibility bounds, dates, types)
- [x] `POST /fitness/sync`: batch idempotency, upsert of days and workouts
- [x] A source-agnostic ingestion pipeline, with Apple HealthKit as the first source
      and a clearly-labelled test source (so Strava or others can be added later)
- [x] Workout handling: duplicates, overlaps, midnight splitting in the user's timezone,
      deletion, manual exclusion
- [x] `RewardEngine`: rule lookup by activity date, `min(cap, floor(0.1·S) + floor(5·M))`,
      per-day reward ledger, delta payout through `WalletService`
- [x] Walking and running paid through steps only; other workouts earn minutes
- [x] Late-arrival window; high-water mark; `REWARD_REVERSAL` on explicit exclusion
- [x] Spending limits and the checks applied at reservation time
- [x] Endpoints: `/fitness/*`, `/wallet/limits`, `/admin/reward-rules`,
      `/admin/workout-types`, `/admin/rewards/recalculate`
- [x] Controlled test fitness data, labelled end to end, disabled in production
- [x] Deletion of fitness data that cannot be used to earn twice
- [x] Tests: aggregation, duplicate steps and workouts, overlaps, missing and late data,
      rule changes, cap, non-eligible activities, replayed and concurrent syncs,
      Lisbon day boundaries on daylight-saving days

Result: 496 automated tests passing (197 new); the stack was started with Docker Compose
and the whole earning path exercised over HTTP (34 checks).

Still open from this phase, not blocking: the workout type labels your watch actually
produces ([DECISIONS.md](DECISIONS.md#open-questions-for-you)). Until confirmed, an
unexpected label earns nothing rather than too much.

## Phase 3 — AI gateway — next

Goal: both providers behind one interface, every request reserved, costed and recorded.

- [ ] **(you)** Create provider accounts, prepaid balance, auto-recharge off, API keys
- [ ] Verify current models and prices from the providers' official pages; record sources
- [ ] Confirm each model is available to your API account
- [ ] Provider adapter interface; OpenAI adapter; Anthropic adapter
- [ ] Usage normalisation across providers
- [ ] Pricing engine: versioned prices, cache categories, long-prompt tier, FX
- [ ] Cost estimation (provider token counting where offered)
- [ ] Gateway: estimate → reserve → call → settle / release (on the Phase 1 wallet)
- [ ] Streaming over Server-Sent Events; cancellation on client disconnect
- [ ] Timeouts, provider errors, refusals, partial streams
- [ ] Chat sessions and messages; history window
- [ ] Endpoints: `/ai/*`, `/analytics/*`, `/admin/models`, `/admin/model-pricing`
- [ ] Spending alerts
- [ ] Tests with mocked providers: both integrations, streaming, usage, pricing,
      errors, insufficient credits, timeouts
- [ ] **(you approve)** One real request per provider on the cheapest model

Accepted when: a message can be sent to one inexpensive model from each provider; the
charge equals the cost computed from provider-reported usage; every failure path leaves
the wallet consistent; no automated test makes a real API call.

## Phase 4 — Frontend

Goal: the product, in a browser, on real data.

- [ ] Next.js project, Tailwind, shadcn/ui, API types generated from the OpenAPI schema
- [ ] Authenticated shell, navigation, responsive layout, light and dark themes
- [ ] Login and first-run registration
- [ ] Dashboard
- [ ] AI Chat: conversations, provider and model selection, streaming, Markdown, code
      highlighting, pre-submission estimate, per-message tokens, cost and credits
- [ ] Activity: daily / weekly / monthly charts, workouts, earned credits, history
- [ ] Wallet: balances, ledger, credit-to-EUR
- [ ] Usage Analytics: tokens, cost by model and provider, averages, date filters
- [ ] Settings: profile, timezone, spending limits, devices, pairing, data deletion
- [ ] Admin: reward rules, models, prices, exchange rates
- [ ] Empty, loading and error states everywhere
- [ ] Component tests; Playwright flows
- [ ] Frontend Dockerfile; added to compose

Accepted when: every figure on every screen comes from the backend; the main flows pass
in Playwright; the layout works on phone, tablet and desktop.

## Before Phase 5 — feasibility investigation

You have no Mac and have asked for no Apple purchases. Before any Phase 5 work, I will
investigate and report on:

- [ ] Whether the HealthKit entitlement and HealthKit background delivery can be signed
      with a free Apple account, and what the 7-day expiry means in daily use
- [ ] What can be built and tested without owning a Mac: hosted macOS build services and
      CI runners, what each costs, and whether any can install onto your iPhone
- [ ] What cannot be tested without the physical phone in any case
- [ ] Alternatives to a native companion that still use official interfaces only
      (for example an Apple Shortcuts automation posting Health data, or Strava as a
      second source) and what each would lose
- [ ] A recommendation with costs, for your decision

No hardware is bought, no programme is joined and no paid service is used as part of
this investigation.

## Phase 5 — iOS and HealthKit

Goal: activity reaches the backend without you thinking about it. Scope and approach
depend on the investigation above.

- [ ] **(you decide)** Build route chosen from the investigation
- [ ] Xcode project, HealthKit capability, background delivery entitlement
- [ ] Permission flow; clear explanation of what is read and why
- [ ] Daily statistics queries; anchored workout query; hand-entered data excluded
- [ ] Pairing with the backend (Phase 1 pairing endpoints); Keychain storage
- [ ] Sync engine: rolling window, batch ids, retry, anchors
- [ ] Background delivery and background refresh
- [ ] Status screen: last sync, today's totals, credits earned, errors
- [ ] Unit tests with a fake HealthKit store
- [ ] **(you)** On-device validation with the Watch S3 over several days

Accepted when: a normal day's activity appears in the web app without opening the
companion; nothing is rewarded twice over a week of real use.

## Phase 6 — Production deployment

Goal: your own instance, on the internet, safely.

- [ ] **(you approve)** DigitalOcean droplet and domain (recurring cost)
- [ ] Production compose: Caddy, frontend, backend, database
- [ ] HTTPS with automatic certificates; security headers
- [ ] Separate migration and runtime database roles
- [ ] Migrations as a release step
- [ ] Encrypted off-box backups; restore rehearsed
- [ ] Structured logs, log rotation, uptime and error monitoring
- [ ] Secret management on the server
- [ ] Firewall, SSH hardening, automatic security updates
- [ ] Deployment and rollback runbook
- [ ] Security checklist from [SECURITY.md](SECURITY.md#before-going-public-phase-6-checklist)

Accepted when: the platform runs at your domain over HTTPS, a backup has been restored
successfully, and the security checklist is complete.

## What you will need to provide, and when

| Needed | Phase | Cost |
|---|---|---|
| Workout type names from Apple Health (optional refinement) | any time | — |
| OpenAI API account, prepaid credit, API key | 3 | Prepaid amount of your choice |
| Anthropic API account, prepaid credit, API key | 3 | Prepaid amount of your choice |
| A decision on the iOS build route | before 5 | Depends on the route; presented first |
| DigitalOcean account and droplet | 6 | Monthly, small droplet tier (confirmed with you before creation) |
| Domain name | 6 | Yearly |

Nothing in this table is purchased, created or deployed without asking you first.
