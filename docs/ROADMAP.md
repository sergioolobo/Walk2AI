# Walk2AI — Implementation Roadmap

Each phase ends with working software, passing tests and updated documentation. A phase
does not start until the previous one is accepted. Items needing something from you are
marked **(you)**.

## Phase 0 — Architecture and repository preparation

- [x] Inspect the development environment and toolchain
- [x] Inspect existing project files
- [x] Evaluate the proposed technology stack
- [x] Research the Xiaomi Watch S3 → Mi Fitness → Apple Health pipeline
- [x] Design the architecture ([ARCHITECTURE.md](ARCHITECTURE.md))
- [x] Design the database and validate it on PostgreSQL 16 ([DATABASE.md](DATABASE.md))
- [x] Define the API contract ([API.md](API.md))
- [x] Define security and privacy controls ([SECURITY.md](SECURITY.md))
- [x] Define development conventions ([DEVELOPMENT.md](DEVELOPMENT.md))
- [x] Create the repository skeleton and environment configuration
- [x] Record decisions and deviations ([DECISIONS.md](DECISIONS.md))
- [ ] **(you)** Approve the architecture and the decisions marked *needs approval*
- [ ] **(you)** Run the device check ([FITNESS_INTEGRATION.md](FITNESS_INTEGRATION.md#3-device-check-please-run-this-first))

## Phase 1 — Backend foundation

Goal: a running, tested API with authentication and the full schema.

- [ ] Backend project: `pyproject.toml`, uv lockfile, Ruff, mypy, pytest
- [ ] Typed settings with startup validation
- [ ] Async database session management
- [ ] SQLAlchemy models for all tables
- [ ] Alembic: initial migration including constraints, triggers, extensions
- [ ] Seed command (reward rules, workout type rules, EUR→WC rate)
- [ ] Authentication: register, login, refresh, logout, me; Argon2id; cookie sessions
- [ ] Roles and the admin dependency; registration modes
- [ ] Device pairing and scoped device tokens
- [ ] Error handling (problem+json), request ids, structured logging
- [ ] Rate limiting; audit log writer
- [ ] Health and readiness endpoints
- [ ] Backend Dockerfile; `docker compose up` runs database + API
- [ ] Test foundation: real-PostgreSQL fixtures, API client, factories
- [ ] CI workflow: lint, types, tests

Accepted when: a fresh clone reaches a healthy API with `docker compose up`; a user can
register, log in and call `/auth/me`; migrations apply and roll back; tests pass in CI.

## Phase 2 — Wallet and reward engine

Goal: activity turns into credits, correctly and exactly once.

- [ ] `WalletService`: reward, reserve, settle, release, adjust — row-locked, ledgered
- [ ] Reservation expiry task
- [ ] Reconciliation check (balances = ledger sums)
- [ ] Fitness ingestion: validation, upserts, batch idempotency
- [ ] Workout de-duplication, overlap handling, midnight splitting, exclusion
- [ ] `RewardEngine`: rule lookup by date, calculation, per-day ledger, delta payout
- [ ] Spending limits and the checks applied at reservation time
- [ ] Controlled test fitness data, labelled end to end, disabled in production
- [ ] Endpoints: `/fitness/*`, `/wallet/*`, `/admin/reward-rules`, adjustments
- [ ] Tests: aggregation, duplicates, overlaps, missing and late data, rule changes,
      cap, idempotence, concurrency, insufficient balance, ledger consistency

Accepted when: the specification's examples produce the specified numbers; replaying any
sync changes nothing; concurrent spending can never overdraw; all required fitness,
reward and wallet tests pass.

Depends on: the device check result for the final workout-eligibility rules.

## Phase 3 — AI gateway

Goal: both providers behind one interface, every request reserved, costed and recorded.

- [ ] **(you)** Create provider accounts, prepaid balance, auto-recharge off, API keys
- [ ] Verify current models and prices from the providers' official pages; record sources
- [ ] Confirm each model is available to your API account
- [ ] Provider adapter interface; OpenAI adapter; Anthropic adapter
- [ ] Usage normalisation across providers
- [ ] Pricing engine: versioned prices, cache categories, long-prompt tier, FX
- [ ] Cost estimation (provider token counting where offered)
- [ ] Gateway: estimate → reserve → call → settle / release
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

- [ ] Next.js project, Tailwind, shadcn/ui, generated API types
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

## Phase 5 — iOS and HealthKit

Goal: activity reaches the backend without you thinking about it.

- [ ] **(you)** Mac with Xcode; Apple Developer Program enrolment
- [ ] Xcode project, HealthKit capability, background delivery entitlement
- [ ] Permission flow; clear explanation of what is read and why
- [ ] Daily statistics queries; anchored workout query; hand-entered data excluded
- [ ] Pairing with the backend; Keychain storage
- [ ] Sync engine: rolling window, batch ids, retry, anchors
- [ ] Background delivery and background refresh
- [ ] Status screen: last sync, today's totals, credits earned, errors
- [ ] Handling of missing data, inferred permission loss, offline
- [ ] Unit tests with a fake HealthKit store
- [ ] **(you)** On-device validation with the Watch S3 over several days

Accepted when: a normal day's activity appears in the web app without opening the
companion; nothing is rewarded twice over a week of real use.

## Phase 6 — Production deployment

Goal: your own instance, on the internet, safely.

- [ ] **(you approve)** DigitalOcean droplet and domain (recurring cost)
- [ ] Production compose: reverse proxy, frontend, backend, database
- [ ] HTTPS with automatic certificates; security headers
- [ ] Separate migration and runtime database roles
- [ ] Migrations as a release step
- [ ] Encrypted off-box backups; restore rehearsed
- [ ] Structured logs, log rotation, uptime and error monitoring
- [ ] Secret management on the server
- [ ] Firewall, SSH hardening, automatic security updates
- [ ] Deployment and rollback runbook
- [ ] Security checklist from [SECURITY.md](SECURITY.md#before-going-public-phase-6-checklist)
- [ ] Point the iOS companion at the production backend

Accepted when: the platform runs at your domain over HTTPS, a backup has been restored
successfully, and the security checklist is complete.

## What you will need to provide, and when

| Needed | Phase | Cost |
|---|---|---|
| Decisions on the open questions in [DECISIONS.md](DECISIONS.md#open-questions-for-you) | 0 | — |
| Device check results | before 2 | — |
| OpenAI API account, prepaid credit, API key | 3 | Prepaid amount of your choice |
| Anthropic API account, prepaid credit, API key | 3 | Prepaid amount of your choice |
| Mac with Xcode | 5 | — |
| Apple Developer Program | 5 | US$99 per year |
| DigitalOcean account and droplet | 6 | Monthly, small droplet tier (confirmed with you before creation) |
| Domain name | 6 | Yearly |

Nothing in this table is purchased, created or deployed without asking you first.
