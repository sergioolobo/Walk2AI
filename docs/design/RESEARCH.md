# Walk2AI — Design research (Phase 4a)

How this was done: from knowledge of how established products in each area work, not
from a fresh survey. The Mobbin connector requires a paid plan and you chose not to
spend, so there are no screenshots or citations here — only patterns that are common
across many well-known products, and what each means for Walk2AI. Nothing is copied
from any product.

## 1. Financial dashboards and fintech apps

Patterns that recur:

- **One number first.** The balance is the largest thing on the screen, with its unit,
  and everything else is subordinate to it.
- **Few accents.** Neutral surfaces, hairline borders, one or two colours that mean
  something. Colour is information, not decoration.
- **Numbers set as numbers.** Tabular figures so columns align; amounts right-aligned;
  the unit smaller than the figure.
- **A short "what changed" summary** near the balance (today, this month) before any
  chart.
- **Trust through traceability.** Every total can be opened into the entries that make
  it up.

For Walk2AI: the balance in Walk Credits leads, with its EUR value as a quiet
secondary line (credits are an allowance, not money — the interface must never make
them look like a bank balance). Today's earning and spending sit beside it.

## 2. Wallets, balances and transaction histories

- **Grouped by day**, newest first, with a day header and that day's net.
- **Sign and colour together**: incoming and outgoing differ by sign, label and
  position, with colour as reinforcement only.
- **Three things per row**: what, when and how much — details on demand.
- **Pending is visibly different** from settled (muted, labelled), and never counted in
  the available balance.
- **Filters by type**, and a running balance where precision matters.

For Walk2AI: the ledger is immutable and already carries `balance_after`, so a running
balance is free. Credits held for an AI request in progress are "pending". Spending on
AI is not a loss, so it is not shown in red: red is kept for errors.

## 3. AI chat and model selection

- **The conversation is the page**: a narrow reading column, generous line height, the
  composer fixed at the bottom.
- **Model choice at the composer**, not buried in settings; each option says what it is
  good for and what it costs.
- **Streaming with a stop control**; the answer's metadata appears when it finishes.
- **History in a side list**, grouped by recency, collapsible.
- **Errors inline** in the conversation, with what happened and what to do.

For Walk2AI: cost is part of the product, so it is shown at three moments — before
sending (the worst-case estimate against the balance), while streaming (credits held),
and after (tokens, exact cost, credits charged). A refusal for lack of credits explains
how many more steps would cover it. Note that stopping a stream does not cancel the
charge (D-43), so no control may suggest that it does.

## 4. Fitness and activity tracking

- **A daily goal as a closing shape** (a ring or a bar) — progress is felt before it is
  read.
- **Today first, then the week**: seven bars with today highlighted.
- **Workouts as cards**: type, duration, time of day.
- **Streaks and personal bests** as light encouragement.
- **Sync state is always visible**: when data last arrived, and from where.

For Walk2AI: the daily cap of 1,500 WC is a natural goal. The earning rules are simple
enough to show in full (steps × 0.1, minutes × 5), so the interface shows the sum
rather than hiding it. Walking and running earn through steps only, and unknown workout
types earn nothing — both must be explained where they occur, not in a help page.
Streaks are not part of the specification and are left out.

## 5. Usage analytics and data visualisation

- **Summary tiles above charts**: totals for the period, with the change from the
  previous one.
- **A date-range control** with presets (7 days, 30 days, this month).
- **Bars for amounts per period, lines for trends**, a table underneath for exact
  figures.
- **Breakdown by dimension** (per model, per provider) as horizontal bars, sorted.
- **Honest scales**: a zero baseline for bars, the same scale for series that are
  compared, direct labels in preference to a legend.

For Walk2AI: the signature chart puts earning and spending on one time axis and one
scale, because the relationship between them is the product. Money is shown in EUR and
credits together where cost is the subject.

## 6. Settings and account management

- **Sections in a side list** (or tabs on a phone): profile, security, devices, limits,
  data.
- **One decision per row**: label, explanation, control; saved state confirmed in place.
- **Dangerous actions set apart**, described plainly, confirmed before acting.
- **Devices and sessions as a list** with last-seen and revoke.

For Walk2AI: spending limits are the most important setting and get plain-language
previews ("at most 500 WC a day — about …"). Device pairing shows the one-time code.
Deleting fitness data states exactly what is kept (credits already earned) and what is
removed.

## Cross-cutting conclusions

1. **Make the chain the hero.** Activity → credits earned → balance → AI consumed →
   allowance left, in that order, on the first screen.
2. **Two meanings, two colours.** One colour for earning (movement), one for spending
   (AI), used the same way on every screen, never for anything else.
3. **Show the arithmetic.** The rules are simple; showing them builds more trust than
   any amount of polish.
4. **Persistent balance.** The available balance is visible on every screen, as in any
   wallet.
5. **Restraint is the premium.** Neutral surfaces, real typography, aligned numbers,
   few colours — rather than gradients and decoration.
6. **Honest states.** Loading, empty, error and "not connected yet" are designed, not
   left to chance: until the iOS companion exists, activity screens must say so.
