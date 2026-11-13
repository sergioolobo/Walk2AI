# Walk2AI backend

FastAPI application: authentication, fitness ingestion, reward engine, wallet, AI gateway,
analytics and administration.

**Status: not implemented.** This directory is a skeleton created in Phase 0. The project
files (`pyproject.toml`, application code, migrations, tests) arrive in Phase 1.

| Path | Will contain | Phase |
|---|---|---|
| `app/core/` | Settings, database session, security, logging, rate limiting, errors | 1 |
| `app/models/` | SQLAlchemy models | 1 |
| `app/schemas/` | Pydantic request and response models | 1–3 |
| `app/api/` | Routers (thin: parse, authorise, delegate, serialise) | 1–3 |
| `app/services/fitness/` | Sync ingestion, validation, de-duplication | 2 |
| `app/services/rewards/` | Reward engine | 2 |
| `app/services/wallet/` | Wallet service — the only writer of wallet tables | 2 |
| `app/services/ai/` | Gateway, provider adapters, pricing, estimation | 3 |
| `app/services/analytics/` | Read-only aggregations | 3 |
| `migrations/` | Alembic migrations | 1 |
| `tests/` | pytest suite (runs against a real PostgreSQL) | 1– |

Design: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) ·
Schema: [docs/DATABASE.md](../docs/DATABASE.md) ·
API: [docs/API.md](../docs/API.md)
