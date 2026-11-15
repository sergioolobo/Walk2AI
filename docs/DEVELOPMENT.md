# Walk2AI — Development Guide

> Status: **Phases 1–3 complete.** The backend (including the AI gateway and the
> analytics), database and Docker Compose stack described here exist and are tested. The
> frontend (Phase 4) and iOS companion (Phase 5) do not exist yet; nothing below
> describes software that is not in the repository.

## Run it

You need Docker (Docker Desktop on Windows or macOS). Nothing else.

**1. Create `.env`** from the template and set the two required secrets.

Windows (PowerShell):

```powershell
Copy-Item .env.example .env
docker run --rm python:3.13-slim python -c "import secrets; print(secrets.token_hex(32))"
```

Linux / macOS:

```bash
cp .env.example .env
openssl rand -hex 32
```

Run the generator twice and paste one value into `POSTGRES_PASSWORD=` and another into
`JWT_SECRET=` in `.env`. The stack refuses to start without them.

**2. Start the stack.**

```bash
docker compose up --build
```

This starts PostgreSQL, applies the migrations, inserts the initial configuration
(reward rules, workout types, the EUR→WC rate) and starts the API. Both setup steps are
idempotent and run on every start.

**3. Use it.**

| What | Where |
|---|---|
| API | <http://localhost:8000/api/v1> |
| Interactive API documentation | <http://localhost:8000/api/v1/docs> |
| Health | <http://localhost:8000/api/v1/health/ready> |

The first account you register becomes the administrator; registration then closes.
Try it from the interactive documentation: `POST /auth/register`, then `POST /auth/login`
(the browser keeps the session cookie), then `GET /wallet`.

**Stop it** with `docker compose down`. Your data stays in a Docker volume;
`docker compose down --volumes` deletes it.

### Useful commands

| Purpose | Command |
|---|---|
| Follow backend logs | `docker compose logs -f backend` |
| Apply migrations | `docker compose run --rm migrate alembic upgrade head` |
| Verify balances against the ledger | `docker compose run --rm migrate python -m app.cli reconcile` |
| Release expired holds now | `docker compose run --rm migrate python -m app.cli expire-reservations` |
| Open a database shell | `docker compose exec db psql -U walk2ai -d walk2ai` |

On Linux, macOS or WSL, `make help` lists shortcuts for the same commands.

### Building behind a TLS-inspecting proxy

Normal builds need nothing extra. If your network intercepts HTTPS, pass the proxy and
its CA certificate to the build; the certificate is mounted only for the build step and
is not stored in the image:

```bash
docker build --network host --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --secret id=extra_ca,src=/path/to/ca-bundle.crt \
  -f infrastructure/docker/backend.Dockerfile -t walk2ai-backend:dev backend
docker compose up
```

## Try the earning path without a phone

The iOS companion does not exist yet, so real activity cannot arrive. To see the reward
engine work, enable clearly-labelled **test** activity on your local stack: set
`ALLOW_TEST_FITNESS_DATA=true` in `.env` and restart (`docker compose up -d`). Then, in
the interactive documentation (signed in as the administrator), call
`POST /fitness/sync/test`:

```json
{
  "batch_id": "11111111-1111-4111-8111-111111111111",
  "days": [{ "date": "<yesterday, YYYY-MM-DD>", "steps": 8000 }],
  "workouts": [{
    "external_id": "test-strength-1",
    "type": "traditional_strength_training",
    "start": "<yesterday>T18:00:00+00:00",
    "end": "<yesterday>T18:30:00+00:00",
    "duration_seconds": 1800
  }]
}
```

The response shows 800 + 150 = 950 WC awarded; `GET /wallet` shows the balance and
`GET /wallet/history` the ledger entry, marked `[TEST DATA]`. Send the same request
again: nothing more is paid. Everything this route creates is labelled as test data, and
the application refuses to start in production with the switch on.

## Try the AI gateway without spending anything

With no API keys set, the models are listed in `GET /admin/models` with the reasons they
cannot be used. To make one usable you need a real key, and a real request costs money —
so ask before doing it. The steps, once you have approved a request:

1. Put `ANTHROPIC_API_KEY=…` (and/or `OPENAI_API_KEY=…`) in `.env`; restart.
2. `POST /admin/exchange-rates` `{"base_currency":"USD","quote_currency":"EUR","rate":"0.92","source":"…"}`
3. `POST /admin/models/{id}/check` — free: it reads the model's metadata.
4. `PUT /admin/models/{id}` `{"enabled": true}`
5. Give yourself credits (test activity above, or `POST /admin/wallet/adjustments`).
6. `POST /ai/estimate`, then `POST /ai/chat`.

On Windows, `infrastructure/scripts/first-ai-request.ps1` does steps 2–6 for you
(ECB rate of the day, check, enable, credits) and asks before sending one real message
to each enabled model:

```powershell
powershell -ExecutionPolicy Bypass -File infrastructure\scripts\first-ai-request.ps1
```

For development without a key, the backend can be pointed at any server that speaks the
provider's API (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`); the Phase 3 smoke test used a
local fake that streams a scripted answer.

## Toolchain

| Tool | Version | Used for |
|---|---|---|
| Python | 3.13 | Backend |
| uv | 0.11 | Dependencies, virtualenv, lockfile (`backend/uv.lock`) |
| PostgreSQL | 16 | Database |
| Docker Engine + Compose | 29 / 5 | Local stack |
| Node.js / pnpm | 22 / 10 | Frontend (Phase 4) |

Backend libraries, pinned exactly in `backend/uv.lock`: FastAPI 0.162, Pydantic 2.14,
SQLAlchemy 2.1, Alembic 1.20, psycopg 3.3, Uvicorn 0.54, argon2-cffi 25.1, PyJWT 2.15.
Provider SDKs: `anthropic` 2.8 and `openai` 3.51 (both official; both use `httpx2`).
Development: pytest 9, Ruff 0.17, mypy 2.4, httpx 0.28, Hypothesis 6.

## Repository layout

```
backend/
├── app/
│   ├── main.py            application factory
│   ├── cli.py             seed · reconcile · expire-reservations
│   ├── seeds.py           initial business configuration (data, not constants)
│   ├── core/              config, db, security, errors, logging, middleware,
│   │                      rate limiting, clock, timezone arithmetic
│   ├── models/            SQLAlchemy mapping of all 21 tables
│   ├── schemas/           request / response models
│   ├── api/               routers: health, auth, devices, wallet, fitness, admin
│   └── services/
│       ├── auth/          accounts, login sessions, device pairing
│       ├── wallet/        WalletService, spending limits, read queries, sweeper
│       ├── audit/         audit trail writer
│       ├── fitness/       normalised records, validation, source policy, ingestion
│       ├── rewards/       the formula (pure), rule lookup and admin, RewardEngine
│       ├── ai/            pricing, catalogue, gateway, conversations, providers/
│       └── analytics/     dashboard aggregations
├── migrations/            Alembic; versions/0001_initial_schema.py, 0002_…
└── tests/                 635 tests
frontend/                  (Phase 4)
mobile/                    (Phase 5)
infrastructure/
├── docker/backend.Dockerfile
├── scripts/schema_doc.py
└── deployment/            (Phase 6)
docs/                      architecture and reference
.github/workflows/ci.yml   lint, types, tests, image build
docker-compose.yml         local stack
```

## Working on the backend

Running the backend outside Docker is for development on Linux, macOS or WSL. On
Windows, use WSL or stay with Docker: the async PostgreSQL driver needs an event loop
that native Windows Python does not use by default.

```bash
cd backend
uv sync                      # create .venv from the lockfile
docker compose up -d db      # from the repository root: just the database
uv run alembic upgrade head
uv run python -m app.cli seed
uv run uvicorn app.main:create_app --factory --reload
```

### Tests

Tests need a PostgreSQL database that is theirs to wipe. They refuse to run against any
database whose name does not end in `_test`.

```bash
# once: create the test database in the compose database
docker compose exec db createdb -U walk2ai walk2ai_test

cd backend
export TEST_DATABASE_URL="postgresql+psycopg://walk2ai:<POSTGRES_PASSWORD>@localhost:5432/walk2ai_test"
uv run pytest                # everything
uv run pytest -m concurrency # only the concurrent-wallet tests
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The same four checks run in CI (`.github/workflows/ci.yml`) on every pull request and on
pushes to `main`.

### What the tests cover

| File | Tests | Covers |
|---|---|---|
| `test_wallet_service.py` | 44 | Every wallet operation and every refusal; the specification's worked example |
| `test_wallet_concurrency.py` | 10 | Simultaneous reservations, settlements, rewards, expiry; a fixed-seed random workload |
| `test_spending_limits.py` | 23 | Per-request, daily, monthly and minimum-balance limits; Lisbon day and month boundaries; concurrent requests |
| `test_fitness_sync_api.py` | 43 | Sync end to end: the reward table, idempotency, duplicates, overlaps, missing and late data, caps, validation, Lisbon and daylight-saving days |
| `test_fitness_concurrency.py` | 7 | Simultaneous syncs: same batch, different batches, growing days, sync racing spending, a fixed-seed storm |
| `test_fitness_exclusion_and_deletion.py` | 11 | Exclusion and reversal, re-inclusion, data deletion that cannot pay twice |
| `test_fitness_views_and_test_data.py` | 11 | Read views, sync status, labelled test activity |
| `test_reward_rules.py` | 16 | Rule administration; rate changes never alter days already begun; workout types |
| `test_schema_integrity.py` | 36 | Guarantees the database enforces on its own, exercised with raw SQL |
| `test_migrations.py` | 9 | Upgrade / downgrade / re-upgrade; models match schema; seed data and catalogue |
| `test_fitness_realistic_payloads.py` | 9 | A HealthKit-shaped week through the device route: doubles, rolling resends, duplicates from two apps |
| `test_ai_providers.py` | 30 | The real Anthropic and OpenAI SDKs against recorded-shape HTTP: streaming, usage, every error class |
| `test_ai_chat.py` | 25 | The gateway end to end: exact charges, refusals before sending, every failure outcome, disconnects, concurrency, history |
| `test_ai_gateway.py` | 10 | Time limits, crashed requests, both real SDKs behind the gateway, estimates, conversations |
| `test_ai_admin.py` | 14 | Models, prices and rates: enabling rules, immutable verified prices, audit |
| `test_analytics.py` | 12 | Day / week / month periods in Lisbon time across summer time; agreement with history and ledger |
| `unit/test_pricing.py` | 32 | Costs per category, long-prompt tier, worst case ≥ actual (generated inputs), rounding |
| `test_auth_api.py` | 35 | Registration, login, rotation, theft detection, logout, password change |
| `test_devices_api.py` | 27 | Pairing, token scope, revocation |
| `test_wallet_api.py` | 32 | Wallet views, pagination, Lisbon day and month boundaries |
| `test_admin_api.py` | 16 | Role enforcement, audited adjustments, reconciliation, audit log |
| `test_http_behaviour.py` | 14 | Probes, error format, headers, CSRF, docs, secret hygiene |
| `test_operations.py` | 5 | CLI commands, reservation sweeper, application lifespan |
| `unit/test_reward_calculator.py` | 43 | The reward formula: examples, edge cases and generated-input properties |
| `unit/test_fitness_model_and_validation.py` | 39 | Type normalisation, plausibility rules, source policy |
| `unit/` (others) | 82 | Timezone arithmetic, cryptography helpers, configuration, rate limiter, architecture rules |

Principles:

- **Real PostgreSQL, real migrations.** Locks, constraints and triggers are part of what
  is being tested, so there is no SQLite and no `create_all()`.
- **A controllable clock.** Token expiry, reservation expiry and day boundaries are
  tested by moving time, not by sleeping.
- **Ledger checked after every money test.** `assert_ledger_consistent` runs the same
  reconciliation the admin endpoint exposes.
- **Warnings are errors.** A deprecation in a dependency fails the suite.
- **Properties, not only examples.** The reward formula is also checked with generated
  inputs (Hypothesis): never above a cap, never negative, order-independent, and a
  duplicated workout never adds minutes.
- **No test calls an AI provider.** Providers are scripted fakes (`tests/ai_support.py`)
  or the real SDKs over an in-memory HTTP transport (`tests/provider_fixtures.py`). No
  test needs a key, reaches a provider or costs anything.

### Architecture rules enforced by tests

`tests/unit/test_architecture_rules.py` reads the source and fails if:

- any code under `services/wallet`, `services/rewards`, `services/fitness`,
  `services/ai` or `services/analytics` uses a float literal, the `float` type, `/` on
  anything but a `Decimal`, or (in rewards and fitness) `timedelta.total_seconds()`
  — the provider adapters excepted, which hold no money logic (also checked);
- a provider SDK is imported anywhere but its adapter;
- anything outside `WalletService` constructs a ledger row or assigns a balance;
- anything outside `RewardEngine` pays or reverses an activity reward;
- anything outside `core/config.py` reads the environment, or the HTTP layer references
  a secret;
- a service calls `.commit()`;
- business logic reads the wall clock instead of the injected `Clock`.

## Database changes

1. Write a new file in `backend/migrations/versions/` (`uv run alembic revision -m "..."`
   creates the skeleton). Write constraints, triggers and indexes as explicit SQL, with
   explicit names.
2. Update the models to match.
3. `uv run pytest tests/test_migrations.py` proves they agree and that the migration
   reverts cleanly.
4. `make schema-doc` regenerates `docs/database/schema.sql`.

## Conventions

- Small, reviewable commits; imperative subject; the body says why.
- Layering: `api` → `services` → `models`. Routers contain no business logic and no
  queries; they authorise, delegate, **commit**, and serialise. Services never commit.
- Money: `int` for credits, `Decimal` for currency. Never `float`.
- Timestamps are timezone-aware UTC. Local-day logic goes through `core/timeutil.py`.
- Errors are typed exceptions with a stable `code`, rendered as problem+json in one place.
- Request bodies reject unknown fields.
- Secrets are `SecretStr` in settings and never logged, returned or audited.
- No mock data presented as real. Test data is labelled as test data end to end.

## What cannot be done in the cloud development environment

| Limitation | Consequence |
|---|---|
| No macOS, Xcode or Swift toolchain | The iOS companion cannot be compiled here (and you have no Mac): see [DECISIONS.md](DECISIONS.md), D-07. |
| No provider API keys | The provider integrations are tested against mocks; one real request per provider needs your keys and your approval. |
| Internet access through a TLS-inspecting proxy | Image builds here need the proxy flags shown above. On an ordinary machine `docker compose up --build` needs nothing extra. |

## Definition of done, per phase

1. The planned functionality works end to end, not only in tests.
2. Automated tests cover it, including failure paths, and pass.
3. Lint and type checks pass.
4. Documentation matches the code; `PROJECT_STATUS.md` is updated.
5. Anything not done, and anything that needs configuration from you, is written down.
