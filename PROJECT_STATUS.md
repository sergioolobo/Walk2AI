# Walk2AI — Project Status

_Last updated: 2026-11-14_

## Current phase

**Phase 2 — Fitness ingestion and reward engine: complete, awaiting your review.**
Next: Phase 3 — the AI gateway.

## Phases

| Phase | Status |
|---|---|
| 0 — Architecture and repository preparation | Done; approved 2026-11-13 |
| 1 — Backend foundation and wallet | Done |
| 2 — Fitness ingestion, reward engine, spending limits | **Done; awaiting review** |
| 3 — AI gateway | Not started |
| 4 — Frontend | Not started |
| Before 5 — iOS feasibility investigation (no Mac, no purchases) | Not started |
| 5 — iOS and HealthKit | Not started |
| 6 — Production deployment | Not started |

## Implemented functionality

Everything in this table runs, and is covered by automated tests.

| Area | What works |
|---|---|
| Stack | `docker compose up --build` starts PostgreSQL, applies migrations, seeds configuration and serves the API on `localhost:8000` |
| Accounts and sessions | Registration (first account becomes admin, then closed), Argon2id passwords, HttpOnly cookies, rotating refresh tokens, theft detection, server-side logout |
| Device pairing | One-time pairing codes; device tokens limited to `fitness:sync`; revocation |
| Wallet | Reward, reversal, reserve, settle, release, expire, adjust — one service, row-locked, on an append-only ledger with idempotency keys |
| **Fitness sync** | A paired device uploads daily totals and workouts; idempotent on a batch id; each record validated for plausibility; days and workouts upserted; deletions handled |
| **Reward engine** | `min(1500, 0.1 × steps + 5 × workout minutes)` per local day, under that day's rules; pays only the difference from what the day was already paid |
| **No double rewards** | Replayed batches, re-sent days, duplicated or overlapping workouts and simultaneous syncs pay nothing extra; walking, running and hiking earn through steps only |
| **Late and revised data** | Paid under its own day's cap within a 7-day window; a downward revision does not claw back; older data is stored, not paid |
| **Exclusion and deletion** | Excluding a rewarded workout reverses its credits (never below zero); deleting fitness data keeps credits and cannot be used to earn twice |
| **Reward rules** | Versioned by activity date; a change can only start on a day that has not begun; rules in force cannot be rewritten; workout types classifiable |
| **Spending limits** | Optional per-request, daily, monthly and minimum-balance limits, enforced when credits are reserved, in the user's timezone |
| **Test activity** | Administrator-only route, off by default, forbidden in production, labelled in every row and ledger entry |
| Views | Wallet balance, history, summary with limit usage; fitness daily, history and workouts with the reason each does or does not earn |
| Administration | Audited wallet adjustments, reconciliation, audit log, reward rules, workout types, recalculation |
| Timezone | `Europe/Lisbon` by default; UTC storage; day and month boundaries with daylight saving |
| Platform | Typed settings with production safety checks, problem+json errors, request ids, JSON logs, rate limiting, CSRF origin check, probes |
| Quality | Ruff, mypy (strict), CI on GitHub Actions |

Not implemented yet: AI gateway, model catalogue and pricing, chat, analytics, frontend,
iOS companion. Real activity cannot arrive until the companion exists; the earning path
can be tried today with labelled test activity (`docs/DEVELOPMENT.md`).

## Test results (2026-11-14)

| Check | Result |
|---|---|
| Automated tests | **496 passed**, 0 failed, 0 skipped (~75 s); 197 added in Phase 2 |
| Concurrency tests repeated 8× | Stable (17 tests) |
| Ruff lint and format | Clean |
| mypy (strict), 91 source files | Clean |
| CI on GitHub (pull request #1) | Lint, types, tests and image build run on every push. One run failed during Phase 2 on a single test: a race inside a Phase 1 *test* (it shared a reservation id with other workers before the transaction creating it had committed). The product code was not at fault; the test was fixed and re-run 25 times. |
| Docker image | Rebuilt with Phase 2; runs as a non-root user |
| End-to-end over HTTP against the running Compose stack | 34 of 34 Phase 2 checks passed |
| Live database and logs of that run | Ledger update refused; no errors logged; no password, token or secret in logs |

Phase 2 tests by area: sync end to end 43 · reward formula (examples and generated-input
properties) 43 · validation and source policy 39 · spending limits 23 · reward rules 16 ·
exclusion and deletion 11 · views and test activity 11 · sync concurrency 7 ·
architecture rules +4.

The tests the specification asks for are all present: daily aggregation, duplicate step
records, duplicate workouts, overlapping workouts, missing data, delayed synchronisation;
reward calculation, daily limits, rule changes, duplicate reward prevention, non-eligible
activities.

## Known issues and limitations

| # | Item | Impact | Status |
|---|---|---|---|
| L1 | `docker compose build` cannot run to completion in the development sandbox (TLS-inspecting proxy). The image is built there with `docker build` plus proxy flags, from the same Dockerfile. | CI builds the image on an ordinary machine without those flags and passes; `docker compose up --build` on your own machine is still unconfirmed. | Please tell me if it fails |
| L3 | Uvicorn's own start-up lines are plain text; application logs are JSON. | Cosmetic. | Tidy in Phase 6 |
| L4 | The rate limiter is per process. | Correct for one backend process, which is the design. | By design (D-03, D-28) |
| L5 | Running the backend natively on Windows (outside Docker/WSL) is not supported. | None if you use Docker Desktop. | Documented |
| L6 | No email flows (verification, password reset). | A forgotten password needs a database-level reset. | Revisit in Phase 6 |
| L7 | Workout type classification is seeded with defaults I chose; your watch's actual labels are unconfirmed. | A mislabelled type earns nothing until classified (one admin call); it cannot over-reward. | Open — needs the labels from your Health app |
| L8 | Real activity cannot reach the backend until the iOS companion exists. | The earning path is fully built and tested, but only test activity can exercise it today. | Phase 5 |
| L9 | No web interface yet: the API is used through the interactive documentation page. | Usable for trying things, not a product. | Phase 4 |

L2 from the previous report (CI unverified) is closed: the workflow ran and passed.

## Risks

| # | Risk | Status |
|---|---|---|
| R1 | Workouts might not reach Apple Health | Closed — you confirmed they do |
| R2 | No Mac: the iOS companion cannot be built or tested by either of us today | Open — investigated before Phase 5, at no cost |
| R3 | HealthKit may require the paid Apple Developer Program | Open — part of the same investigation; nothing purchased without approval |
| R4 | iOS background sync is not real-time | Accepted — designed for |
| R5 | Provider prices change | Mitigated by design — versioned, sourced, dated prices; built in Phase 3 |
| R6 | A full day's allowance is €0.15 | By design — surfaced through estimates in the chat |
| R8 | Workout type labels may differ from the seeded defaults | Low — can only under-reward (L7) |

## Required external configuration

To run the stack: Docker, and a `.env` with `POSTGRES_PASSWORD` and `JWT_SECRET`
(see `docs/DEVELOPMENT.md`). Nothing else.

Needed later, none of it yet:

| Item | Phase | Notes |
|---|---|---|
| OpenAI API key (dedicated project, prepaid, auto-recharge off) | end of 3 | Backend environment only; only for one approved real request |
| Anthropic API key (dedicated workspace, prepaid, auto-reload off) | end of 3 | Backend environment only; only for one approved real request |
| A decision on the iOS build route | before 5 | After the feasibility report |
| DigitalOcean account, droplet, domain | 6 | Paid; only with your approval |

No external cost has been incurred, no paid service subscribed to, and nothing deployed.
CI uses GitHub Actions' included minutes and runs only on pull requests and pushes to
`main`.

## Next actions

1. **You:** review Phase 2. Pull request #1 holds Phases 0–2.
2. **You (optional):** send the workout type labels shown in Apple Health.
3. **Me, on your go-ahead:** Phase 3 — verify current models and prices from the
   providers' official pages, build the provider adapters, pricing engine and gateway
   (estimate → reserve → call → settle) on the existing wallet and limits, all tested
   against mocked providers. No real API call without your keys and your approval.
