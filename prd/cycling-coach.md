---
title: 'AI Cycling Coach (Forma)'
slug: 'cycling-coach'
scope: product
status: resolved
parent: null
children: ['cycling-coach/indoor-training-sessions.md', 'cycling-coach/memory-layer.md', 'cycling-coach/multi-user-auth.md', 'cycling-coach/conversational-onboarding.md']
created: 2026-04-13
updated: 2026-06-09
resolution: 8/8
version: 0.3
prototype: 'https://frontend-iota-seven-6je7r6040e.vercel.app/dashboard'
---

# Forma — AI Cycling Coach

> **v0.2** — Working draft, intended as a brief for Claude Code. Sections are sequenced from "why" to "what" to "how". Anything marked **OPEN** is a decision still to be made. The three pillars in §2 are non-negotiable; everything else is in service of them.

## Problem

Competitive amateur cyclists already have hardware, data, and a vague training plan. What they don't have, and can't afford, is a relationship with a coach who:

1. Builds a plan from *their* goal, life, history and context.
2. Adapts that plan continuously as life and form diverge from the model.
3. Walks them through each session indoors with live coaching.
4. Remembers what was said, what worked, what didn't, what hurt, what scared them.
5. Coaches their head as well as their legs — fear of the descent before a sportive, race-day nerves, motivation slumps in week 8 of a long block.

Existing tools each solve one slice. Nobody solves the whole relationship.

- **TrainingPeaks** — calendar and analytics layer. No coach. The coach is a human you pay separately, and most amateurs can't afford one.
- **Intervals.icu** — brilliant analytics, zero coaching, no plan generation, no companion.
- **TrainerRoad / Adaptive Training** — strong workout library and AT plan adaptation, but the personalisation is statistical, not relational. It does not know your life, your race history, your psychology, or your context. There is no one to talk to.
- **Join / Humango** — closer; AI plan generation exists. But the experience is "set the plan and follow it". There is no sustained companion. No memory of who you are across months. No mindset coaching.
- **Strava** — a social feed and ride store. Not a coach.
- **Strava's official MCP** — **LAUNCHED 1 June 2026 (confirmed live).** A remote, OAuth, **read-only** MCP connector, **Claude-only** at launch, **Strava-subscriber-only**, that lets users query their own activity data conversationally (per-second HR/pace/power streams, GPS, clubs/events). **Verified limitations (June 2026):** it *cannot* generate or adapt training plans, holds *no* persistent cross-session memory, and *cannot* control a trainer or guide a live workout. Strava's VP of Partnerships frames it as *"more ways to analyze their own training data"* — i.e. **analytics-as-a-service, not coaching.** Strava has publicly planted its flag in the *analysis* lane and stayed out of the *coaching/relationship* lane Forma occupies. **Strategic implications:** (1) it *validates* the AI-on-cycling thesis and educates millions of athletes to expect AI on their data — free top-of-funnel for a real coach product; (2) it **commoditises Forma's cheapest surface** (conversational metric explanation) — *do not compete there*; (3) it starts a **~12–18 month clock** — the death-warrant scenario is Strava (or TrainingPeaks / TrainerRoad) later adding write + plans + memory, so Forma's defensible moat (Pillar 2 memory) and loop (Pillar 3 execution) must be *real* before then. Forma keeps **free-Strava-account ingestion via direct API** (positioning advantage: *"Free Strava + Forma — that's all you need"*); adopting Strava's subscriber-gated MCP as an optional power-user data path is a *post-beta nice-to-have, not a now*. Positioning line to lead with: ***"Strava + Claude tells you what you did. Forma tells you what to do next, remembers it, and rides with you while you do it."***

## Vision

**One sentence:** Forma is a world-tour-level cycling coach in your pocket — a sport scientist and mindset coach combined, with persistent memory of every ride, goal, and conversation, who builds and adapts your training plan to *your* life and walks every kilometre with you, indoors and out.

**Why it exists.** Tadej Pogačar has a coach, a sports scientist, a nutritionist, a psychologist and a directeur sportif building his season around him. Competitive amateurs have a Garmin, a Strava subscription and a generic plan written for somebody who isn't them. Forma closes that gap. Not a plan-you-adapt-to. A coach who adapts to you.

**Wedge:** the *coach* is the product. The plan, the analytics, the ride execution, the race reports — those are the *evidence* of the coach. Every screen and every interaction must feel like one continuous relationship with one coach who knows you.

**Positioning evolution (June 2026):** Forma is a **life coach and a cycling coach** — because the
life *is* the training context. The memory graph (Pillar 2, v2) models the whole person — values,
goals, gaps, habits, people, life events, mind and body — and renders it back to the user as
**"Your Brain"**: a living, visual memory organ that grows with the relationship (see
`cycling-coach/memory-layer.md`). The product is therefore three things fused: **a brain** (your
connected life), **a training system** (plans, rides, analytics), and **a companion** (Forma, who
reads both). Strava/TrainerRoad can never draw this page; it is the moat made visible.

**Coach identity — the coach *is* Forma, and Forma adapts.** The brand and the coach are one: everyone's coach is **Forma**. We do not ship a separate named persona that "works for Forma" — that would trade away the brand equity of a single shared name (word of mouth is *"Forma flagged my decoupling"*, not *"my coach Dave"*) for a personalisation that riders don't actually feel day to day. What the rider *feels* is not the name; it is the manner, the voice, and the face. So those are what we let them shape:

- **Manner (tone)** — how Forma talks. Presets (`coach_tone`): Balanced, Empathetic, Stoic, Direct, Analytical, Playful. This is the primary personalisation and the one that changes the daily experience.
- **Voice** — the voice in your ear on the turbo (`coach_voice_id`). This is where masculine / feminine / accent live.
- **Face** — an avatar chosen from a gallery (`coach_avatar`).
- **Name** — defaults to **Forma** and stays Forma in all marketing and copy. An **optional** "call your coach something else" setting exists for riders who want that intimacy, but it is **off by default** and never surfaced as a required onboarding step.

**Decouple gender from personality.** Tone, voice and face are independent axes — "feminine and stoic" and "masculine and encouraging" are both valid. Never bundle a gender with a manner; that halves the expressive range and bakes in a stereotype. Gender expression is carried by the chosen *voice and avatar*, not by the tone preset.

**Brand.** The product brand is **FORMA** (previously open, now decided). The coach name and the product brand are deliberately the *same* — that is the point.

### The three pillars (non-negotiable)

These are the three things that, if they don't work, the product doesn't exist. Every feature ships in service of one or more.

#### Pillar 1 — Forma is everywhere, and Forma is one coach

Forma is not a chat tab. Forma is the operating layer of the product. He shows up on the Dashboard, in every ride detail, on the Performance page reading the trends, on the Goals page interpreting the route, in Training writing the next block, and in the live ride session pacing you through the workout.

The same persona, the same memory, the same voice, in every surface.

#### Pillar 2 — Persistent, layered memory

Forma remembers. Every ride, every conversation, every goal, every race report, every off-hand comment ("I've got a wedding in June, no Saturday ride that week"), every PR, every illness, every bike, every life event. This is the moat. No competitor can replicate years of relational context.

Memory is the engine that makes Pillar 1 feel human and Pillar 3 feel coached rather than scripted.

#### Pillar 3 — Forma rides with you. Indoor first.

Forma is not just an analytics layer that reads your Strava after the fact. The training plan is rideable *inside the app*, on a turbo trainer (Wahoo Kickr Core first), with live power, heart rate and cadence, ERG control, in-ride coaching cues, and a complete ride record written back to memory the moment the cooldown ends.

**v1: Desktop Chrome only** (Web Bluetooth limitation). **v1.1: Native iOS companion** (Capacitor wrapper) extends Pillar 3 to mobile. Outdoor live ride support follows in v2.

If a user can't do a session inside Forma — even on one supported device class — Forma isn't a coach yet. He's a critic.

## Users

### Primary — "The Working Cat 3"

- 35–50, cycles 6–10 hrs/week, full-time job, partner, often kids.
- Owns a power meter and a smart trainer (Wahoo Kickr / Tacx Neo / Wahoo Kickr Core).
- Does 1–4 A-races a year — sportives, gran fondos, regional road races, Cat 3 / Cat 2 amateur scene, 100–400 km events.
- Has used TrainingPeaks, Intervals.icu, TrainerRoad, Strava — has FTP literacy, knows what TSS is, knows what a build block is.
- Cannot justify £200–£500/month for a real coach. Has tried generic plans and felt them not fit their life.
- Pain: every Sunday they wonder *"is what I did this week the right thing for the goal in 12 weeks?"* and there is no one to answer.

### Secondary — "The Aspirational Cat 1/2"

- 25–40, racing seriously at amateur elite, 12–18 hrs/week.
- Wants the coaching infrastructure of a pro without the team.
- Higher data literacy. Will scrutinise the plan and push back. Forma needs to defend its choices.

### Tertiary (later) — "The Returning Athlete"

- Used to race, has come back after 5–10 years off, weight up, FTP down.
- Needs a coach to *re-learn* them. This is a memory-heavy persona.

### Anti-users (out of scope)

- Pure recreational cyclists with no power, no goals, no data.
- Multi-sport athletes (triathletes / runners). May come later.
- Junior athletes (under 18) — coaching minors carries different duty of care.

## Core Capabilities

### 1. Conversational onboarding (Forma's first contact)

Voice + text conversation that captures goals, history, life context, FTP, equipment, injuries — feels like meeting a coach, not filling a form. Generates the first plan as the closing act of onboarding. **Feature PRD:** `cycling-coach/conversational-onboarding.md`.

### 2. Adaptive training plans

Goal-driven, periodized plans that respond to what actually happens — not just what was planned. Multi-goal awareness. A/B/C race priority. Adaptive to fitness, life, missed sessions. Workout types span endurance, tempo, sweet spot, threshold, VO2max, sprint, recovery. Export to ZWO/ERG/MRC/FIT. **Status: largely BUILT.**

### 3. Omnipresent Coach Forma (Pillar 1)

Forma appears on every surface — Dashboard, Rides, Performance, Goals, Training, Workout Detail, in-ride. Same persona, same voice, same memory. Proactive nudges, post-ride debriefs, in-context metric explanations, chat conversations, voice mode. **Status: chat + voice BUILT; cross-surface presence in M2.**

### 4. Layered persistent memory (Pillar 2)

Five-layer store (structured state, event log, episodic facts, semantic profile, coach arc). Hide-from-view but retain-in-background editability. **Epic PRD:** `cycling-coach/memory-layer.md`.

### 5. In-app indoor ride execution (Pillar 3)

Web Bluetooth (FTMS) trainer pairing on desktop Chrome → live workout player → ERG control → in-ride voice cues → completed-ride record + Forma debrief. **Epic PRD:** `cycling-coach/indoor-training-sessions.md`.

### 6. Performance analytics, narrated by Forma

PMC (CTL/ATL/TSB), power profile, rider profiling (sprinter/climber/TT/all-rounder), zone distribution, race projection, FTP history. All numbers paired with Forma's interpretation in plain English. **Status: charts BUILT; Forma interpretation layer in M2.**

### 7. Race execution support

Race reports, post-race needs assessments, race-day pacing strategies, segment-level pacing on goals with GPX. **Status: race report flow BUILT; race-day mode in v2.**

### 8. Multi-user auth & data isolation

Email + password + OAuth (Google, Apple), per-user data isolation via Postgres RLS, session/refresh-token rotation, GDPR-compliant deletion. **Epic PRD:** `cycling-coach/multi-user-auth.md`.

## Boundaries

### In scope, v1 (Now)

- Cycling, indoor + outdoor (analytics on outdoor; in-app execution indoor only).
- Single-rider use (one human, one account).
- Strava ingestion + Dropbox FIT import + manual FIT upload. **A free Strava account is sufficient** — users do not need a paid Strava subscription. The endpoints we use (athlete, activities, streams, detailed activity) are available to free Strava accounts. The paid-Strava requirement in the developer program applies only to *our* developer-account registration, not to end users.
- Goal definition (event date, type, route via GPX, A/B/C tag, history notes).
- AI plan generation tied to goal + availability + history.
- Workout detail with targets (power, cadence, duration, zones).
- In-app ride execution on a smart trainer (Wahoo Kickr Core first), desktop Chrome, with ERG control, live telemetry, in-ride coaching prompts, auto-saved ride record. (Pillar 3.)
- Persistent multi-layer memory (Pillar 2).
- Coach Forma persona present on every screen (Pillar 1) — text + voice.
- Conversational voice/text onboarding.
- Performance analytics: PMC, FTP tracking, power curve, weekly load, ride list with TSS/IF/NP.
- Race reports / post-race needs assessments.
- Multi-user auth + secure account/data model.

### In scope, v1.1 (Next)

- **Native iOS companion (Capacitor wrapper)** — extends Pillar 3 to mobile. Trigger: ≥5 closed-beta users request it unprompted.
- Outdoor live ride support (mobile companion: in-ride voice cue, post-ride debrief on save).
- Health-data ingestion: Garmin Connect / Apple Health / Whoop (sleep, RHR, HRV, body battery).
- Coach proactivity — Forma messages first ("you said you had a bad sleep — let's pull tomorrow back to Z2").
- Beta program tooling (invites, feedback capture).

### In scope, v2 (Later)

- Multi-bike profile, indoor/outdoor power offset.
- Nutrition coaching (race-day fuelling plans tied to event GPX).
- Group / club mode (read-only — coach-of-friends sharing).
- Multi-sport (run / swim) — only after cycling retention hits target.
- Native Android companion.

### Explicitly out of scope (v1 and v1.1)

- Coaching minors.
- Medical advice (injury rehab is referred out, never prescribed).
- Real-time live ride video / Zwift-style world.
- Social feed.
- E-commerce (gear store, affiliate links).

## Success Criteria

### North star (operational)

**In-app ride share ≥ 40% by month 3 of a user's life.** This is the proof that Pillar 3 is real, which is the proof that the whole "rides with you" story works, which is the leading indicator for relationship quality, which trails into retention. Read it weekly.

### North star (strategic)

**18-month subscription retention ≥ 60%.** Cannot be measured for 18+ months but is the long-term proof the coach relationship is real. If users drop after 6 or 12 months, we've built a tool, not a coach.

### Supporting metrics

- **Performance proof.** Median TSS-per-week increased while maintaining freshness band (TSB ≥ -25), normalised by cohort starting fitness. Avoids vanity-metric trap of "FTP improvement %" that flatters returning riders and disadvantages near-ceiling racers.
- **Plan adherence.** Median % of planned sessions completed (any version — original or adapted). Target: ≥ 75%.
- **Coach engagement.** Median Forma conversations per active user per week. Target: ≥ 2 (operational), ≥ 3 aspirational once Pillar 1 is fully shipped. (One conversation = a multi-turn session, ~6 turns median.)
- **Voice usage.** % of conversations that include voice (mic) input. Target: ≥ 25%.
- **Memory recall quality.** Internal eval — when Forma is asked "what was my best 20-minute power last summer?" or "remind me what I said about my knee in March", he answers correctly. Target: ≥ 90%.
- **Referral rate.** Users acquired via existing-user referral. Target: ≥ 20% of new sign-ups by month 6 of public launch.

### Commercial guardrails

- **Gross margin ≥ 60%** after Anthropic + ElevenLabs + hosting + Strava per active user. (See cost model below.)
- **CAC payback ≤ 9 months.**

### Cost model

**Definitions:**
- A **conversation** = a multi-turn session opened from any surface. Median = 6 turns.
- A **Forma call** = one billable LLM invocation (one turn, one debrief, one extraction, etc.).

**Per-active-user monthly Forma workload (target engagement: 2 convs/week):**

| Activity | Volume | Model | Approx cost/call | Monthly cost |
|---|---|---|---|---|
| Conversation turns | 52 | Haiku 4.5 | $0.004 | $0.21 |
| Ride debriefs | 20 | Haiku 4.5 | $0.006 | $0.12 |
| Episodic-fact extraction | 26 | Haiku 4.5 | $0.003 | $0.08 |
| Plan generation/adaptation | 4 | Sonnet 4.5 | $0.09 | $0.36 |
| Weekly profile rewrite | 4 | Sonnet 4.5 | $0.075 | $0.30 |
| **Subtotal Claude** | | | | **~$1.07** |
| ElevenLabs voice synth (25% of turns) | ~13 | Creator tier | — | ~$0.50 |
| Hosting + Strava + Postgres (amortised at 100 users) | — | — | — | ~$0.30 |
| **Total per active user / month** | | | | **~$1.87** |

**Subscription target: £19.99/month (annual £180).** Annual must be ≥ 70% of users to keep blended margin healthy.

**No double subscription.** Users keep their existing (free) Strava account. Forma is one £19.99/mo line item — not Forma + Strava Premium + something else. This is a real positioning advantage over TrainingPeaks Premium (~$20/mo, typically stacked on Strava Premium for serious users). Lead marketing copy with this when we launch: *"Free Strava + Forma. That's all you need."*

**Headroom:** ~92% gross margin at target engagement. Massive cushion.

**Hard alerting threshold: $8/user/month.** If real usage hits this, escalate to Pro tier or investigate runaway loop.

**Cost-control mechanisms:**
- Tiered model routing: Haiku for fact extraction, ride debriefs, simple chat. Sonnet for plan generation, weekly profile rewrite, race reports. Opus only for explicit user-requested deep analysis.
- **Prompt caching from day 1** — `cache_control` on stable system-prompt + memory-profile prefix. Expected to cut Claude subtotal another 30-40% on cache hits.
- Hard per-user monthly token budget. Soft-cap at 80%, hard-cap at 100% with graceful "you've used your conversation quota this month" — heavy users escalate to Pro tier.
- Voice: cap voice synthesis at N seconds per session.

## Open Questions

- **OQ1:** Final pricing — £19.99/mo? £14.99/mo with annual at £150? Founding-member rate for closed beta?
- **OQ2:** Voice gallery — Forma's voice is picked from a small gallery during onboarding (voice carries gender/accent, decoupled from tone and name). Need to lock: (1) the **default house voice** heard before any pick, and (2) a starter gallery of ~3-4 options spanning registers. The warm female voice `NbkKnEAZ7Bqw4EAkVEaz` is already auditioned and is a strong candidate for the default or a gallery slot; audition a masculine-register option and one accent option that pair tonally. Pick the default before conversational onboarding ships.
- **OQ3:** Native iOS trigger — confirmed at "≥5 closed-beta users request it unprompted." Re-evaluate at Milestone 2 review.
- **OQ4:** What does "rider profile" classify on? (Today: All-Rounder. Taxonomy matters because Forma coaches differently per type.)
- **OQ5:** Health-data ingestion priority — Whoop first or Garmin Connect first? Depends on the beta cohort.
- **OQ6:** Multi-coach personas — future "Forma voiced as Cav's coach" / "as a sport psychologist"? Park.
- **OQ7:** Race-day mode — separate spec or part of in-ride?
- **OQ8:** Memory limit per user — soft cap on episodic-fact count, age-out policy. Address in memory epic.
- **OQ9:** Strava API rate-limit budget at 100 active users — model and cache strategy.
- **OQ10:** Coaching liability insurance — required pre-public-launch.

## Epics

Sequenced by dependency and user impact. The architecture below uses `forma-core` as the single funnel for all Forma calls — it must exist (at least as a thin wrapper) before any other epic ships features that talk to Claude.

### Epic A — Indoor Training Sessions (Milestone 1)

**Status:** in scope, v1. **Detail:** `cycling-coach/indoor-training-sessions.md`

**Outcome:** Gareth (and shortly: closed-beta) can pick a planned session, pair Wahoo Kickr Core, ride the workout inside the app on desktop Chrome, and have the result land in ride history with a Forma-written debrief written into memory.

### Epic B — Memory Layer (Milestone 1 skeleton, Milestone 2 full)

**Status:** in scope, v1. **Detail:** `cycling-coach/memory-layer.md`

**Outcome:** Five-layer memory store, retrieval API, hide-from-view editability, audit log. Forma's prompts route through `forma-core` and consume memory automatically.

### Epic C — Coach Presence (Milestone 2)

**Status:** in scope, v1. *(Existing focus from v0.1 — formal PRD to follow.)*

**Outcome:** Forma surfaces on Dashboard, Rides, Performance, Goals, Training, Workout Detail. Proactive nudges. Post-ride debriefs. In-context metric explanations. Powered by memory layer + `forma-core`.

### Epic D — Multi-User & Auth (Milestone 3 gate)

**Status:** in scope, v1. **Detail:** `cycling-coach/multi-user-auth.md`

**Outcome:** Email/password + OAuth, per-user RLS, refresh-token rotation, GDPR deletion, intermediate "secret-link, single-shared-instance" mode for early beta dogfooding before full multi-tenant.

**Child feature:** Conversational Onboarding (`cycling-coach/conversational-onboarding.md`).

### Epic E — Native iOS Companion (Milestone 2 / v1.1)

**Status:** in scope, v1.1. *(PRD to follow when triggered.)*

**Outcome:** Capacitor-wrapped iOS app reusing the Next.js codebase + native BLE plugin, distributed via TestFlight. Cost: $99/yr Apple Dev Program + ~2 weeks dev time.

**Trigger:** ≥5 closed-beta users request mobile in-ride support unprompted.

### Epic F — Brand, UX & Design System (pre-public-launch)

**Status:** pre-launch blocker. *(PRD to follow.)*

**Outcome:** Product naming finalised, domain acquired, logo + colour + typography + tone of voice locked, design system, marketing site, App Store / PWA metadata.

**Depends on:** Epic D.

### Epic G — Growth & Billing (pre-public-launch)

**Status:** pre-launch blocker. *(PRD to follow.)*

**Outcome:** Stripe subscriptions, pricing page, trial, usage analytics with per-user AI cost tracking, referral mechanism, churn detection with Forma re-engagement.

**Depends on:** Epics D + F.

---

## Appendix A — Current state inventory (5 May 2026)

This is what exists today, walked from the live URL.

### Working

| Surface | State |
|---|---|
| Auth (Gareth-only) | functional |
| Strava OAuth + activity ingestion | connected, 5,760 / 6,243 activities imported |
| Dropbox FIT-file import | connector wired |
| Manual FIT upload | button present |
| Dashboard | shows CTL/ATL/TSB/FTP, Forma message, rider profile, race-report nudge |
| Rides list | duration/distance/avg power/NP/TSS/IF |
| Performance page | tiles populate (CTL 25, ATL 34, TSB -9, weekly TSS); PMC chart renders empty axes — likely query/render bug |
| Goals | event date, GPX, A/B/C race tag, countdown, post-race needs-assessment, race report flow |
| Training (plan view) | active plan tied to "Manchester to London 2026", week view, planned vs done, plan phases stub |
| Workout detail | workout profile graphic, steps, target watts/cadence |
| Start Session → device pairing | Web Bluetooth chooser opens on desktop Chrome; the connect path itself runs |
| AI Coach | Forma persona, chat history sidebar, voice mic, suggested prompts |
| Settings | profile, FTP test, weekly hours, hard/easy/rest day calendar, Strava status |

### Broken / incomplete — *reconciled against code audit, 9 June 2026*

> The original 5 May list was stale **in both directions** — it understated Pillar 3 progress and understated the architectural debt. Verified reality below.

1. **In-app ride execution — ~90% WIRED, not broken.** FTMS pairing, live power/HR/cadence parsing, ERG target-power control, 1 Hz telemetry buffering (with localStorage crash-recovery), and ride save all work and are correctly connected. The loop breaks at two points only (#2, #3). *(The previous PRD claim that ERG/telemetry/save were unwired was incorrect.)*
2. **Post-ride debrief never fires** — `_generate_debrief_bg` `await`s a *synchronous* `generate_ride_debrief()`; the resulting `TypeError` is swallowed by a bare `except`. Affects every in-app ride **and** every FIT upload. ~1-line fix. **This silently blocks the Milestone 1 exit gate.**
3. **Planned workout never marked complete** after an in-app ride — the ride saves with `workout_id` but `update_workout_status` is never called, so the calendar + weekly compliance stay permanently wrong.
4. **`forma-core` does not exist** — the *non-negotiable* single funnel for all Claude calls is unbuilt; 7 `anthropic.Anthropic()` call sites are scattered across 3 services with hardcoded models. Risk: an inconsistent Forma persona across surfaces (directly undermines Pillar 1).
5. **Persistent memory (Pillar 2) is 0% built** — no `mem_*` tables, no `forma_calls` table, no pgvector, no embeddings, no `get_context()`. "Memory" is flat chat logs + cached debrief/nudge text rebuilt live each call. **The moat does not exist.**
6. **No cost logging / no prompt caching / wrong model routing** — chat *and* ride debrief run on Sonnet where the cost model specifies Haiku; no `cache_control` anywhere (and the system prompt is structurally uncacheable). The ~92%-margin business case is currently **unmeasured**.
7. **Forma presence (Pillar 1) ~45%** — strong on Dashboard, Ride detail, Workout detail, In-ride, and Coach; **absent on Performance and the Training calendar** (the two data hubs) and Rides-list; link-out/passive on Goals.
8. **Performance PMC chart renders empty — FRONTEND only.** Recharts-3 plot-area collapse (double height-bounded container + manual `ticks`). Backend data verified healthy (5,117 PMC rows).
9. **Rider Profile "complete some rides with power data" — FRONTEND only.** Quick-vs-full fitness query race feeds all-zero scores into the radar's `hasData` gate. Backend returns correct non-zero scores.
10. **Multi-user / security NOT ready** — auth is ~40% of Epic D (no email verification, password reset, OAuth, refresh-token rotation, GDPR delete). **CRITICAL: `SECRET_KEY` is the literal default placeholder → anyone can forge any user's JWT.** IDOR via workout link-ride leaks cross-user ride data. No Postgres RLS. Hard gate before a second human logs in.
11. **Voice** — TTS only; **no ASR** (voice *input* is unbuilt). Configured ElevenLabs voice ID does not match the OQ2-locked female ID `NbkKnEAZ7Bqw4EAkVEaz`.
12. **Design system mid-migration** — the "VoiceBox" editorial theme is applied to ~4 surfaces while ~8 remain on the old slate theme; a *second*, divergent "questui" theme is also being explored. Visual fracture + scope-thrash risk.

## Appendix B — Non-functional requirements

### Authentication & accounts (covered in Epic D)

- Email + password with verification. Password reset via email.
- OAuth providers: Google, Apple. Strava OAuth is integration-only, not auth-only.
- Per-user data isolation enforced at DB row level (RLS in Postgres).
- Session: refresh-token rotation, 30-day idle timeout, 90-day absolute.
- Optional MFA (TOTP) — not v1 blocker but pre-public-launch blocker.

### Data retention & privacy

- All ride data and memory layers retained while subscription active + 90 days.
- Account deletion: soft-delete 30 days, hard-delete after.
- **Memory layer C is user-hideable but retained in background** (per Pillar 2 spec). Forma continues to use hidden facts when planning, but does not surface them in user-facing views or quote them verbatim.
- Privacy policy must explicitly state: *"Forma retains coaching-relevant context even after you hide it from view, to keep your training safe. Account deletion permanently removes everything."*
- Safety-relevant categories (injuries, illnesses, surgeries, medications) get a soft warning on hide.
- GDPR / UK GDPR compliant. Data export = JSON archive of all layers + raw FIT files.
- Strava terms compliance (data not redistributed; user is the only consumer of their data).

### Compliance & certifications

**Decision (2026-07-16):** the active compliance target is **UK GDPR + Data Protection Act 2018 plus industry-standard security engineering** — appropriate for a pre-PMF, ~100-user consumer subscription. Formal audited certifications (SOC 2, ISO 27001) are **deferred** — they are procurement-driven attestations for enterprise/B2B sales, cost £15–40k and 6–12 months each, and buy nothing at this stage. This foundation is exactly what a later SOC 2/ISO audit builds on, so none of the GDPR/security work is wasted.

**⚠️ PRE-PUBLIC-LAUNCH REMINDERS (do NOT skip — flagged so we don't forget):**

- **ICO registration + annual fee** (~£40–60) — a UK data controller must register with the Information Commissioner's Office. A form + payment, legally required. *Gareth's action.*
- **Privacy policy live** — public privacy notice covering lawful basis, data-subject rights, health-data (special-category) explicit consent, retention, and breach process. Claude can draft; Gareth reviews/publishes.
- **Cyber Essentials (UK)** — ~£300, self-assessed, government-backed. Cheap early trust signal; a checklist + config hardening we can prepare pre-launch. *Not a blocker, but do it before public launch.*
- **Re-evaluate SOC 2 / ISO 27001** — pursue **only** if a customer, partner, or employer (e.g. Mindvalley) contractually requires an audited certification. Revisit trigger: first enterprise/partner deal or procurement request. Until then, do not spend on it.
- **Postgres row-level security (RLS)** — deferred from the beta build (2026-07-16 decision): beta ships app-layer user filtering + an automated cross-user isolation test suite. Add RLS before public launch so the database refuses cross-user reads by construction (defense-in-depth the isolation audits expect). Pairs with this milestone.

### Strava API budget & Developer Program

- Strava: 100 requests / 15 min / app, 1000 / day / app (Standard Tier).
- Webhook-driven on new ride; bulk backfill rate-limited to fit window.
- Each user's initial 5–10 year backfill is queued, not real-time.

**Developer Program changes (announced May 2026):**

- **Standard Tier** (current) — direct OAuth integration, ≤10 athletes self-serve. Requires the registered developer's Strava account to have an active subscription from 1 June 2026 (existing devs: 30 June 2026). 3-month free promo available via code `a44e571570` if no current subscription.
- **Extended Access Tier** — higher rate limits, greater user capacity, prioritised support, Partner API eligibility, **no developer-account subscription required.** Apply once we hit ≥25 active beta users. This is the long-term lane.
- **Intermediary platform restriction** (1 June 2026) — apps routing data through third-party brokers are no longer supported. **We are compliant:** direct REST calls to `strava.com` from our FastAPI backend, no proxy / no broker. Audited in `app/services/strava_service.py`.
- **June 2027 technical migration** — auth tokens already in `Authorization: Bearer` headers (compliant). Base URL change is one-line: `STRAVA_API_BASE = "https://www.strava.com/api/v3"` → `"https://www.api-v3.strava.com"` in `strava_service.py` line 39. No use of the retiring `oauth/deauthorize` endpoint; should add `oauth/revoke` call to `disconnect()` for clean revocation (security hygiene, not a blocker).

### Reliability

- Uptime target: 99.5% v1 (~3.6 hrs downtime / month). 99.9% pre-public-launch.
- RPO ≤ 24 hrs (daily Postgres backup minimum). RTO ≤ 4 hrs.
- Live-ride session: must continue to work *offline* in browser if connection drops mid-ride. Telemetry buffered locally, synced on reconnect.

### Performance

- Dashboard first contentful paint ≤ 2.0 s on broadband, ≤ 4.0 s on 4G.
- Forma response p95 ≤ 4 s (text), ≤ 6 s (voice synthesis).
- Live-ride telemetry render @ 1 Hz minimum, target 4 Hz from BLE.

### Observability

- Structured logging on every Forma call (model, prompt token count, response token count, latency, retrieval hits, cost in cents).
- Per-user cost dashboard (admin).
- Memory-retrieval audit log.
- Sentry on frontend + backend.

## Appendix C — Technical architecture

### Current stack

- **Frontend:** Next.js (Vercel deploy — `frontend-iota-seven-…vercel.app`). TypeScript + Tailwind.
- **Backend:** FastAPI on Railway.
- **Database:** Postgres on Railway.
- **AI:** Anthropic (Claude). Voice: ElevenLabs (synthesis) + Whisper or ElevenLabs ASR.
- **Integrations:** Strava OAuth + activity webhook + REST. Dropbox.
- **Schema migrations:** Alembic.

### New components required for v1

- **`forma-core` service** — single source of truth for Forma. Wraps Claude calls. Owns prompt construction (memory retrieval + system prompt + user turn). Routes between Haiku/Sonnet/Opus per task. Implements prompt caching. Emits structured cost logs. **Used by every screen — there is no other path to Claude.**
- **`memory` service** — owns layers A–E. Postgres tables for A/B (time series + event log), Postgres + pgvector for C (episodic facts with embeddings), Postgres jsonb for D and E. Provides `get_context(user_id, query, k)` to `forma-core`.
- **`ride-session` service** — runs on the frontend (Web Bluetooth is browser-only). Manages BLE pairing, ERG control, telemetry capture, local buffer. POSTs ride record to backend on completion. Mirrors a thin server-side state for "ride in progress" so Forma can be aware mid-ride.
- **`auth` service** — multi-user, Postgres row-level security, OAuth provider support, refresh-token rotation.
- **`billing` service** — Stripe subscription, quota enforcement, plan tier (Standard / Pro).

### Mobile platform strategy

- **v1:** Desktop Chrome only for in-app rides. Web Bluetooth + FTMS protocol. Dashboard/chat/analytics work on any modern browser.
- **v1.1:** Capacitor-wrapped iOS app reusing the Next.js codebase + `@capacitor-community/bluetooth-le`. Distributed via TestFlight (free for closed beta, no App Store approval needed). Cost: $99/yr Apple Dev Program + ~2 weeks dev time.
- **v2:** Native Android companion (likely also Capacitor) once iOS is validated.

### Known technical debt

- Alembic migration discipline — formalise migration review per merge.
- Test coverage — add e2e tests on the Forma-call path and the ride-session path before public launch. Property-based test on memory retrieval (no PII leak across users).
- Observability — instrument before public launch, not after.

## Appendix D — Roadmap & milestones

Three horizons, no dates. Each milestone has an exit gate — a thing that must be true to move on.

### Milestone 1 — "I can train inside Forma" (Now)

**Outcome:** Gareth can pick a planned session, pair his Wahoo Kickr Core, ride the workout inside the app on desktop Chrome, and have the result land in his ride history with a Forma-written debrief.

**Work:**
- Stand up `forma-core` skeleton (model routing, prompt caching, cost logging).
- Stand up `memory` schema (layers A–E) — retrieval API stub, no full population yet.
- Fix Web Bluetooth ride flow on desktop Chrome end-to-end (pairing → ERG control → telemetry → save).
- Wire ride completion → Forma debrief → write to memory layer C.
- Fix Performance chart render bug.
- Fix Rider Profile "no power data" bug.
- Add in-ride voice cues at warm-up / interval start / cooldown (basic).
- "Secret-link, single-shared-instance" mode so 2-3 close friends can dogfood.

**Exit gate:** Gareth completes 5 consecutive planned sessions inside Forma with no manual intervention, and the debriefs reference at least one prior conversation or ride.

### Milestone 2 — "Forma feels like a coach" (Next)

**Outcome:** Memory layer is real, Forma shows up on every screen, the coaching feels continuous and human. Conversational onboarding live. Native iOS companion shipped to closed beta if trigger met.

**Work:**
- Memory service (layers A–E) fully populated and integrated.
- Forma surfaces on Dashboard, Rides, Performance, Goals, Training, Workout detail.
- "What Forma Knows About Me" settings page (with hide-not-delete UX + safety warnings).
- Voice quality pass on ElevenLabs voice selection + persona prompt (resolve OQ2).
- Conversational onboarding (text + voice) ships.
- Proactive Forma — daily morning message based on yesterday's ride + today's plan + life context.
- Race report flow rewritten to feed memory.
- Native iOS Capacitor wrapper if trigger met.

**Exit gate:** in a blind side-by-side eval, Forma's coaching responses score higher than a generic Claude+RAG baseline on relevance, recall, and *continuity* (does Forma remember the user). Gareth and ≥3 closed-beta users report "this feels like talking to a coach" unprompted.

### Milestone 3 — "Beta with friends" (Later)

**Outcome:** 5–15 cycling friends are using Forma daily. Multi-user, billing, retention data starts.

**Work:**
- Multi-user auth (per Epic D), per-user Strava OAuth.
- Stripe billing, quota enforcement.
- Onboarding flow polished (Strava connect → goal → first plan → first ride) under 10 minutes.
- Beta feedback capture in-app.
- **Apply for Strava Extended Access Tier** once we cross ~25 active beta users — drops the dev-account subscription requirement, lifts rate limits, signals seriousness to Strava. Required before public launch.

**Exit gate:** ≥ 10 paying beta users. Median user has completed ≥ 8 in-app rides. Cost-per-user dashboard live, blended cost ≤ £6/user/month.

### Beyond Milestone 3 — public launch

- Public sign-up, marketing site, pricing page (Epic F + G).
- Health-data integrations (Whoop, Garmin Connect, Apple Health).
- Outdoor live ride.
- Multi-bike profiles.

## Appendix E — Risks

| Risk | Mitigation |
|---|---|
| Claude + ElevenLabs cost exceeds subscription revenue | Tiered model routing, per-user token budget, voice caps, prompt caching, Pro tier for heavy users (cost model above) |
| Web Bluetooth dead-end on mobile blocks Pillar 3 at scale | Native iOS Capacitor companion in v1.1 (Epic E) |
| Strava API rate limit / access policy change | **Actively managed (May 2026):** direct integration audited (compliant with intermediary restriction); token-in-headers already correct; June 2027 base-URL migration is a one-line change tracked in code. Multiple ingestion paths (Strava + Dropbox + manual FIT) as fallback. Extended Access Tier application planned at ≥25 users to lift rate limits. |
| Strava's official MCP becomes "good enough" and users skip the coach | Forma's wedge is *relationship + plan + execution*, not analytics. The MCP answers questions; Forma proposes actions, remembers context, rides with you. Lean marketing positioning into this: *"Strava tells you what you did. Forma tells you what to do next, and rides with you while you do it."* |
| Solo-dev bandwidth | Ruthless scoping per milestone. Claude Code as a force multiplier. No sport extension until cycling retention proven. |
| Memory privacy breach (cross-user PII leak) | RLS at DB layer, retrieval audit log, automated tests asserting cross-user isolation, no shared embedding namespace |
| Anthropic / ElevenLabs pricing or model changes | Provider abstraction in `forma-core` so a model swap is one config change. Hold a 25% margin buffer on cost ceilings. |
| Coaching liability — bad advice causes injury | Forma never gives medical or rehab advice; refers out. Disclaimers on plan generation. Insurance pre-public-launch. Hidden-but-retained injury memory keeps Forma aware even when user hides facts. |
| Differentiation erodes if TrainingPeaks ships its own AI coach | Memory layer is the moat; ship Pillar 2 fast and well. The real defence is *quality* of memory + relational coach voice + tightness of plan-adapt-execute loop. |

## Appendix F — Data model (first pass)

Postgres + pgvector. Indicative — to be hardened in implementation.

```
users (id, email, password_hash, created_at, …)
user_profiles (user_id, full_name, weight, ftp, max_hr, resting_hr, weekly_hours, hard_days)
strava_links (user_id, strava_athlete_id, access_token, refresh_token, expires_at)

goals (id, user_id, name, event_type, race_grade {A,B,C}, event_date, route_gpx_blob, history_notes, status)
plans (id, user_id, goal_id, generated_at, model_version, status)
plan_phases (plan_id, phase_index, name, start_date, end_date, target_focus)
sessions (id, plan_id, scheduled_date, name, description, planned_duration_s, planned_tss, status {planned, completed, skipped})
session_steps (session_id, step_index, type {warmup, interval, recovery, steady, cooldown}, duration_s, target_pct_ftp_lo, target_pct_ftp_hi, target_cadence)

rides (id, user_id, source {strava, dropbox, fit_upload, in_app}, source_id, started_at, duration_s, distance_m, avg_power, np, tss, if, fit_blob)
ride_session_link (ride_id, session_id)  -- actual ↔ planned

devices (id, user_id, device_type {power_meter, hr, smart_trainer, cadence}, ble_id, friendly_name)

-- Memory layers
mem_state_timeseries (user_id, metric, ts, value)               -- Layer A
mem_events (user_id, ts, event_type, payload_jsonb)             -- Layer B
mem_facts (id, user_id, ts_observed, fact_text, embedding,
           source_event_id, category {general, medical, life, performance},
           user_hidden_at, soft_deleted_at)                     -- Layer C
mem_profile (user_id, updated_at, profile_md)                   -- Layer D
mem_arc (user_id, updated_at, arc_md)                            -- Layer E

-- Forma conversations
conversations (id, user_id, started_at, surface {dashboard, coach, rides, …, onboarding})
conversation_turns (id, conversation_id, role {user, forma}, content, tokens_in, tokens_out, model, retrieval_log_id, created_at)

-- Cost / observability
forma_calls (id, user_id, ts, model, tokens_in, tokens_out, cost_cents, latency_ms, surface, retrieval_count, cache_hit)
```

## Appendix G — API surface (first pass)

REST. JWT auth. All endpoints per-user via RLS.

```
POST   /auth/signup
POST   /auth/login
POST   /auth/refresh
POST   /auth/logout

GET    /me
PATCH  /me/profile

POST   /onboarding/start
POST   /onboarding/turn          -- {message, voice?} → {forma_message, voice_audio_url?, complete?}
GET    /onboarding/state         -- profile-being-built

POST   /integrations/strava/connect
DELETE /integrations/strava
POST   /integrations/strava/sync

GET    /goals
POST   /goals
PATCH  /goals/{id}
DELETE /goals/{id}
POST   /goals/{id}/route        -- GPX upload

POST   /plans/generate          -- {goal_id, available_hours_override?}
GET    /plans/active
GET    /plans/{id}
POST   /plans/{id}/adapt        -- triggers a Forma-driven re-plan

GET    /sessions?week=…
GET    /sessions/{id}
POST   /sessions/{id}/start     -- creates an in-app ride container
POST   /sessions/{id}/complete  -- {ride_telemetry blob, fit_file}
POST   /sessions/{id}/skip

GET    /rides?range=…
GET    /rides/{id}
POST   /rides/upload            -- manual FIT
GET    /rides/{id}/debrief      -- Forma-written

GET    /performance/pmc?range=…
GET    /performance/power-curve?range=…
GET    /performance/ftp-history

POST   /coach/converse          -- {surface, message, voice?} → {forma_message, voice_audio_url?}
GET    /coach/conversations
GET    /coach/conversations/{id}
GET    /coach/memory            -- "What Forma Knows About Me"
PATCH  /coach/memory/{fact_id}/hide       -- hide-not-delete
PATCH  /coach/memory/{fact_id}/unhide
PATCH  /coach/memory/{fact_id}            -- edit fact text
DELETE /coach/memory                       -- account-level GDPR erasure only

GET    /billing/subscription
POST   /billing/portal
```

## Appendix H — Glossary

- **CTL** — Chronic Training Load. 42-day exponentially weighted average of TSS. "Fitness".
- **ATL** — Acute Training Load. 7-day EWMA of TSS. "Fatigue".
- **TSB** — Training Stress Balance. CTL − ATL. "Form".
- **TSS** — Training Stress Score. Quantified ride load.
- **IF** — Intensity Factor. NP / FTP for that ride.
- **NP** — Normalized Power. Variability-corrected average power.
- **FTP** — Functional Threshold Power. Roughly 1-hour sustainable power.
- **ERG** — Smart-trainer mode that holds a target wattage regardless of cadence.
- **FTMS** — Fitness Machine Service. Standard BLE service for trainer telemetry + control.
- **A/B/C race** — race priority. A = peak event. B = important. C = training/tune-up.
- **RLS** — Row-Level Security. Postgres feature for per-row access control.

## Appendix I — Launch Readiness Checklist

Consolidated so nothing is overlooked before public launch. `[x]` = shipped, `[ ]` = outstanding.
Updated 2026-07-16 after the auth + data-security build.

### Security & auth (Epic D)
- [x] Integration tokens (Strava/Dropbox/TP) encrypted at rest — `TOKEN_ENCRYPTION_KEY`, `EncryptedString`
- [x] Refresh-token rotation + revocation + reuse detection + `/auth/logout`
- [x] Login user-enumeration timing fix + per-IP rate limiting on auth endpoints
- [x] Security-headers middleware (nosniff, X-Frame DENY, Referrer, HSTS, COOP)
- [x] Account status (`is_active` / `deleted_at`) enforced on login + every authed request
- [x] SECRET_KEY rotated (local + Railway)
- [ ] **Email verification + password reset** — build the flow (pluggable sender, stubbed for beta); wire a live provider (Resend/Postmark) for public launch
- [ ] **OAuth social login (Google/Apple)** — needs Gareth to create the OAuth apps; Apple needs the paid dev account
- [ ] **Postgres RLS** — deferred (beta ships app-layer filtering + isolation test suite); add before public launch
- [ ] **MFA (TOTP)** — pre-public-launch per Appendix B
- [ ] **Rate limiter → Redis-backed** before running more than one backend instance (current limiter is process-local)

### GDPR & compliance (see Appendix B)
- [x] Data export endpoint (`GET /users/me/export`)
- [x] Account deletion (soft) + retention-window purge script (`scripts/purge_deleted_accounts.py`)
- [ ] **Schedule the purge** — cron / Railway scheduled job running the purge with `--commit`
- [ ] **ICO registration + annual fee** (~£40–60) — *Gareth's action*
- [ ] **Privacy policy live** (Claude drafts; Gareth reviews/publishes) — lawful basis, data-subject rights, health-data explicit consent, retention, breach process
- [ ] **Health-data (special-category) explicit consent** captured at onboarding
- [ ] **Cyber Essentials** (~£300, self-assessed)
- [ ] **Re-evaluate SOC 2 / ISO 27001** only if a partner/enterprise buyer requires it

### Platform dependencies (discovered 2026-07-23)
- [ ] **Strava API is now subscriber-only** — Strava deactivated the "Cycling Coach" app
      (`Application/Status/Inactive` on every call) because the owning Strava account is free.
      *Gareth's action:* subscribe on the owning Strava account to reactivate; recurring cost goes
      in the P&L. Riders connecting their own Strava accounts should NOT need a subscription
      (developer-side requirement) — re-verify once reactivated.
- [ ] **Data-in resilience** — Strava is now a paid, policy-volatile dependency. FIT upload +
      Dropbox sync already exist as independent channels; evaluate direct Garmin/Wahoo
      integrations before public launch so one platform can't cut off rider data.
- [ ] **Strava app rebrand** — consent screen still says "Cycling Coach" with the old Vercel URL;
      rename to FORMA + update website/callback branding before launch.
- [ ] **Vercel frontend deploys are MANUAL** (`npx vercel --prod` from `frontend/`; no git
      integration). Either add Vercel git auto-deploy or keep the manual step in the release
      ritual — the live site sat 15 days stale before 2026-07-23.

### Infra / deployment (Railway env vars + ops)
- [ ] Set **`TOKEN_ENCRYPTION_KEY`** + run `python -m scripts.encrypt_integration_tokens` once
- [ ] Set **`ADMIN_EMAILS`** (for `/admin/costs`)
- [ ] Set **`CORS_ORIGINS`** + **`FRONTEND_URL`** to the production frontend origin
- [ ] **Sentry** on frontend + backend
- [ ] **DB backups** (RPO ≤ 24h) + verify restore
- [ ] Per-user cost monitoring (the `/admin/costs` endpoint exists; add alerting at the $8/user hard threshold)

### Product gaps (from the 2026-07-15 PRD reconciliation)
- [x] `forma-core` single Claude funnel + `forma_calls` cost ledger + per-user monthly budget
- [x] **Semantic memory retrieval (RAG)** — in-process embeddings (fastembed, 384d) on every
      memory, per-message similarity recall in coach prompts (2026-07-25). pgvector INDEX
      deferred until a rider passes ~5k memories; the seams don't change.
- [ ] **ASR / voice input** — browser STT only today; decide on server-side ASR or confirm browser-only is acceptable
- [ ] **ElevenLabs voice** = the OQ2-locked pick (currently a different default voice id)
- [ ] **Conversational onboarding** — currently a form wizard (Milestone 2)
- [ ] **Billing (Epic G)** — Stripe subscription + quota tiers (the monthly budget cap is built; wire the subscription + Pro tier)
- [ ] **Test coverage** beyond the isolation suite — Forma-call path + ride-session path (per Appendix C tech debt)
