# Walk2AI backend

FastAPI application: authentication, device pairing, the wallet and its immutable ledger,
and (from later phases) fitness ingestion, the reward engine and the AI gateway.

**Status: Phase 1 complete.** Everything listed under "implemented" below runs and is
covered by tests.

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
| Administration | Audited wallet adjustments, ledger reconciliation, audit log |
| Platform | Typed settings, problem+json errors, request ids, JSON logs, rate limiting, CSRF origin check, health and readiness probes |
| Database | 21 tables, one migration, constraints and triggers that hold even if the application is wrong |
| Operations | `python -m app.cli seed \| reconcile \| expire-reservations` |

## Not implemented yet

| Area | Phase |
|---|---|
| Fitness sync, reward engine, spending limits | 2 |
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
| `app/services/wallet/` | `WalletService`: the only writer of wallet tables |
| `app/services/auth/` | Accounts, login sessions, device pairing |
| `app/services/audit/` | Audit trail |
| `migrations/` | Alembic; the migrations *are* the schema |
| `tests/` | 299 tests against a real PostgreSQL |

## Rules worth knowing before changing anything

1. Credits are `int`, money is `Decimal`. No `float` in money code — a test enforces it.
2. Only `WalletService` writes wallet tables — a test enforces it.
3. Routers commit; services never do — a test enforces it.
4. Time comes from the injected `Clock` — a test enforces it.
5. Schema changes are hand-written migrations; `tests/test_migrations.py` proves the
   models still match.

Design: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) ·
Schema: [docs/DATABASE.md](../docs/DATABASE.md) ·
API: [docs/API.md](../docs/API.md) ·
Development: [docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md)
