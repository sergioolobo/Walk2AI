# Walk2AI — Project Status

_Last updated: 2026-11-13_

## Current phase

**Phase 0 — Architecture and repository preparation: complete, awaiting approval.**

No application code has been written. Implementation starts with Phase 1 once the
architecture is approved.

## Completed phases

| Phase | Status | Date |
|---|---|---|
| 0 — Architecture and repository preparation | Delivered, awaiting approval | 2026-11-13 |
| 1 — Backend foundation | Not started | |
| 2 — Wallet and reward engine | Not started | |
| 3 — AI gateway | Not started | |
| 4 — Frontend | Not started | |
| 5 — iOS and HealthKit | Not started | |
| 6 — Production deployment | Not started | |

## Implemented functionality

Application functionality: **none yet.**

What exists in the repository:

| Item | State |
|---|---|
| Architecture, database, API, security, development, fitness and roadmap documentation | Written (`docs/`) |
| Reference database schema (`docs/database/schema.sql`) | Written and validated on PostgreSQL 16 |
| Monorepo skeleton (`backend/`, `frontend/`, `mobile/`, `infrastructure/`) | Empty directories |
| `docker-compose.yml` | Database service only; verified to start and accept the schema |
| `.env.example`, `.gitignore`, `.editorconfig` | Written |

## Verified during Phase 0

- Toolchain present: Python 3.13, uv, Node.js 22, pnpm, PostgreSQL 16, Docker 29 with
  Compose, Git, Make, Playwright. Not present: Swift / Xcode (expected on Linux).
- The schema applies cleanly (20 tables) on a local PostgreSQL 16 and on
  `postgres:16-alpine` through `docker compose`.
- Database-level guarantees behave as designed: overlapping rule versions rejected,
  overdrafts rejected, ledger immutable, idempotency keys unique, and of three concurrent
  reservations for the same credits exactly one succeeds.
- The specification's reward examples and wallet example reproduce exactly.

## Outstanding tasks

Before Phase 1 can begin:

- [ ] Approval of the architecture
- [ ] Decisions on the items marked *needs approval* in `docs/DECISIONS.md`
      (Supabase, reverse proxy)

Before Phase 2 is finalised:

- [ ] Device check: does the Xiaomi Watch S3 deliver workouts to Apple Health?
      (`docs/FITNESS_INTEGRATION.md`, section 3)

The full checklist for every phase is in `docs/ROADMAP.md`.

## Known issues and risks

| # | Risk | Impact | Status |
|---|---|---|---|
| R1 | Mi Fitness may not write workouts to Apple Health for the Watch S3 | Strength and cycling rewards would need a fallback (Strava relay, manual log, or steps only) | **Open — needs the device check** |
| R2 | The iOS companion cannot be built or tested in this environment | Swift code is written here uncompiled; building needs a Mac | Open — need to know whether a Mac is available |
| R3 | HealthKit requires the paid Apple Developer Program | US$99 per year before Phase 5 | Open — cost, will ask before it is needed |
| R4 | iOS background sync is not real-time | Credits may lag activity by an hour or more | Accepted — designed for; documented |
| R5 | Provider prices change | Computed cost could drift from the invoice | Mitigated — versioned, sourced, dated prices; reconciliation in Phase 3 |
| R6 | A full day's allowance is €0.15 | Large models allow only a handful of requests per day | By design — surfaced through estimates in the chat |
| R7 | Xiaomi support pages were unreachable from this environment (network policy) | Fitness research relies on search summaries for those pages | Noted in the sources |

## Required external configuration

Nothing is required yet. Later phases need:

| Item | Phase | Notes |
|---|---|---|
| OpenAI API key (dedicated project, prepaid, auto-recharge off) | 3 | Backend environment only |
| Anthropic API key (dedicated workspace, prepaid, auto-reload off) | 3 | Backend environment only |
| Mac with Xcode | 5 | |
| Apple Developer Program membership | 5 | Paid |
| DigitalOcean account, droplet | 6 | Paid |
| Domain name and DNS | 6 | Paid |

## Next actions

1. **You:** review `docs/ARCHITECTURE.md` and `docs/DECISIONS.md`; approve or adjust.
2. **You:** run the ten-minute device check and report what appears in Apple Health.
3. **Me, on approval:** Phase 1 — backend project, models, migrations, authentication,
   Docker, test foundation.
