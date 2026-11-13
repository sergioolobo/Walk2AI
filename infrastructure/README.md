# Walk2AI infrastructure

**Status: not implemented.** This directory is a skeleton created in Phase 0.

| Path | Will contain | Phase |
|---|---|---|
| `docker/` | Dockerfiles for the backend and frontend | 1, 4 |
| `deployment/` | Production compose file, reverse-proxy configuration, backup and restore scripts, deployment runbook | 6 |

The local development stack is the `docker-compose.yml` at the repository root. Today it
defines the database only.

Nothing here creates paid resources or deploys anything on its own. Creating the
DigitalOcean droplet and pointing a domain at it are steps you approve explicitly.
