# Walk2AI — Security and Privacy

> Status: **Phase 0 design, awaiting approval.** Controls are marked with the phase in
> which they are implemented. Nothing here is in place until that phase is complete.

## What is being protected

| Asset | Why it matters |
|---|---|
| Provider API keys | Anyone holding them can spend your money directly at OpenAI / Anthropic. |
| The wallet ledger | It decides how much real money the application is allowed to spend. |
| Fitness data | Personal health-related data. |
| Conversation content | Private. |
| The account | Controls all of the above. |

## Threats considered

| Threat | Mitigation |
|---|---|
| Provider key leaks through the frontend or the phone | Keys exist only in backend environment variables. No endpoint returns them; no client code references them. |
| Client edits its own balance | No balance-setting endpoint exists. Balances change only inside `WalletService` as a result of validated server-side events. |
| Phone reports inflated activity | The phone sends facts, the server calculates. Plausibility bounds, daily cap, user-entered samples excluded, per-day reward ledger. |
| Replay of a sync or a chat request | Idempotency keys (`batch_id`, `request_id`) backed by unique constraints. |
| Two requests spend the same credits | Row lock on the wallet for every operation; non-negative `CHECK` as a last line. |
| A stolen device token | Scoped to `fitness:sync`: it cannot read conversations, spend credits or reach admin routes. Revocable from settings. |
| A stolen session cookie | `HttpOnly`, `Secure`, `SameSite=Lax`; short access-token life; refresh rotation with reuse detection revokes the chain. |
| CSRF | Same-origin deployment, `SameSite=Lax`, and an `Origin` check on every state-changing request. |
| Brute-force login | Argon2id hashing, per-IP and per-account rate limits, uniform error messages. |
| Strangers registering | `REGISTRATION_MODE=first_user_only` by default. |
| A bug that overspends | Provider-side prepaid balance with auto-recharge off (below). |
| Runaway cost from one prompt | Worst-case reservation before sending; per-request, daily and monthly limits. |
| Tampering with history | Ledger and audit log are append-only at the database level. |
| Loss of the database | Encrypted off-box backups with a tested restore (Phase 6). |

## Honest limits

- **"Verified activity" is not cryptographic proof.** HealthKit data arrives from a device
  you control. Verification means: it came through an authenticated, paired device, it was
  not hand-entered in the Health app, it passes plausibility checks, and it has not been
  rewarded before. For a personal platform that is the right bar. Apple's App Attest could
  later prove requests come from the genuine app binary; it is not planned for the MVP.
- **Walk Credits are not a provider balance.** The interface never implies otherwise.
  Internal credits can only *restrict* spending; they cannot make a provider request
  succeed if the provider account itself has no funds.
- **Provider cost is computed, not billed.** Cost is calculated from the provider's
  reported token usage and the stored price list. If a provider changes prices and the
  price table is not updated, computed cost will drift from the invoice. Mitigations: the
  `verified_at` date is shown in the admin view, and Phase 3 adds a reconciliation report
  against the provider's own usage reporting where the account type supports it.

## Controls by area

### Authentication and sessions — Phase 1

- Passwords hashed with Argon2id (`argon2-cffi`), minimum length enforced, never logged.
- Access token: signed, ~15 minutes. Refresh token: random, stored hashed, rotated on
  every use; presenting an already-rotated token revokes the whole chain.
- Cookies: `HttpOnly`, `Secure` (production), `SameSite=Lax`, scoped to the site origin.
- Device tokens: 256 bits of randomness, shown once at pairing, stored as a SHA-256 hash,
  kept in the iOS Keychain on the phone.
- Role-based authorisation as a FastAPI dependency; admin routes are a separate router
  that cannot be reached without the dependency.

### Secrets — Phases 1 and 6

- All secrets come from environment variables, loaded through one typed settings object.
  The application refuses to start in production with missing or placeholder secrets.
- `.env` is git-ignored; `.env.example` contains no real values.
- In production the environment file lives outside the repository with `600` permissions.
  DigitalOcean volumes are encrypted at rest; secrets never enter images or logs.
- Log redaction for `Authorization` headers, cookies, keys and tokens.

### Input and transport — Phases 1 and 6

- Every request body is a Pydantic model with explicit bounds. Unknown fields rejected.
- SQL only through SQLAlchemy with bound parameters.
- HTTPS everywhere outside localhost; HSTS; a restrictive Content-Security-Policy for the
  web app; `X-Content-Type-Options`, `Referrer-Policy`, frame denial.
- Assistant output is rendered as Markdown with raw HTML disabled.
- Rate limiting per route group at the application and at the reverse proxy.

### Wallet integrity — Phase 2

- Single-writer service, row locking, immutable ledger, idempotency keys, shape and
  non-negativity constraints (see [DATABASE.md](DATABASE.md#invariants-and-where-they-are-enforced)).
- The application's database role is not the owner of the ledger tables' triggers and
  cannot drop them (Phase 6 hardening: separate migration and runtime roles).
- A reconciliation check compares balances with ledger sums and reports any difference.

### AI gateway — Phase 3

- Official SDKs and official API keys only. No consumer-subscription sessions, cookies or
  unofficial endpoints.
- A request is sent only after a reservation covering its worst-case cost has committed.
- Bounded `max_output_tokens` on every request; bounded request timeout.
- Prompts and responses are stored only in your own database. Provider-side storage
  options are turned off where the API offers the choice.

### Audit logging — from Phase 1

Recorded: registration, login success and failure, logout, session revocation, device
pairing and revocation, changes to reward rules, prices, exchange rates, models and
spending limits, admin wallet adjustments, workout exclusions, fitness data deletion.
Not recorded: passwords, tokens, keys, message content.

## Provider-side controls

These are settings in the provider consoles, not code. They are the only control that
still holds if Walk2AI itself is wrong, so they are part of the design rather than an
afterthought. To be done by you when the accounts are created (Phase 3):

1. **Prepay a small amount** on each provider and **leave auto-recharge / auto-reload
   off.** The prepaid balance becomes the absolute ceiling on real spending.
2. Create a **dedicated project / workspace** for Walk2AI on each provider and generate
   the API key inside it, so usage is separable from anything else you do.
3. Set a **monthly budget or spend limit** on that project / workspace where offered.
4. Use a key with the narrowest permissions available. Rotate it if it is ever exposed.

Walk2AI never changes these settings and never purchases credit.

## Privacy

- **Minimum data.** HealthKit permissions requested: step count, walking + running
  distance, cycling distance, workouts, and active energy (optional). Not requested: heart
  rate, sleep, body measurements, location, or anything else.
- **Read-only.** The companion never writes to HealthKit.
- **Your server only.** Fitness data goes from the phone to your backend. It is never sent
  to an AI provider and never shared with a third party.
- **Disconnect and delete.** Revoking the device stops syncing. `DELETE /fitness/data`
  removes stored daily records and workouts. Credits already earned stay in the ledger as
  amounts with dates; the underlying activity detail is gone.
- **Conversations** can be deleted individually; their content is removed, their cost
  record (no content) remains.
- **GDPR.** As a single-user personal deployment you are both controller and data subject.
  Health-related data is special-category data; if the platform is ever opened to other
  people, a lawful basis, privacy notice, export and erasure flows become mandatory and
  this section must be revisited before that happens.

## Before going public (Phase 6 checklist)

- [ ] All secrets rotated from development values
- [ ] `COOKIE_SECURE=true`, HSTS on, registration closed
- [ ] `ALLOW_TEST_FITNESS_DATA=false`
- [ ] Database not reachable from the internet; firewall allows 80/443 and SSH only
- [ ] SSH key-only access, automatic security updates
- [ ] Backups running and a restore rehearsed
- [ ] Provider prepaid balances and limits confirmed
- [ ] Dependency audit (`pip-audit`, `pnpm audit`) clean or triaged
