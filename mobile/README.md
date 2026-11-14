# Walk2AI iOS companion

SwiftUI app that reads Apple HealthKit and synchronises activity with the Walk2AI
backend. It does one job: move facts from Apple Health to your server. The web app
remains the main interface.

**Status: not implemented.** This directory is a skeleton. Nothing here is built, signed
or deployed.

## Where this stands

- **The data path is confirmed.** Steps and workouts from the Xiaomi Watch S3 reach Apple
  Health through Mi Fitness (you verified this on 2026-11-13).
- **The server side of pairing exists.** One-time pairing codes and device tokens scoped
  to `fitness:sync` were built in Phase 1, so the contract this app will use is fixed.
- **The sync endpoint is Phase 2.** It is built and tested with labelled test data, so it
  does not wait for this app.
- **No Mac is available**, and no Apple hardware or paid Apple Developer Program is to be
  bought. iOS apps cannot be compiled on Windows or Linux, so before Phase 5 there is a
  no-cost feasibility investigation into how — and whether — to build this natively.
  See [docs/ROADMAP.md](../docs/ROADMAP.md#before-phase-5--feasibility-investigation).

## Planned layout

| Path | Will contain |
|---|---|
| `Walk2AI/HealthKit/` | Permission flow, statistics and workout queries, background delivery |
| `Walk2AI/Services/` | Sync engine, API client, Keychain storage |
| `Walk2AI/Models/` | Sync payloads and local state |
| `Walk2AI/Views/` | Pairing, permissions, sync status |

Design: [docs/FITNESS_INTEGRATION.md](../docs/FITNESS_INTEGRATION.md)
