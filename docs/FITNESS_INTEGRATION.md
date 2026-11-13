# Walk2AI — Fitness Integration

> Status: **Pipeline confirmed viable; design approved.** On 2026-11-13 you confirmed on
> your own devices that workouts synchronise from Mi Fitness to Apple Health, so Apple
> Health is the source for both steps and workouts. The server side of this integration
> is built in Phase 2; the iOS companion in Phase 5, after a feasibility investigation
> (you have no Mac — see [section 9](#9-building-the-ios-companion-without-a-mac)).

## 1. The pipeline

```
Xiaomi Watch S3 ──Bluetooth──▶ Mi Fitness (iPhone) ──writes──▶ Apple Health
                                                                    │ HealthKit (read-only)
                                                                    ▼
                                    Walk2AI backend ◀──HTTPS── Walk2AI iOS companion
```

Three independent hops, each with its own delay and failure modes:

| Hop | Controlled by | What can go wrong |
|---|---|---|
| Watch → Mi Fitness | Xiaomi | Syncs when the app runs; can lag until Mi Fitness is opened |
| Mi Fitness → Apple Health | Xiaomi | Only writes the categories it supports and you have enabled |
| Apple Health → Walk2AI | Us | iOS decides when background work runs; no access while the phone is locked |

Walk2AI controls only the last hop. The design therefore assumes data is late, partial
and occasionally revised, and treats that as normal rather than as an error.

## 2. What Mi Fitness writes to Apple Health

Public sources could not settle the Watch S3's exact behaviour: Xiaomi does not publish a
per-device list, third-party sources disagree, and none mention the Watch S3 by name
(Xiaomi's own support pages could not be opened from the development environment, so they
are cited from search summaries only). Your own check settled the one question that
mattered — workouts do arrive.

| Metric Walk2AI needs | HealthKit type | Does Mi Fitness write it? | Confidence | If it does not |
|---|---|---|---|---|
| Daily steps | `stepCount` | Yes | **High** — consistent across sources | iPhone's own step count still works (phone must be carried) |
| Walking + running distance | `distanceWalkingRunning` | Probably | Medium — not explicitly confirmed | iPhone records it too; display-only metric, does not affect rewards |
| Active energy | `activeEnergyBurned` | Probably | Medium — reported for older bands | Optional metric; omit |
| Workouts (type, start, end, duration) | `HKWorkout` | **Yes** | **Confirmed by you** on your devices | — |
| Strength training | `HKWorkout` (`traditionalStrengthTraining`) | Yes, as a workout | Type label to be confirmed | Unrecognised types are stored and earn nothing until classified |
| Cycling | `HKWorkout` (`cycling`) + `distanceCycling` | Yes, as a workout | Type label to be confirmed | As above |

What this means for the reward formula `WC = min(1500, 0.1 × S + 5 × M)`:

- **`S` (steps)** is the primary earning mechanism.
- **`M` (workout minutes)** comes from verified workouts that are not step-based.
- **No double reward.** Walking, running and hiking workouts are classified
  `step_based`: their effort is already in the step count, so they add no minutes.
- The one remaining detail is which *type label* each of your workouts carries in Apple
  Health. A type the server does not recognise is stored and earns nothing until an
  administrator classifies it, so an unexpected label can under-reward but never
  over-reward.

## 3. Device check

**Result (2026-11-13): workouts synchronise from Mi Fitness to Apple Health.** The
procedure is kept here because the remaining observations — type labels, how
fine-grained the step entries are, how long data takes to appear — are still useful for
tuning Phase 2, and because it is how to diagnose the pipeline if sync ever stops.

Nothing to install. Only the watch, the iPhone and the two apps you already use.

**Setup**

1. Mi Fitness → **Profile → Third-party data → Health** (older versions: *App
   permissions → Health*). Enable every activity category. Note which categories are
   offered.
2. iPhone **Health** app → profile picture → **Apps → Mi Fitness**. Note the categories
   listed under "Allow Mi Fitness to write".

**Generate data**

3. With the watch on, walk for about ten minutes.
4. On the watch, record a **strength training** workout for about five minutes.
5. On the watch, record a **cycling** workout (indoor is fine) for about five minutes.
6. Open Mi Fitness and let it sync.

**Observe in the Health app**

| Where | Question |
|---|---|
| Browse → Activity → **Steps** → Data Sources & Access | Is *Mi Fitness* listed as a source? |
| Steps → Show All Data → pick today | How fine-grained are Mi Fitness entries: per minute, per hour, or one per sync? |
| Browse → Activity → **Workouts** | Do both workouts appear? Is the source Mi Fitness? |
| Open each workout | Is the type right (*Traditional Strength Training*, *Cycling*) or generic (*Other*)? Is the duration right? |
| **Walking + Running Distance**, **Cycling Distance**, **Active Energy** | Is Mi Fitness a source for each? |
| Timing | How long after finishing did the data appear? Did it need Mi Fitness to be opened? |

**One setting to change regardless**

7. Steps → Data Sources & Access → **Edit** → drag *Mi Fitness* above *iPhone*. Apple
   devices rank above third-party apps by default. With the watch first, the watch (worn
   all day) is the primary step source and the iPhone fills gaps.

The type labels shown for a strength session, a cycling session, a walk and a run are the
most useful of these to send me; the rest is optional.

## 4. HealthKit design

### What the companion reads

| Data | Query | Why this query |
|---|---|---|
| Daily steps | `HKStatisticsCollectionQuery`, `.cumulativeSum`, one-day buckets | Returns HealthKit's **merged** total: overlapping samples from the iPhone and Mi Fitness are resolved by source priority. It is the number the Health app shows. |
| Distances, active energy | Same, per type | Same reason |
| Workouts | `HKAnchoredObjectQuery` on the workout type | Returns new workouts **and deletions** since the last anchor |

Summing raw step samples would count the phone and the watch twice. The companion never
does that.

### Rules applied on the device

- **Hand-entered data is excluded.** Queries filter out samples carrying
  `HKMetadataKeyWasUserEntered`, so typing 20,000 steps into the Health app earns nothing.
  Hand-entered workouts are sent flagged, stored, and not rewarded.
- **Read-only.** The companion requests read access only and never writes to HealthKit.
- **Minimum scope.** Steps, walking + running distance, cycling distance, workouts, and
  optionally active energy. Nothing else is requested.
- **No reward logic.** The device sends facts. The server decides what they are worth.

### Two things HealthKit does not offer

1. **Walking distance and running distance are one type.** HealthKit has
   `distanceWalkingRunning`, not two separate quantities. Walk2AI derives:
   *running distance* = sum of distances of running workouts;
   *walking distance* = walking + running total − running distance.
   If running workouts are not written to Apple Health, running distance is unavailable
   and the combined figure is shown instead, labelled honestly.
2. **An app cannot tell whether read permission was denied.** Apple hides this by design:
   a denied read looks exactly like "no data". The companion therefore cannot detect
   revocation directly. It infers it — queries that previously returned data now return
   nothing — and tells you to check Health → Apps → Walk2AI. The backend independently
   shows "last synced" so a silent stop is visible in the web app.

## 5. Background synchronisation: what to expect

| Mechanism | Behaviour |
|---|---|
| `HKObserverQuery` + `enableBackgroundDelivery` | iOS wakes the app when new data lands. Requires the HealthKit Background Delivery entitlement. For step count the system enforces **at most hourly** delivery. |
| Locked phone | The HealthKit store is encrypted while the device is locked. Reads fail until it is unlocked, so deliveries effectively wait for the next unlock. |
| Background budget | Deliveries are budgeted by iOS and are not guaranteed. A force-quit app may stop receiving them until it is opened again. |
| `BGAppRefreshTask` | A second, opportunistic trigger. iOS chooses the time. |
| App open / foreground | Always syncs immediately. This is the reliable path. |
| Simulator | Background delivery does not work there. Testing needs the physical iPhone. |

Realistic expectation: credits typically appear within an hour or two of activity, and
immediately when you open either app. Truly real-time credit is not achievable on iOS and
the product should not promise it. The watch → Mi Fitness hop is usually the slower one.

## 6. Synchronisation protocol

Every sync, whatever triggered it, does the same thing:

1. Query daily totals for a **rolling window** — the last 7 days, including today.
2. Query workouts changed or deleted since the stored anchor.
3. `POST /fitness/sync` with a fresh `batch_id` (see [API.md](API.md#post-fitnesssync)).
4. On success, persist the new anchor and the sync time. On failure, keep the old anchor
   and retry the same batch with the same `batch_id` (exponential backoff).

Why a rolling window instead of "only what changed": late watch data, HealthKit
re-merging sources and edits in the Health app all change *past* days. Re-sending a week
of daily totals is a few hundred bytes, and because the server upserts by day and pays
only the difference, re-sending is always safe.

## 7. Server-side integrity

| Requirement | Mechanism |
|---|---|
| Source identification | `source` on every record; origin app and device stored for workouts |
| Idempotent sync | `UNIQUE (user_id, client_batch_id)`; a replayed batch returns the stored result |
| No duplicate days | `UNIQUE (user_id, activity_date, source)`; latest values overwrite |
| No duplicate workouts | `UNIQUE (user_id, source, external_id)` on the HealthKit UUID |
| Cross-source duplicates | Same category, start within 2 minutes, duration within 10% → the later arrival is linked via `duplicate_of` and not rewarded |
| Overlapping workouts | Eligible minutes are bounded by the union of the intervals, so no minute counts twice |
| Workouts across midnight | Split between days by the user's timezone |
| Manual exclusion | `excluded_from_rewards` with a reason, via `PATCH /fitness/workouts/{id}` |
| Daily aggregation | Server computes `active_minutes` from stored workouts; never accepts it from the device |
| Late arrivals | Each affected day is recalculated under its own cap and rules, within `REWARD_WINDOW_DAYS` |
| Never rewarded twice | The reward engine pays `entitled − already awarded` per day ([ARCHITECTURE.md](ARCHITECTURE.md#6-reward-engine)) |
| Plausibility | Bounded steps per day, bounded workout duration, no future dates |
| Test data | `source = 'test'`, forced `is_test_data`, disabled in production |

## 8. Other sources: Strava and beyond

Apple Health is the source today. The ingestion pipeline (Phase 2) is designed so that
another source can be added without touching validation, de-duplication, the reward
engine or the wallet:

| Piece | What adding a source involves |
|---|---|
| Source identity | A new member of `FitnessSource` and a one-line migration extending the `CHECK` constraints |
| Adapter | Code that fetches from the source's **official** API and produces the same normalised day and workout records the HealthKit sync produces |
| Canonical-source rule | A day is paid from one source only, chosen by a configured priority, so two sources reporting the same day are never summed |
| Cross-source duplicates | Covered by the duplicate rule in section 7: same category, start within 2 minutes, similar duration → one is linked as a duplicate and not rewarded |

Strava is the natural second source: Mi Fitness can push workouts to it, and it has an
official OAuth API that a server can call directly — no phone app required. That also
makes it a possible route for *workouts* if the native companion proves impractical
(section 9), though not for daily step totals, which Strava does not carry.

Fallbacks if the Apple Health path ever stops delivering workouts:

| Option | How | Trade-off |
|---|---|---|
| **A. Relay through Strava** | Mi Fitness → Strava → Apple Health, read from HealthKit unchanged; or Strava read directly by the backend. | One more account; strength sessions must survive the relay. |
| **B. Manual workout log in Walk2AI** | Enter type and duration in the web app. Stored as `source = manual`, unverified. | Not verified activity. Off by default; if enabled, it gets its own explicit reward rule so the distinction stays visible in the ledger. |
| **C. Steps only** | Run the platform on step rewards alone. | Simplest. Strength and cycling earn nothing. |

Not recommended: calling Xiaomi's private cloud API. It is undocumented and
reverse-engineered, which contradicts the project's rule of using official interfaces
only, and it would break without notice.

## 9. Building the iOS companion without a Mac

You work on Windows, do not own a Mac, and have asked for no Apple hardware and no paid
Apple Developer Program. That does not block Phases 1–4:

- The backend validates everything a device sends, so the whole fitness pipeline —
  sync, validation, de-duplication, rewards — is built and tested in Phase 2 with
  clearly-labelled test data.
- Device pairing and scoped device tokens already exist (Phase 1), so the companion's
  server-side contract is fixed before the companion is written.
- The companion's source is kept ready to compile on macOS. It is not built, signed or
  deployed until you choose how.

**Before Phase 5** I will investigate and report, without spending anything: what a free
Apple account can and cannot sign, what can be built without owning a Mac and at what
cost, what can only be tested on the phone, and which official-API alternatives to a
native app exist (see [ROADMAP.md](ROADMAP.md#before-phase-5--feasibility-investigation)).

What is known today about the native route:

| Requirement | Detail |
|---|---|
| macOS with a current Xcode | iOS apps cannot be compiled on Windows or Linux. Whether a hosted macOS build service is a workable substitute is part of the investigation. |
| Apple Developer Program | US$99 per year. Reports from other projects indicate a free "Personal Team" cannot sign the HealthKit entitlement, and free builds expire after 7 days regardless. To be verified in the investigation; **nothing is purchased without your approval.** |
| The physical iPhone | HealthKit data and background delivery cannot be tested in the Simulator. |
| HTTPS backend | iOS refuses plain HTTP by default. A real domain and certificate are needed before the phone can sync to a deployed backend (Phase 6); local testing uses the Mac's address with a development exception. |

No App Store review is needed for personal use: the app is installed directly from Xcode
or through TestFlight internal testing.

## 10. Sources

Research performed 2026-11-13. Findings marked "unknown" above stay unknown until the
device check is done.

- Xiaomi integration overview (steps, heart rate, sleep, weight via Mi Fitness; varies by model): <https://sahha.ai/integrations/xiaomi/>
- Third-party setup guide claiming steps, heart rate, sleep and workouts: <https://proapp.uk/xiaomi-apple-health>
- User report — only steps and calories synced, workouts and sleep did not; band + phone duplication fixed by source priority: <https://discussions.apple.com/thread/252660101>
- Xiaomi support — enabling Health sync under Profile → Third-party data (search summary only; page not reachable from this environment): <https://www.mi.com/global/support/faq/details/KA-517375/>
- Xiaomi support — re-authorising Health access; Low Power Mode blocks sync (search summary only): <https://www.mi.com/global/support/faq/details/KA-230360/>
- Mi Fitness on the App Store: <https://apps.apple.com/app/id1493500777>
- Apple — `enableBackgroundDelivery` (entitlement; hourly limit for step count): <https://developer.apple.com/documentation/healthkit/hkhealthstore/enablebackgrounddelivery(for:frequency:withcompletion:)>
- Apple — executing observer queries: <https://developer.apple.com/documentation/healthkit/executing-observer-queries>
- Apple Developer Forums — HealthKit unavailable while the device is locked: <https://developer.apple.com/forums/thread/730202>
- Apple Developer Forums — background delivery after force-quit: <https://developer.apple.com/forums/thread/803365>
- Apple Support Communities — source priority and step totals: <https://discussions.apple.com/thread/254858828>
- Source priority walkthrough: <https://www.myhealthyapple.com/how-do-i-set-device-priority-for-calculating-steps-and-other-activity-on-iphone-and-apple-watch>
- Projects reporting that HealthKit needs the paid developer programme: <https://github.com/owen282000/life-dashboard-companion-app-ios/pull/90>, <https://github.com/ArturHI/HoopPro/pull/8>
