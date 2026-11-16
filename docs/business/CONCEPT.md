# Walk2AI — Concept: "Walk your AI"

_Draft 1 · 16 November 2026 · a hypothesis to test, not a plan to execute_

> **An AI companion that runs on your steps.** Like a dog, it needs walks. Unlike a dog,
> it helps you with anything.

"Walk2AI" is the working name. Check that it is free to use (trademarks, app-store
names, domain) before publishing anything under it.

---

## 1. Why change the concept

The original Walk2AI is a personal tool: walking earns credits, and the credits allow
the owner to spend money they already have on AI. Money only flows out, so it can never
be a business as it stands. A business needs someone to pay more than it costs to serve
them.

Two things learned along the way shape this concept:

1. **The daily cap is a cost control.** If users pay a flat subscription and their
   movement unlocks usage, the most any user can cost is known in advance. Every
   subscriber carries a guaranteed margin.
2. **The reward has to be something people care about.** A few cents of AI credit is not.
   A character you look after can be: Tamagotchi, Pokémon GO and Pikmin Bloom all show
   that caring for a virtual creature changes what people do every day.

## 2. The concept

You get a personal AI companion — a small character with a name, a look and a
personality. It is genuinely useful: it answers questions and helps you plan, write and
learn, like any AI assistant.

| Mechanic | How it works |
|---|---|
| **Energy** | The companion has an energy bar. Only your movement fills it: steps charge it, a workout gives it a boost. |
| **Talking costs energy** | Every message uses a little. An active day means a talkative companion; a lazy day makes it sleepy and brief. Longer, harder questions cost more. |
| **Growth** | Days in a row with enough movement make it grow: new looks, abilities and personalities. A long streak becomes something visible you don't want to lose. |
| **Crews** | Friends form a crew. Everyone's steps count towards a shared weekly goal, and reaching it unlocks something for every companion in the crew. People walk partly so as not to let the others down. |

What it is not: a fitness tracker with charts, a way to earn money, or a romantic
companion.

### Proposed numbers (to be tuned in testing)

| Rule | Proposal |
|---|---|
| Full battery | 100% = 10,000 steps (1% per 100 steps); a workout adds 1% per minute; capped at 100% a day |
| A short message | about 2% (so a full battery is about 50 short messages) |
| A long or "think harder" message | more, in proportion to its real cost |
| Unused energy | carries over a little (for example up to 20%) so one rest day doesn't hurt |
| Growth | a stage every 7 days with at least 50% charge |

## 3. Who it is for

- **First:** adults (18+) who already use AI chat apps and would like to move more. They
  understand the AI part immediately, and the companion gives them a reason to walk.
- **Second:** friend groups, couples and colleagues who like light competition and
  shared goals. Crews are how the product spreads from person to person.
- **Not at first:** children. Data from minors is the hardest case to handle legally,
  even though the idea would appeal to parents.

## 4. Why people would join, and stay

- **Understood in one sentence**, with a joke built in ("you have to walk your AI").
- **Attachment.** People look after things they have named and watched grow.
- **Loss aversion.** A streak and a grown companion are things you don't want to lose.
- **Social accountability.** A crew notices when you don't show up.
- **The limit feels like a game rule**, not a paywall: "he's tired, take him out".

The one thing that must be tested before anything else: **do people want this
combination?** Companion apps and fitness games each work on their own; nobody has shown
that the two together make people pay.

## 5. Business model

- **Subscription:** around **€3.99 a month** (price including VAT), after a free trial.
- **Optional extras later:** cosmetic items for the companion. Never energy for sale:
  buying energy would break the whole idea.
- **No advertising and no selling of data.** The product handles activity data, which is
  health data in law; trust is part of what is sold.

### Unit economics (estimates, November 2026 prices)

What one subscriber brings in, on Google Play:

| | Per month |
|---|---|
| Price paid | €3.99 |
| − VAT (23% in Portugal; the store collects and pays it) | €3.24 net |
| − store fee (15% on subscriptions) | **€2.76** |

What one subscriber costs in AI, using a model in the price class of Claude Haiku 5.5 or
GPT-6 Luna ($0.10 per million input tokens, $0.50 per million output tokens, verified on
the providers' pages on 15 November 2026):

| | Estimate |
|---|---|
| One short message, conversation history included (about 3,000 tokens in, 300 out) | about €0.0004 |
| A full battery every single day (about 50 messages × 30 days) | about **€0.60** at most |
| A typical user (half the battery, most days) | about €0.20–0.30 |

The energy cap is what makes this safe: the heaviest possible user still leaves about
€2.15 a month. Costs that don't depend on users: a small server, about €10–20 a month.

| Paying subscribers | Net per month, after VAT, store fee and AI (typical) | After server |
|---|---|---|
| 10 | ~€25 | ~€10 |
| 100 | ~€250 | ~€235 |
| 1,000 | ~€2,500 | ~€2,480 |
| 10,000 | ~€25,000 | more servers and support needed |

Income tax is not included. (An earlier estimate in conversation left out VAT; these
figures correct it.)

**Reality check.** Consumer subscription apps typically convert a few percent of people
who try them into payers, and many subscribers cancel within months. 1,000 paying
subscribers might need tens of thousands of downloads. This is a side business unless
crews make it spread on their own.

## 6. Competition and what's different

| Category | Examples | Their strength | What they don't do |
|---|---|---|---|
| AI chat apps | ChatGPT, Claude, Gemini | Free tiers that are more capable | No reason to move; no character |
| Walking games | Pokémon GO, Pikmin Bloom | Proven that games get people walking | No useful AI |
| Move-to-earn | Sweatcoin, StepBet | Large audiences; money is motivating | Money attracts cheating, and some fell apart when the rewards lost value |
| AI companions | Character-style apps | Strong attachment | Mostly entertainment; no link to real life |
| Fitness coaches | Apple, Google, Whoop | Data and ecosystems | Serious and data-heavy, not playful |

The differentiator is the combination: a **useful** AI with **personality**, whose
**limit is your own movement**, in a **social** setting. Because nothing is ever paid out
in money, cheating only fools the cheater's own companion. That is the advantage over
move-to-earn.

## 7. What already exists

The Walk2AI backend built in Phases 1–3 is most of the engine:

| Built | Becomes |
|---|---|
| Wallet and immutable ledger, with reservations | Energy, exactly accounted |
| Reward engine (steps, workouts, daily cap, Lisbon days) | Charging |
| AI gateway: providers, exact pricing, cost caps, streaming | The companion's brain, with protected margins |
| Conversations, analytics | Memory, streaks, growth |
| Authentication, device pairing, fitness sync | Accounts and phones |

Missing: multiple users at scale (registration is deliberately limited to one account
today), the companion itself (personality, looks, growth), crews, subscriptions, and a
phone app.

## 8. Platform plan

- **Android first.** It can be built entirely on a Windows PC. Activity comes from
  Health Connect. A Google Play developer account costs a one-time $25.
- **iPhone later**, once there is revenue: it needs a Mac and the Apple Developer
  Program (€99 a year).
- **The website** (waitlist now, account pages later) works everywhere.

## 9. Risks

| Risk | How it is handled |
|---|---|
| Nobody wants the combination | Test with a waitlist before building anything more (section 10) |
| People try it, then leave | Streaks, growth and crews are retention mechanics; measure them in a small beta before charging |
| Cheating (phone shakers) | It only affects the cheater's own companion; nothing is paid out |
| Unhealthy attachment | A helpful buddy, not a romantic partner; adults only; no manipulative notifications |
| Health data under GDPR | Collect only step counts and workouts; explicit consent; data stays in the EU where possible; written privacy policy before launch |
| AI prices or policies change | Several providers already supported; open models as a fallback; the energy cost per message can be re-tuned |
| App-store rules on subscriptions and health data | Follow them from the start; budget for the store's review time |
| The name is taken | Check before publishing anything |

## 10. Validation plan

Nothing more is built until this answers "yes".

1. **Waitlist page** (built: `landing/`). One sentence, the companion, an email sign-up
   and one optional question: *"Would you pay about €4 a month?"*
2. **Share it** where walkers and AI users gather: relevant subreddits, Portuguese online
   communities, friends and family, with a 30-second explanation.
3. **Measure for four weeks:**

   | Signal | Promising | Rethink |
   |---|---|---|
   | Sign-ups | 100 or more | fewer than about 20 |
   | "Would pay about €4" (yes + maybe) | 40% or more | under 15% |
   | Replies to a follow-up email | several people keen to talk | silence |

   These thresholds are judgement calls for a small, informal test, not science.
4. **Talk to 10 people who signed up.** Ask about their walking, their AI use, what they
   would want the companion to do, and what would make them stop using it.
5. **Live with it yourself** for a month: the current Walk2AI with a companion
   personality. Do you walk more?

Then decide: build the Android beta, change the concept, or stop.

## 11. Open decisions

1. The name (and checking it is free).
2. The price to test (€3.99 is a starting point).
3. Whether crews are in the first version or come later.
4. The companion's look: one character for everyone, or several to choose from.
5. Who is the "we" on the waitlist page and in the privacy notice (your name and a
   contact email are required before it goes live).
