# Walk2AI iOS companion

SwiftUI app that reads Apple HealthKit and synchronises activity with the Walk2AI
backend. It does one job: move facts from Apple Health to your server. The web app
remains the main interface.

**Status: not implemented.** This directory is a skeleton created in Phase 0. The Xcode
project is created in Phase 5.

| Path | Will contain |
|---|---|
| `Walk2AI/HealthKit/` | Permission flow, statistics and workout queries, background delivery |
| `Walk2AI/Services/` | Sync engine, API client, Keychain storage |
| `Walk2AI/Models/` | Sync payloads and local state |
| `Walk2AI/Views/` | Pairing, permissions, sync status |

Building requires a Mac with Xcode, the paid Apple Developer Program and a physical
iPhone. It cannot be built on Linux.

Before any of this is built, one question must be answered on your own devices:
[does the Xiaomi Watch S3 deliver workouts to Apple Health?](../docs/FITNESS_INTEGRATION.md#3-device-check-please-run-this-first)
