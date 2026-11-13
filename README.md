# Walk2AI

**Earn your AI usage by moving.** Walk2AI is a personal platform where physical activity
earns internal credits, and those credits are the only way to pay for conversations with
OpenAI and Anthropic models.

```
Physical activity → verified activity data → Walk Credits → AI allowance → AI requests
```

> **Status: Phase 1 complete.** The backend, database, authentication, device pairing and
> the wallet exist and are tested. Fitness sync, the reward engine, the AI gateway, the
> web app and the iOS companion are not built yet. See [PROJECT_STATUS.md](PROJECT_STATUS.md).

## Run it

Requires Docker. Create `.env` from `.env.example`, set `POSTGRES_PASSWORD` and
`JWT_SECRET` (the file explains how to generate them), then:

```bash
docker compose up --build
```

- API: <http://localhost:8000/api/v1>
- Interactive API documentation: <http://localhost:8000/api/v1/docs>

The first account you register becomes the administrator, then registration closes.
Full instructions, including Windows: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#run-it).

## How it works

| Step | What happens | Built |
|---|---|---|
| Move | A Xiaomi Watch S3 and the iPhone record steps and workouts into Apple Health. | — |
| Sync | A small iOS companion reads HealthKit and sends the raw facts to the backend. | Phase 5 (pairing: done) |
| Earn | The backend validates them and pays Walk Credits: `min(1500, 0.1 × steps + 5 × workout minutes)` per day. | Phase 2 (wallet: done) |
| Spend | Each AI request reserves its worst-case cost first, then is charged its actual, provider-reported cost. | Phase 3 (reservations: done) |
| See | One dashboard for activity, credits, tokens and real money spent. | Phase 4 |

**1,000 Walk Credits = €0.10 of AI allowance.** Credits are an internal accounting unit.
They are not money and not a provider balance. Real API charges still occur on your
provider accounts; Walk2AI ensures you never spend there more than you have earned.

## Architecture at a glance

| Part | Technology |
|---|---|
| Web app | Next.js, React, TypeScript, Tailwind CSS, shadcn/ui, Recharts |
| Backend | Python, FastAPI, Pydantic, SQLAlchemy, Alembic |
| Database | PostgreSQL 16 |
| Fitness | Native iOS companion (SwiftUI + HealthKit) |
| AI | Official OpenAI and Anthropic SDKs behind one gateway |
| Hosting | Docker Compose on DigitalOcean, Caddy for HTTPS |

## Documentation

| Document | Contents |
|---|---|
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Where the project stands, test results, risks, next actions |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Running, testing, conventions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, credit economy, reward engine, wallet, AI gateway |
| [docs/DATABASE.md](docs/DATABASE.md) | Schema design and guarantees ([generated SQL](docs/database/schema.sql)) |
| [docs/API.md](docs/API.md) | Backend API |
| [docs/SECURITY.md](docs/SECURITY.md) | Threats, controls, privacy |
| [docs/FITNESS_INTEGRATION.md](docs/FITNESS_INTEGRATION.md) | Xiaomi → Mi Fitness → Apple Health → Walk2AI |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased implementation checklist |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decisions and deviations from the original specification |

## Repository layout

```
backend/         FastAPI application, migrations, tests      (Phase 1 done; 2–3 to come)
frontend/        Next.js web application                     (Phase 4)
mobile/          SwiftUI + HealthKit companion               (Phase 5)
infrastructure/  Dockerfile, scripts, deployment             (Phase 6 for deployment)
docs/            Architecture and reference documentation
```

## Principles

- Provider API keys never leave the backend.
- No AI request is sent without credits reserved to cover it.
- Balances are integers, changed only through an immutable ledger.
- Reward rates and model prices are versioned data, never constants in code.
- Activity is never rewarded twice.
- Nothing is purchased, topped up or deployed automatically.
