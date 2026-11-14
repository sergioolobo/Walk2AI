# Walk2AI backend

FastAPI application: authentication, device pairing, the wallet and its immutable ledger,
fitness ingestion and the reward engine — and, from Phase 3, the AI gateway.

**Status: Phases 1 and 2 complete.** Everything listed under "implemented" below runs
and is covered by tests.

## Run

From the repository root, with `.env` created as described in
[docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md#run-it):

```bash
docker compose up --build
```

API at <http://localhost:8000/api/v1>, interactive documentation at
<http://localhost:8000/api/v1/docs>.

## Implemented

| Area | What exists |
|---|---|
| Accounts | Registration (first account is the admin, then closed), Argon2id passwords, profile, password change |
| Sessions | HttpOnly cookies; 15-minute access token; rotating 30-day refresh token; theft detection; server-side logout |
| Devices | One-time pairing codes; device tokens scoped to `fitness:sync`; revocation |
| Wallet | Rewards, reversals, reservations, settlement, release, expiry, adjustments — all through one service, under a row lock, onto an append-only ledger |
| Wallet API | Balance with EUR equivalent, ledger history, today / month summary in the user's timezone, reservations |
| Spending limits | Per-request, daily, monthly and minimum-balance limits, enforced at reservation under the wallet lock |
| Fitness sync | Idempotent upload from a paired device; per-record validation; duplicate and overlap handling; deletion |
| Reward engine | `min(cap, floor(0.1·steps) + floor(5·minutes))` per local day, paid as a difference, exactly once; reversal on explicit exclusion |
| Reward rules | Versioned by activity date; a change can only start on a day that has not begun; workout types classifiable |
| Test activity | Administrator-only, off by default, forbidden in production, labelled end to end |
| Administration | Audited wallet adjustments, ledger reconciliation, audit log |
| Platform | Typed settings, problem+json errors, request ids, JSON logs, rate limiting, CSRF origin check, health and readiness probes |
| Fitness views | Daily, history and workouts — each workout with the reason it does or does not earn |
| Database | 21 tables, one migration, constraints and triggers that hold even if the application is wrong |
| Operations | `python -m app.cli seed \| reconcile \| expire-reservations` |

## Not implemented yet

| Area | Phase |
|---|---|
| AI gateway, model catalogue, pricing, chat, analytics | 3 |

The tables for these already exist; the code does not, and no endpoint pretends otherwise.

## Layout

| Path | Contents |
|---|---|
| `app/main.py` | Application factory (`uvicorn app.main:create_app --factory`) |
| `app/core/` | Settings, database, security, errors, logging, middleware, rate limiting, clock, timezone arithmetic |
| `app/models/` | SQLAlchemy mapping of the schema |
| `app/schemas/` | Request and response models |
| `app/api/` | Routers: thin — authorise, delegate, commit, serialise |
| `app/services/wallet/` | `WalletService`: the only writer of wallet tables; spending limits |
| `app/services/fitness/` | Normalised records, validation, source policy, ingestion |
| `app/services/rewards/` | The formula as pure functions, rule lookup and administration, `RewardEngine` |
| `app/services/auth/` | Accounts, login sessions, device pairing |
| `app/services/audit/` | Audit trail |
| `migrations/` | Alembic; the migrations *are* the schema |
| `tests/` | 496 tests against a real PostgreSQL |

## Rules worth knowing before changing anything

1. Credits are `int`, money is `Decimal`. No `float` in money code — a test enforces it.
2. Only `WalletService` writes wallet tables, and only `RewardEngine` pays activity
   rewards — tests enforce both.
3. Routers commit; services never do — a test enforces it.
4. Time comes from the injected `Clock` — a test enforces it.
5. Schema changes are hand-written migrations; `tests/test_migrations.py` proves the
   models still match.

Design: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) ·
Schema: [docs/DATABASE.md](../docs/DATABASE.md) ·
API: [docs/API.md](../docs/API.md) ·
Development: [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md)
