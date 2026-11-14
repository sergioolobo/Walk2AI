# Walk2AI — Security and Privacy

> Status: **Phase 1 and Phase 2 controls are implemented and tested** (authentication,
> sessions, device tokens, wallet integrity, audit logging, input handling, fitness
> validation, reward integrity, spending limits). Controls belonging to later phases are
> marked with their phase and are not in place yet.

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

### Authentication and sessions — implemented (Phase 1)

- Passwords hashed with Argon2id (`argon2-cffi`), 12–128 characters, never logged,
  never echoed in validation errors, transparently re-hashed when parameters are raised.
- Unknown e-mail and wrong password give the same answer and cost the same time (a decoy
  hash is verified when the account does not exist).
- Access token: JWT signed with HS256 (PyJWT), 15 minutes; the algorithm is pinned and
  unsigned tokens are rejected. It is honoured only while its login session is alive in
  the database, and role and account status are read on every request.
- Refresh token: 256 random bits, stored as SHA-256, rotated on every use. Replaying a
  rotated token revokes the whole login session; a 10-second grace window covers two
  tabs refreshing at once.
- Cookies: `HttpOnly`; `Secure` in production (the application refuses to start in
  production otherwise); access `SameSite=Lax`, refresh `SameSite=Strict` and scoped to
  `/api/v1/auth`.
- Logout and password change revoke sessions on the server, not just in the browser.
- Device tokens: 256 random bits, shown once at pairing, stored as SHA-256, scoped to
  `fitness:sync`, revocable. Pairing codes: 8 characters from an unambiguous alphabet,
  10 minutes, single-use, stored as an HMAC keyed with the server secret, 10 attempts
  per minute.
- Role-based authorisation as a FastAPI dependency declared on the admin *router*, so a
  route added there cannot forget it.
- Registration closed after the first account by default; the race for "first account"
  is serialised with a database advisory lock.
- Rate limits on login (per address and per account), registration, pairing, refresh
  and password change.

### Secrets — implemented (Phase 1); server-side handling in Phase 6

- All secrets come from environment variables, loaded through one typed settings object
  as `SecretStr`. The application refuses to start with a short signing secret, and in
  production with placeholder secrets, insecure cookies, a non-HTTPS origin, open
  registration or test data enabled. A test asserts nothing else reads the environment.
- `.env` is git-ignored; `.env.example` contains no real values.
- In production the environment file lives outside the repository with `600` permissions.
  DigitalOcean volumes are encrypted at rest; secrets never enter images or logs.
- Log redaction for `Authorization` headers, cookies, keys and tokens.

### Input and transport — application part implemented (Phase 1); TLS in Phase 6

- Every request body is a Pydantic model with explicit bounds. Unknown fields rejected.
- SQL only through SQLAlchemy with bound parameters.
- Responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- Unhandled errors return a generic problem+json 500 with a request id; details go to
  the server log only.
- Interactive API documentation is disabled in production.
- HTTPS everywhere outside localhost; HSTS; a restrictive Content-Security-Policy for the
  web app; `X-Content-Type-Options`, `Referrer-Policy`, frame denial.
- Assistant output is rendered as Markdown with raw HTML disabled.
- Rate limiting per route group at the application and at the reverse proxy.

### Wallet integrity — implemented (Phase 1)

- Single-writer service, row locking, immutable ledger, idempotency keys, shape and
  non-negativity constraints (see [DATABASE.md](DATABASE.md#invariants-and-where-they-are-enforced)).
- No endpoint lets a client set, add or move credits; the only write path for a person
  is an audited administrative adjustment, which requires a reason and cannot overdraw.
- Concurrency is tested with simultaneous transactions, including a negative control in
  which the application lock is skipped and the database still refuses the overdraft.
- The application's database role is not the owner of the ledger tables' triggers and
  cannot drop them (Phase 6 hardening: separate migration and runtime roles).
- A reconciliation check (`GET /admin/wallet/reconcile`, `python -m app.cli reconcile`)
  compares balances with ledger sums and active holds and reports any difference. It
  never "repairs": a repair would itself rewrite financial history.

### Fitness ingestion and rewards — implemented (Phase 2)

- Activity is accepted only from a paired device's scoped token. A browser session cannot
  post activity; a device token cannot read it back, exclude workouts or delete data.
- The payload carries facts only. It has no field for credits and unknown fields are
  rejected, so nothing a device sends can state or influence an amount.
- Every record is checked for plausibility on the server (no future dates, bounded steps,
  distances and durations, coherent intervals). A rejected record is never stored.
- Hand-entered workouts are stored as unverified and earn nothing. Unknown workout types
  earn nothing until an administrator classifies them: an unexpected label can only
  under-reward.
- A day is paid only the difference between what it is worth and what it has already been
  paid. Replayed batches, re-sent days, duplicated or overlapping workouts and
  simultaneous syncs all pay nothing extra (tested, including under real concurrency).
- Walking, running and hiking workouts add nothing on top of their steps.
- Reward rules can only change days that have not begun anywhere; a rule in force cannot
  be rewritten. Every rule change, workout-type classification and recalculation is
  audit-logged.
- Test activity is a separate, administrator-only route that is refused unless explicitly
  enabled; the application will not start in production with it enabled; everything it
  produces is labelled as test data; and stored test rows earn nothing on an instance
  that forbids them.
- Deleting fitness data cannot be used to earn the same days twice: the date and the
  credits awarded are kept.

### Spending controls — implemented (Phase 2)

- Optional per-request, daily, monthly and minimum-balance limits, set by the user and
  audit-logged when changed.
- Enforced when credits are reserved, under the wallet row lock, in the user's own
  timezone. Credits held for requests in flight count toward the limits.
- With no limits set, the balance itself is still a hard bound: the wallet cannot be
  overdrawn.

### AI gateway — Phase 3

- Official SDKs and official API keys only. No consumer-subscription sessions, cookies or
  unofficial endpoints.
- A request is sent only after a reservation covering its worst-case cost has committed.
- Bounded `max_output_tokens` on every request; bounded request timeout.
- Prompts and responses are stored only in your own database. Provider-side storage
  options are turned off where the API offers the choice.

### Audit logging — implemented (Phase 1), extended by each later phase

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
