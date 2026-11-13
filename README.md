# Walk2AI

**Earn your AI usage by moving.** Walk2AI is a personal platform where physical activity
earns internal credits, and those credits are the only way to pay for conversations with
OpenAI and Anthropic models.

```
Physical activity → verified activity data → Walk Credits → AI allowance → AI requests
```

> **Status: Phase 0 complete — architecture and repository skeleton.** No application
> code exists yet. See [PROJECT_STATUS.md](PROJECT_STATUS.md).

## How it works

| Step | What happens |
|---|---|
| Move | A Xiaomi Watch S3 and the iPhone record steps and workouts into Apple Health. |
| Sync | A small iOS companion reads HealthKit and sends the raw facts to the backend. |
| Earn | The backend validates them and pays Walk Credits: `min(1500, 0.1 × steps + 5 × workout minutes)` per day. |
| Spend | Each AI request reserves its worst-case cost first, then is charged its actual, provider-reported cost. |
| See | One dashboard for activity, credits, tokens and real money spent. |

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
| Hosting | Docker Compose on DigitalOcean, HTTPS reverse proxy |

## Documentation

| Document | Contents |
|---|---|
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Where the project stands, risks, next actions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, credit economy, reward engine, wallet, AI gateway |
| [docs/DATABASE.md](docs/DATABASE.md) | Schema design and guarantees ([reference SQL](docs/database/schema.sql)) |
| [docs/API.md](docs/API.md) | Backend API contract |
| [docs/SECURITY.md](docs/SECURITY.md) | Threats, controls, privacy |
| [docs/FITNESS_INTEGRATION.md](docs/FITNESS_INTEGRATION.md) | Xiaomi → Mi Fitness → Apple Health → Walk2AI, with a device checklist |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Toolchain, conventions, testing strategy |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased implementation checklist |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decisions and deviations from the original specification |

## Repository layout

```
backend/         FastAPI application, migrations, tests      (Phases 1–3)
frontend/        Next.js web application                     (Phase 4)
mobile/          SwiftUI + HealthKit companion               (Phase 5)
infrastructure/  Dockerfiles and deployment                  (Phases 1, 6)
docs/            Architecture and reference documentation
```

## Running what exists today

Only the development database:

```bash
cp .env.example .env      # set POSTGRES_PASSWORD
docker compose up -d db
```

Instructions for the backend, frontend and companion are added as each is built.

## Principles

- Provider API keys never leave the backend.
- No AI request is sent without credits reserved to cover it.
- Balances are integers, changed only through an immutable ledger.
- Reward rates and model prices are versioned data, never constants in code.
- Activity is never rewarded twice.
- Nothing is purchased, topped up or deployed automatically.
