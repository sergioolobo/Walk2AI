# Walk2AI — Fitness Integration

> Status: **Phase 0 research and design.** The pipeline has one unverified link that
> only your own devices can confirm. Please run the
> [10-minute device check](#3-device-check-please-run-this-first) before Phase 2 is
> finalised — its result decides how workout rewards are implemented.

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

I could not confirm the Watch S3's exact behaviour from public sources. Xiaomi does not
publish a per-device list, third-party sources disagree, and none mention the Watch S3 by
name. Xiaomi's own support pages could not be opened from this environment (blocked by
the session's network policy), so they are cited from search summaries only.

| Metric Walk2AI needs | HealthKit type | Does Mi Fitness write it? | Confidence | If it does not |
|---|---|---|---|---|
| Daily steps | `stepCount` | Yes | **High** — consistent across sources | iPhone's own step count still works (phone must be carried) |
| Walking + running distance | `distanceWalkingRunning` | Probably | Medium — not explicitly confirmed | iPhone records it too; display-only metric, does not affect rewards |
| Active energy | `activeEnergyBurned` | Probably | Medium — reported for older bands | Optional metric; omit |
| Workouts (type, start, end, duration) | `HKWorkout` | **Unknown** | **Low** — one guide says yes; user reports say missing or unreliable | See [fallbacks](#8-if-workouts-do-not-reach-apple-health) |
| Strength training | `HKWorkout` (`traditionalStrengthTraining`) | Unknown | Low | Depends on the row above, and on Mi Fitness using the right type |
| Cycling | `HKWorkout` (`cycling`) + `distanceCycling` | Unknown | Low | As above |

What this means for the reward formula `WC = min(1500, 0.1 × S + 5 × M)`:

- **`S` (steps) is safe.** The core of the product works with what is already confirmed.
- **`M` (workout minutes) is at risk.** Strength training and cycling rewards depend
  entirely on Mi Fitness writing workouts to Apple Health with usable types. This is the
  single largest risk in the project, and it costs ten minutes to resolve.

## 3. Device check (please run this first)

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

Send me the answers (screenshots are ideal) and I will finalise the workout design.

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

## 8. If workouts do not reach Apple Health

In order of preference:

| Option | How | Trade-off |
|---|---|---|
| **A. Relay through Strava** | Mi Fitness can push workouts to Strava; Strava can write workouts to Apple Health. Walk2AI then reads them from HealthKit unchanged. | One more hop and account; must confirm strength sessions survive the relay. No code change in Walk2AI. |
| **B. Manual workout log in Walk2AI** | Enter type and duration in the web app. Stored as `source = manual`, unverified. | Not verified activity. Off by default; if enabled, it gets its own explicit reward rule so the distinction stays visible in the ledger. |
| **C. Steps only** | Run the platform on step rewards alone. | Simplest. Strength and cycling earn nothing. |

Not recommended: calling Xiaomi's private cloud API. It is undocumented and
reverse-engineered, which contradicts the project's rule of using official interfaces
only, and it would break without notice.

## 9. What building the iOS app requires

| Requirement | Detail |
|---|---|
| A Mac with a current Xcode | iOS apps cannot be built on Linux or in this cloud environment. I can write the Swift code here, but it must be compiled and run on a Mac. |
| Apple Developer Program | US$99 per year. Reports from other projects indicate a free "Personal Team" cannot sign the HealthKit entitlement, and free builds expire after 7 days regardless — unworkable for an app that must run daily. **This is a cost; I will ask before it is needed.** |
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
