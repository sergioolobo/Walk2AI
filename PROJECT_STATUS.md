# Walk2AI — Project Status

_Last updated: 2026-11-15_

## Current phase

**Phase 3 — AI gateway: complete and confirmed against both live providers.**
**Phase 4 — the web application: design stage, waiting for your choice of visual
direction.** Research is written up and three directions for the dashboard are drawn
(`docs/design/`). No Mobbin, no Figma, nothing spent (D-53). No frontend code exists or
will be written before you have reviewed the designs.

## Business concept (new, 2026-11-16)

As a personal tool, Walk2AI only ever spends money: walking unlocks AI you pay for
yourself. To explore turning it into a business, a new concept is drafted and ready to
test before anything more is built:

- **"Walk your AI"**: an AI companion that runs on your steps — energy from movement,
  growth from consistency, crews of friends — paid for by a small subscription, with the
  energy cap guaranteeing a margin on every subscriber. See
  [docs/business/CONCEPT.md](docs/business/CONCEPT.md).
- **A bilingual waitlist page** to test demand and willingness to pay, ready to host for
  free: [landing/](landing/README.md). It needs your name and a contact email for its
  privacy notice before it can go live.

The frontend design work (Phase 4) is paused until the test says whether to continue
with this concept.

## Phases

| Phase | Status |
|---|---|
| 0 — Architecture and repository preparation | Done; approved 2026-11-13 |
| 1 — Backend foundation and wallet | Done |
| 2 — Fitness ingestion, reward engine, spending limits | Done; you approved continuing on 2026-11-15 |
| 3 — AI gateway | Done — first real request to each provider succeeded on 2026-11-15; automatic ECB rate added |
| 4 — Frontend | **Design stage: research done; three visual directions await your choice** |
| Before 5 — iOS feasibility investigation (no Mac, no purchases) | Not started |
| 5 — iOS and HealthKit | Not started |
| 6 — Production deployment | Not started |

## Implemented functionality

Everything in this table runs, and is covered by automated tests.

| Area | What works |
|---|---|
| Stack | `docker compose up --build` starts PostgreSQL, applies migrations, seeds configuration and the model catalogue, and serves the API on `localhost:8000` |
| Accounts and sessions | Registration (first account becomes admin, then closed), Argon2id passwords, HttpOnly cookies, rotating refresh tokens, theft detection, server-side logout |
| Device pairing | One-time pairing codes; device tokens limited to `fitness:sync`; revocation |
| Wallet | Reward, reversal, reserve, settle, release, expire, adjust — one service, row-locked, on an append-only ledger with idempotency keys |
| Fitness sync | Daily totals and workouts from a paired device; idempotent; validated; de-duplicated; HealthKit's decimal numbers accepted and normalised |
| Reward engine | `min(1500, 0.1 × steps + 5 × workout minutes)` per local day; no double rewards; late data within 7 days; unknown workout types earn nothing |
| Spending limits | Optional per-request, daily, monthly and minimum-balance limits, enforced when credits are reserved |
| **AI providers** | Anthropic (Messages API) and OpenAI (Responses API, `store: false`) through the official SDKs, streamed; usage normalised across both; every error class mapped |
| **Model catalogue** | Models, versioned prices (per category, long-prompt tier) and conversion rates, administered and audited; a model is offered only when enabled, confirmed available, priced from a cited source, and convertible to credits |
| **Pricing engine** | Exact cost from reported usage; worst case from the provider's own token count; credits rounded up, minimum 1 WC; the price version and rates of every request are stored |
| **AI gateway** | Estimate → reserve (committed) → stream → settle exactly, in one transaction with the usage record and the answer. Refused before sending: insufficient credits, a limit, an unusable model, a reused request id, a conversation too long |
| **Failure handling** | Provider errors, refusals, partial streams, time limits, shutdown, crashed requests — each with a defined charge (exact, nothing, or the reserved worst case) and a recorded status |
| **Disconnects** | A closed browser does not stop or escape a request: it finishes on the server, is charged exactly and the answer is saved |
| **Conversations** | Create, list, read (with tokens, cost and credits per answer), rename, delete; the last 20 complete messages are sent as context |
| **Analytics** | Activity, rewards, AI usage (per model, with averages) and credit flows by day, week or month in Lisbon time; a dashboard summary |
| Administration | Wallet adjustments, reconciliation, audit log, reward rules, workout types, recalculation, models, prices, exchange rates |
| Platform | Typed settings with production safety checks, problem+json errors, request ids, JSON logs, rate limiting, CSRF origin check, probes, graceful shutdown that settles requests in progress |
| **Automatic USD→EUR rate** | The ECB's daily reference rate, fetched at start-up and every three hours; versioned; outages and implausible figures leave the last rate in force; a hand-set rate holds until the ECB's next publication; status and refresh-now for the administrator |
| Quality | Ruff, mypy (strict), CI on GitHub Actions |

Not implemented yet: the web interface (Phase 4) and the iOS companion (Phase 5). Until
then the API is used through its interactive documentation (`/api/v1/docs`), and
activity can only be test activity.

## Test results (2026-11-15)

| Check | Result |
|---|---|
| Automated tests | **658 passed**, 0 failed, 0 skipped (~77 s); 153 added in Phase 3 |
| ECB rate, live | `refresh-rates` run against the real ECB service: applied 2026-11-13's rate (1 USD = 0.90481361 EUR); a second run reported nothing new |
| No real provider calls | Every AI test uses a scripted fake or the real SDKs over an in-memory transport. Nothing was sent to OpenAI or Anthropic; nothing was spent |
| Timing-sensitive AI tests repeated 10× | Stable (disconnect, concurrency, deletion mid-answer, shutdown, time limits, crash) |
| Ruff lint and format | Clean |
| mypy (strict), 119 source files incl. tests | Clean |
| Docker image | Rebuilt with both SDKs; runs as a non-root user; the Compose stack applies migration 0002, seeds the catalogue and serves all 51 routes |
| Real-process smoke test | The server under Uvicorn, against a local fake Anthropic API over real sockets: answers streamed progressively; exact charge; a disconnect mid-answer still finished and was charged on the server; a SIGTERM mid-answer let it finish and settle before exit; the ledger reconciled |

Phase 3 tests by area: chat end to end 25 · provider SDK adapters 30 · pricing 32 ·
catalogue administration 14 · analytics 12 · gateway internals and both SDKs end to end
10 · migrations and seed +1 · architecture rules +4.

## Known issues and limitations

| # | Item | Impact | Status |
|---|---|---|---|
| L1 | `docker compose build` cannot run to completion in the development sandbox (TLS-inspecting proxy); the image is built there with `docker build` plus proxy flags. | None for you: you confirmed `docker compose up --build` works on your machine. | Closed for you |
| L3 | Uvicorn's own start-up lines are plain text; application logs are JSON. | Cosmetic. | Tidy in Phase 6 |
| L4 | The rate limiter is per process. | Correct for one backend process, which is the design. | By design |
| L6 | No email flows (verification, password reset). | A forgotten password needs a database-level reset. | Revisit in Phase 6 |
| L7 | Workout type labels from your watch are unconfirmed. | Unknown types earn nothing (your decision). | Phase 5 |
| L8 | Real activity cannot arrive until the iOS companion exists. | Test activity only. | Phase 5 |
| L9 | No web interface yet. | The API works through `/api/v1/docs`. | Phase 4 |
| L10 | OpenAI's website was at first unreachable from this environment. | Closed: you allowed it; `gpt-6-luna`'s price is now verified on OpenAI's page (with its cache-write prices). An existing database is upgraded on its next start. | Closed |
| L11 | First real requests (2026-11-15, run by you): Claude Haiku 5.5 — 36 in / 8 out tokens, USD 0.0000076, 1 WC; GPT-6 Luna — 23 in / 7 out, USD 0.0000058, 1 WC. Charges match the price × usage exactly. | Both live integrations confirmed. | Closed |
| L12 | No automatic reconciliation with the providers' own usage reports. | Requests with an uncertain outcome are marked `unknown` for a manual comparison. Automating it would need an admin-scoped provider key, which Walk2AI deliberately does not hold. | By design for now |
| L13 | A closed browser does not cancel an answer (D-43). | You pay for an answer you stopped watching, at most its reserved worst case; the answer is kept. | By design |

## Risks

| # | Risk | Status |
|---|---|---|
| R2 | No Mac: the iOS companion cannot be built or tested by either of us today | Open — investigated before Phase 5, at no cost |
| R3 | HealthKit may require the paid Apple Developer Program | Open — same investigation; nothing purchased without approval |
| R4 | iOS background sync is not real-time | Accepted — designed for |
| R5 | Provider prices change | Mitigated — versioned, sourced, dated prices; a verified price can never be edited; every charge records its price version |
| R6 | A full day's allowance is €0.15 | By design — Haiku 5.5 answers cost 1–10 WC each; Opus 5.5 can cost hundreds |
| R8 | Workout type labels may differ from the seeded defaults | Low — can only under-reward |
| R9 | A provider changes its streaming or usage format | Mitigated — only the adapters know the formats; their tests fail loudly; the gateway charges the worst case if usage goes missing after generation began |

## Required external configuration

To run the stack: Docker, and a `.env` with `POSTGRES_PASSWORD` and `JWT_SECRET`. Nothing
else.

To use the AI (each step only with your go-ahead):

| Item | Notes |
|---|---|
| Anthropic API key — dedicated workspace, small prepaid balance, auto-reload off | `ANTHROPIC_API_KEY` in `.env` only |
| OpenAI API key — dedicated project, small prepaid balance, auto-recharge off | `OPENAI_API_KEY` in `.env` only |
| USD→EUR rate | Automatic from the ECB — nothing to configure |
| Model check and enabling | `POST /admin/models/{id}/check` (free), then `PUT /admin/models/{id}` `{"enabled": true}` |
| A decision on the iOS build route | Before Phase 5 |
| DigitalOcean account, droplet, domain | Phase 6; paid; only with your approval |

Provider accounts were created by you, each with a small prepaid balance and automatic
recharge off. The only real AI spend so far is the two test requests above (under
USD 0.00002 in total). Nothing deployed, no other paid service.

## Next actions

1. **You:** open the design canvas (link in `docs/design/README.md`) and choose a visual
   direction — A, B, C, or a mix.
2. **Me:** the design system and the main screens (Dashboard, AI Chat, Activity, Wallet,
   Usage Analytics, Settings; desktop, tablet and mobile; all states) in that direction,
   documented in `docs/design/`.
3. **You:** review the main screens. No frontend code before that.
4. **Me, after your approval:** Phase 4c — implementation.
5. **You (on your machine):** `git pull` and `docker compose up -d --build` to get the
   automatic ECB rate.
