# Walk2AI web app

Next.js application: dashboard, AI chat, activity, wallet, usage analytics, settings.

**Status: not implemented.** This directory is a skeleton created in Phase 0. The project
is created in Phase 4, after the backend endpoints it depends on exist — every screen is
built against real endpoints, never mock figures.

| Path | Will contain |
|---|---|
| `app/` | Routes (App Router): authenticated shell and pages |
| `components/` | UI components (shadcn/ui based), charts (Recharts) |
| `hooks/` | Data hooks (TanStack Query), streaming chat hook |
| `lib/` | API client, formatters for credits and money |
| `types/` | Types generated from the backend's OpenAPI schema |

Design: [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md#12-frontend)
