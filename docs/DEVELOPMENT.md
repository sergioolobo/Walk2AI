# Walk2AI — Development Guide

> Status: **Phase 0.** The repository contains documentation, configuration and an empty
> skeleton. Commands for running the backend, frontend and tests are added to this file
> in the phase that makes them real; nothing below describes software that does not exist.

## Toolchain

Inspected on 2026-11-13 in the cloud development environment used for this project
(Linux x86-64, 4 vCPU, 15 GB RAM).

| Tool | Found | Used for | Notes |
|---|---|---|---|
| Python | 3.13.18 | Backend | Target runtime: Python 3.13 |
| uv | 0.11.32 | Python dependencies, virtualenv, lockfile | Chosen over pip/Poetry: fast, one lockfile |
| Node.js | 22.23.4 | Frontend | LTS line |
| pnpm | 10.28.0 | Frontend dependencies | |
| PostgreSQL | 16.15 server + client | Database | Local cluster available; also via Docker |
| Docker Engine | 29.9.2 | Containers | Daemon is not started automatically here; it starts and runs correctly when launched |
| Docker Compose | 5.6.1 | Local stack | `docker compose up db` verified |
| Git | 2.43.0 | Version control | |
| GNU Make | 4.3 | Task runner | |
| Playwright + Chromium | 1.63.0 | Browser tests (Phase 4) | Pre-installed |
| Swift / Xcode | **not available** | iOS companion | Linux cannot build iOS apps; see below |
| Nginx / Caddy | not installed | Reverse proxy | Only needed in production; runs as a container |
| Redis | 7.0.15 present | — | Not used by the design |

Package registries (PyPI, npm, Docker Hub) are reachable.

### Planned dependency versions

Latest stable releases on 2026-11-13. Exact versions are pinned by lockfiles when each
component is created.

| Backend | Version | | Frontend | Version |
|---|---|---|---|---|
| FastAPI | 0.162 | | Next.js | 16.4 |
| Pydantic | 2.14 | | React | 19.3 |
| SQLAlchemy | 2.1 | | TypeScript | 7.0 |
| Alembic | 1.20 | | Tailwind CSS | 4.3 |
| psycopg | 3.3 | | shadcn (CLI) | 4.23 |
| Uvicorn | 0.54 | | Recharts | 3.10 |
| `openai` SDK | 3.50 | | TanStack Query | 5.107 |
| `anthropic` SDK | 2.8 | | react-markdown | 10.1 |
| argon2-cffi | 25.1 | | Zod | 4.6 |
| pytest | 9.2 | | | |
| Ruff / mypy | 0.17 / 2.4 | | | |

### What cannot be done in this environment

| Limitation | Consequence |
|---|---|
| No macOS, Xcode or Swift toolchain | The iOS companion can be written here but must be compiled, signed and run on a Mac. |
| No iPhone, watch or HealthKit | The Xiaomi → Apple Health link can only be verified on your devices. |
| No provider API keys configured | Provider integrations are developed against mocks; a real smoke test needs your keys and your explicit approval to spend. |
| Ephemeral container | Everything worth keeping is committed and pushed. |

## Repository layout

```
walk2ai/
├── backend/            FastAPI application            (Phase 1–3)
│   ├── app/{api,core,models,schemas,services}
│   ├── migrations/     Alembic
│   └── tests/
├── frontend/           Next.js web application        (Phase 4)
│   └── {app,components,hooks,lib,types}
├── mobile/Walk2AI/     SwiftUI + HealthKit companion  (Phase 5)
├── infrastructure/     Dockerfiles, deployment        (Phase 1, 6)
├── docs/               Architecture and reference
├── docker-compose.yml  Local development stack
├── .env.example        Configuration template
└── PROJECT_STATUS.md   Living status record
```

## Running what exists today

Only the database:

```bash
cp .env.example .env          # then set POSTGRES_PASSWORD
docker compose up -d db
docker compose exec -T db psql -U walk2ai -d walk2ai < docs/database/schema.sql   # optional: load the reference schema
```

## Conventions

### General

- Small, reviewable commits; imperative subject line; the body says why.
- Work happens on a feature branch; `main` always works.
- A phase is finished only when its tests pass, its documentation is updated and
  `PROJECT_STATUS.md` reflects it.
- No secrets in the repository, in logs or in test fixtures.
- No mock data presented as real. Test data is labelled as test data end to end.

### Backend (Python)

- Formatting and linting: Ruff. Types: mypy in strict mode for `services/` and `core/`.
- Layering: `api` → `services` → `models`. Routers contain no business logic and no
  queries. Services never import from `api`.
- Money: `Decimal` and `int` only. A lint rule forbids `float` in `services/wallet`,
  `services/rewards` and `services/ai/pricing`.
- Database access is async SQLAlchemy 2 with explicit transactions; a service method
  that changes money states its transaction boundary in its docstring.
- No provider call inside an open database transaction.
- Time comes from an injected clock, never `datetime.now()` in business logic.
- Errors are typed exceptions mapped to problem+json in one place.

### Frontend (TypeScript)

- `strict` TypeScript; ESLint and Prettier.
- API types are generated from the backend's OpenAPI schema, not written by hand.
- Server state through TanStack Query. No balance arithmetic in the browser.
- Credits are rendered from integers; money from decimal strings with a formatter.

### iOS (Swift)

- SwiftUI, Swift concurrency, no third-party dependencies unless unavoidable.
- HealthKit access isolated behind one protocol so sync logic is unit-testable without it.
- The device token lives in the Keychain only.

## Testing strategy

| Layer | Approach |
|---|---|
| Reward and wallet logic | pytest against a **real PostgreSQL** (not SQLite): locking, constraints and exclusion ranges are part of what is being tested. |
| Concurrency | Tests that issue simultaneous reservations and assert that exactly the affordable ones succeed. |
| Ledger consistency | A fixture asserts `balances = Σ ledger` after every test that touches a wallet. |
| Reward engine | Table-driven cases plus property-based tests: idempotence, order independence, cap never exceeded, never double-paid. |
| AI gateway | Provider SDKs are mocked at the HTTP layer with recorded response shapes, including streams, errors and timeouts. **No real API calls in automated tests.** |
| API | httpx `AsyncClient` against the app with a test database; auth, authorisation and validation on every route. |
| Frontend | Component tests for formatting and state; Playwright for the main flows against a seeded backend. |
| iOS | Unit tests for payload building and sync state with a fake HealthKit store; manual checklist on the physical device. |

A single real request per provider is made once, manually, at the end of Phase 3 to
confirm the integration end to end — only with your approval, on the cheapest model,
with a tiny output limit.

## Environment configuration

`.env.example` documents every variable. Rules:

- Environment variables hold infrastructure settings and secrets.
- Business parameters (reward rates, cap, prices, exchange rates, limits) are database
  rows managed through the admin API.
- The backend validates its configuration at startup and refuses to run in production
  with missing or placeholder secrets.

## Definition of done, per phase

1. The planned functionality works end to end, not only in tests.
2. Automated tests cover it, including failure paths, and pass.
3. Lint and type checks pass.
4. Documentation matches the code; `PROJECT_STATUS.md` is updated.
5. Anything not done, and anything that needs configuration from you, is written down.
