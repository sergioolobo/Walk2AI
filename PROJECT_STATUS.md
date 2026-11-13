# Walk2AI — Project Status

_Last updated: 2026-11-13_

## Current phase

**Phase 1 — Backend foundation: complete, awaiting your review.**
Next: Phase 2 — fitness ingestion and the reward engine.

## Phases

| Phase | Status |
|---|---|
| 0 — Architecture and repository preparation | Done; approved 2026-11-13 |
| 1 — Backend foundation (+ wallet, brought forward) | **Done; awaiting review** |
| 2 — Fitness ingestion and reward engine | Not started |
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
| Database | 21 tables; one migration, tested up → down → up; constraints and triggers that hold even if the application is wrong |
| Accounts | Registration (first account becomes admin, then closed), Argon2id passwords, profile, password change |
| Sessions | HttpOnly cookies, 15-minute access token, rotating 30-day refresh token, theft detection, server-side logout |
| Device pairing | One-time pairing codes; device tokens limited to `fitness:sync`; revocation |
| Wallet | Reward, reversal, reserve, settle, release, expire, adjust — one service, row-locked, on an append-only ledger with idempotency keys |
| Wallet API | Balance with EUR equivalent; ledger history; today / this month in the user's timezone; reservations |
| Administration | Audited wallet adjustments; ledger reconciliation; audit log |
| Timezone | `Europe/Lisbon` by default; UTC storage; day and month boundaries with daylight saving |
| Platform | Typed settings with production safety checks, problem+json errors, request ids, JSON logs, rate limiting, CSRF origin check, health and readiness probes |
| Operations | CLI: `seed`, `reconcile`, `expire-reservations`; background sweep of expired holds |
| Quality | Ruff, mypy (strict), CI workflow |

Not implemented yet (tables exist, code does not): fitness sync, reward engine, spending
limits, AI gateway, model catalogue and pricing, chat, analytics, frontend, iOS companion.

## Test results (2026-11-13)

| Check | Result |
|---|---|
| Automated tests | **299 passed**, 0 failed, 0 skipped (~35 s) |
| Race-sensitive tests repeated 5× | Stable |
| Ruff lint and format | Clean |
| mypy (strict), 70 source files | Clean |
| Docker image | Builds; 277 MB; runs as a non-root user |
| End-to-end over HTTP against the running Compose stack | 27 of 27 checks passed |
| Live database | `UPDATE` / `DELETE` on the ledger and a negative balance each refused |
| Logs of that run | Structured JSON; password, tokens, pairing code and secrets absent |
| Data persistence | Account and balance survive `docker compose down` / `up` |

Tests by area: wallet service 44 · wallet concurrency 10 · database integrity 36 ·
migrations and seeds 6 · authentication 35 · devices 27 · wallet API 32 · administration
16 · HTTP behaviour 14 · operations 5 · unit (timezones, cryptography, configuration,
rate limiter, architecture rules) 74.

## Known issues and limitations

| # | Item | Impact | Status |
|---|---|---|---|
| L1 | `docker compose build` could not be run to completion **in the development sandbox**: its internet access goes through a TLS-inspecting proxy that Compose's builder cannot be given. The image was built with `docker build` plus proxy flags from the same Dockerfile, and Compose then ran it. | None expected on your machine; first real confirmation is your own `docker compose up --build`. | Please tell me if it fails |
| L2 | The CI workflow has not executed yet (it runs on GitHub, on pull requests and pushes to `main`). | Unverified until its first run. | Will confirm on the first PR |
| L3 | Uvicorn's own start-up lines are plain text; application logs are JSON. | Cosmetic. | Tidy in Phase 6 |
| L4 | The rate limiter is per process. | Correct for one backend process, which is the design; must change if the backend is ever scaled out. | By design (D-03, D-28) |
| L5 | Running the backend natively on Windows (outside Docker/WSL) is not supported. | None if you use Docker Desktop. | Documented |
| L6 | No email flows (verification, password reset). | A forgotten password needs a database-level reset. Acceptable for a single-user instance. | Revisit in Phase 6 |

## Risks

| # | Risk | Status |
|---|---|---|
| R1 | Workouts might not reach Apple Health | **Closed** — you confirmed they do |
| R2 | No Mac: the iOS companion cannot be built or tested by either of us today | Open — investigated before Phase 5, at no cost |
| R3 | HealthKit may require the paid Apple Developer Program | Open — part of the same investigation; nothing purchased without approval |
| R4 | iOS background sync is not real-time | Accepted — designed for |
| R5 | Provider prices change | Mitigated — versioned, sourced, dated prices; reconciliation in Phase 3 |
| R6 | A full day's allowance is €0.15 | By design — surfaced through estimates in the chat |
| R8 | Workout type labels from the watch may differ from the seeded defaults | Low — unknown types earn nothing until classified, so the error can only under-reward |

## Required external configuration

To run the stack: Docker, and a `.env` with `POSTGRES_PASSWORD` and `JWT_SECRET`
(see `docs/DEVELOPMENT.md`). Nothing else.

Needed later, none of it yet:

| Item | Phase | Notes |
|---|---|---|
| OpenAI API key (dedicated project, prepaid, auto-recharge off) | 3 | Backend environment only |
| Anthropic API key (dedicated workspace, prepaid, auto-reload off) | 3 | Backend environment only |
| A decision on the iOS build route | before 5 | After the feasibility report |
| DigitalOcean account, droplet, domain | 6 | Paid; only with your approval |

No external cost has been incurred, no paid service subscribed to, and nothing deployed.
The CI workflow uses GitHub Actions' included minutes and runs only on pull requests and
pushes to `main`; delete `.github/workflows/ci.yml` if you prefer not to use it.

## Next actions

1. **You:** review Phase 1 — try `docker compose up --build` on your machine.
2. **You (optional):** send the workout type labels shown in Apple Health.
3. **Me, on your go-ahead:** Phase 2 — fitness sync, validation, de-duplication, the
   reward engine on top of the Phase 1 wallet, spending limits.
