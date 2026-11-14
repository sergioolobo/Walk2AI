# Walk2AI infrastructure

| Path | Contents | State |
|---|---|---|
| `docker/backend.Dockerfile` | Backend image: dependencies from the lockfile in a builder stage, slim runtime, non-root user, health check | Built and run in Phase 1 |
| `scripts/schema_doc.py` | Turns a `pg_dump` into `docs/database/schema.sql` (`make schema-doc`) | In use |
| `deployment/` | Production compose file, Caddy configuration, backup and restore scripts, deployment runbook | Phase 6 |

The local development stack is the `docker-compose.yml` at the repository root: database,
a one-shot migration and seed step, and the backend API. The frontend joins it in Phase 4.

Nothing here creates paid resources or deploys anything. Creating the DigitalOcean
droplet and pointing a domain at it are steps you approve explicitly.
